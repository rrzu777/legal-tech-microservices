"""Admission and cancellation with real protocol files, no application imports."""
import asyncio
import importlib
import threading

import pytest

from test_worker_maintenance_store import (CREATED, NEXT_OPERATION, OPERATION, SERVICE,
                                         identity, protocol)


def coordinator(protocol):
    m, store, _, _ = protocol
    source = SERVICE / "worker" / "maintenance.py"
    assert source.is_file(), "Missing worker admission coordinator implementation"
    module = importlib.import_module("worker.maintenance")
    store.initialize_hold(OPERATION)
    return m, module, store, module.WorkerMaintenance(store, identity(m))


def open_admission(m, store):
    store.transition(OPERATION, "hold", m.Control(1, "open", OPERATION, CREATED))


def operator_hold(m, store):
    store.transition(OPERATION, "open", m.Control(1, "hold", NEXT_OPERATION, CREATED))


def ack_state(store, worker):
    control = store.read_control()
    return store.read_ack(expected_operation_id=control.operation_id,
                          expected_identity=worker.identity).state


def can_lock(m, store):
    try:
        with store.exclusive_lease():
            return True
    except m.AdmissionClosed:
        return False


def test_hold_drains_existing_operation_without_admitting_another(protocol):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        open_admission(m, store)
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def work():
            entered.set()
            await finish.wait()
            return 7
        running = asyncio.create_task(worker.run(work))
        await entered.wait()
        operator_hold(m, store)
        with pytest.raises(m.AdmissionClosed):
            await worker.run(lambda: calls.append("forbidden-claim"))
        assert calls == []
        assert not can_lock(m, store)
        assert worker.inflight == 1
        assert ack_state(store, worker) == "draining"
        finish.set()
        assert await running == 7
        assert worker.inflight == 0
        assert can_lock(m, store)
        assert ack_state(store, worker) == "quiescent"
    asyncio.run(scenario())


def test_hold_survives_new_coordinator_and_never_creates_work(protocol):
    async def scenario():
        m, module, store, worker = coordinator(protocol)
        worker.publish_ack()
        assert ack_state(store, worker) == "quiescent"
        new = module.WorkerMaintenance(store, identity(m))
        calls = []
        with pytest.raises(m.AdmissionClosed):
            await new.run(lambda: calls.append("forbidden-startup"))
        assert calls == []
        assert store.read_control().state == "hold"
        assert ack_state(store, new) == "quiescent"
    asyncio.run(scenario())


def test_open_zero_work_advertises_capability_but_not_quiescence(protocol):
    m, _, store, worker = coordinator(protocol)
    open_admission(m, store)
    worker.publish_ack()
    assert ack_state(store, worker) == "draining"
    assert worker.inflight == 0


def test_exclusive_operator_lease_blocks_work_but_allows_closed_startup_ack(protocol):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        with store.exclusive_lease():
            worker.publish_ack()
            assert ack_state(store, worker) == "quiescent"
            with pytest.raises(m.AdmissionClosed):
                await worker.run(lambda: pytest.fail("work created under exclusive lock"))
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_invalid_control_never_calls_operation_or_emits_safe_ack(protocol, damage):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        path = protocol[2] / "control.json"
        if damage == "missing":
            path.unlink()
        else:
            path.write_text("private-sentinel")
        with pytest.raises(m.MaintenanceError):
            await worker.run(lambda: pytest.fail("unsafe admission"))
        assert worker.inflight == 0
        assert worker.uncertain
        assert not (protocol[3] / "ack.json").exists()
    asyncio.run(scenario())


def test_failed_ack_prevents_operation_creation_and_marks_uncertainty(protocol):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        open_admission(m, store)
        protocol[3].chmod(0o755)
        with pytest.raises(m.MaintenanceError):
            await worker.run(lambda: pytest.fail("work created despite unavailable ACK"))
        assert worker.inflight == 0
        assert worker.uncertain
        assert not can_lock(m, store)
    asyncio.run(scenario())


def test_exception_is_sticky_uncertain_and_never_retried(protocol):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        open_admission(m, store)
        calls = []
        async def work():
            calls.append("once")
            operator_hold(m, store)
            raise ValueError("remote-result-unknown")
        with pytest.raises(ValueError):
            await worker.run(work)
        assert calls == ["once"]
        assert worker.uncertain
        assert worker.inflight == 0
        assert ack_state(store, worker) == "draining"
        worker.publish_ack()
        assert ack_state(store, worker) == "draining"
    asyncio.run(scenario())


def test_explicit_uncertainty_blocks_quiescence_with_zero_work(protocol):
    m, _, store, worker = coordinator(protocol)
    worker.mark_uncertain()
    worker.publish_ack()
    assert worker.inflight == 0
    assert worker.uncertain
    assert ack_state(store, worker) == "draining"


def test_cancellation_retains_lease_until_tracked_real_thread_finishes(protocol):
    async def scenario():
        m, module, store, worker = coordinator(protocol)
        open_admission(m, store)
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def thread_work():
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5)
            return 11
        async def work():
            future = loop.run_in_executor(None, thread_work)
            return await module.track_auxiliary(future)
        running = asyncio.create_task(worker.run(work))
        try:
            await entered.wait()
            operator_hold(m, store)
            running.cancel()
            await asyncio.sleep(0)
            running.cancel()  # Repeated cancellation must not abandon the FD.
            await asyncio.sleep(0)
            assert not running.done()
            assert worker.inflight == 1
            assert not can_lock(m, store)
            assert ack_state(store, worker) == "draining"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert not can_lock(m, store)
        assert worker.inflight == 0
        assert worker.uncertain
        assert ack_state(store, worker) == "draining"
    asyncio.run(scenario())


def test_auxiliary_outliving_success_keeps_admission_alive_until_completion(protocol):
    async def scenario():
        m, module, store, worker = coordinator(protocol)
        open_admission(m, store)
        entered, release = asyncio.Event(), asyncio.Event()
        async def auxiliary():
            entered.set()
            await release.wait()
        async def work():
            module.track_auxiliary(asyncio.create_task(auxiliary()))
            return 22
        running = asyncio.create_task(worker.run(work))
        await entered.wait()
        operator_hold(m, store)
        worker.publish_ack()
        assert worker.inflight == 1
        assert not running.done()
        assert not can_lock(m, store)
        release.set()
        assert await running == 22
        assert not worker.uncertain
        assert ack_state(store, worker) == "quiescent"
    asyncio.run(scenario())


def test_auxiliary_exception_prevents_quiescence_even_after_body_success(protocol):
    async def scenario():
        m, module, store, worker = coordinator(protocol)
        open_admission(m, store)
        async def bad_auxiliary():
            raise RuntimeError("remote-outcome-unknown")
        async def work():
            module.track_auxiliary(asyncio.create_task(bad_auxiliary()))
            operator_hold(m, store)
            return 22
        await worker.run(work)
        assert worker.uncertain
        assert ack_state(store, worker) == "draining"
    asyncio.run(scenario())


def test_overlapping_operations_do_not_share_an_unlock(protocol):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        open_admission(m, store)
        started = [asyncio.Event(), asyncio.Event()]
        finished = [asyncio.Event(), asyncio.Event()]
        async def work(index):
            started[index].set()
            await finished[index].wait()
        first = asyncio.create_task(worker.run(lambda: work(0)))
        second = asyncio.create_task(worker.run(lambda: work(1)))
        await asyncio.gather(*(event.wait() for event in started))
        operator_hold(m, store)
        finished[0].set()
        await first
        assert worker.inflight == 1
        assert not can_lock(m, store)
        assert ack_state(store, worker) == "draining"
        finished[1].set()
        await second
        assert can_lock(m, store)
        assert ack_state(store, worker) == "quiescent"
    asyncio.run(scenario())


def test_uncertainty_retains_lock_even_with_stale_quiescent_ack_and_write_failure(protocol):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        worker.publish_ack()
        assert ack_state(store, worker) == "quiescent"
        protocol[3].chmod(0o500)  # Invalid ACK directory; previous bytes remain intact.
        worker.mark_uncertain()
        with pytest.raises(m.MaintenanceError):
            worker.publish_ack()
        protocol[3].chmod(0o700)
        assert ack_state(store, worker) == "quiescent"  # Stale proof is still on disk.
        assert not can_lock(m, store)  # But its compulsory exclusive lock is denied.
        open_admission(m, store)
        with pytest.raises(m.AdmissionClosed):
            await worker.run(lambda: pytest.fail("uncertain instance admitted new work"))
        assert not can_lock(m, store)
    asyncio.run(scenario())


def test_uncertain_operation_transfers_lease_without_reopening_lock_file(protocol):
    async def scenario():
        m, _, store, worker = coordinator(protocol)
        open_admission(m, store)
        async def work():
            operator_hold(m, store)
            protocol[2].chmod(0o500)  # Reacquisition/ACK forbidden: retain existing FD.
            raise RuntimeError("unknown")
        with pytest.raises(RuntimeError):
            await worker.run(work)
        protocol[2].chmod(0o750)
        assert not can_lock(m, store)
    asyncio.run(scenario())


def test_context_probe_preserves_unrelated_api_callers_and_tracks_only_admitted_work(protocol):
    async def scenario():
        m, module, store, worker = coordinator(protocol)
        assert not module.has_active_operation()
        open_admission(m, store)
        async def work():
            assert module.has_active_operation()
            module.mark_uncertain()
        await worker.run(work)
        assert not module.has_active_operation()
        assert worker.uncertain
    asyncio.run(scenario())


def test_closed_inherited_context_rejects_before_creating_thread_under_exclusive(protocol):
    async def scenario():
        m, module, store, worker = coordinator(protocol)
        open_admission(m, store)
        release_child = asyncio.Event()
        submitted = []
        thread_lock_observations = []
        loop = asyncio.get_running_loop()

        def thread_body():
            # A real thread records whether the operator's EX was still held.
            thread_lock_observations.append(can_lock(m, store))

        async def delayed_inherited_child():
            await release_child.wait()
            # Production integration contract: probe, create, register without await.
            tracked = module.has_active_operation()
            future = loop.run_in_executor(None, thread_body)
            submitted.append(future)
            return await (module.track_auxiliary(future) if tracked else future)

        children = []
        async def operation():
            children.append(asyncio.create_task(delayed_inherited_child()))

        await worker.run(operation)
        operator_hold(m, store)
        worker.publish_ack()
        assert ack_state(store, worker) == "quiescent"
        with store.exclusive_lease():
            release_child.set()
            with pytest.raises(m.MaintenanceError):
                await children[0]
            # In the broken implementation a thread was already submitted before
            # track_auxiliary rejected. Await it while EX remains held for an
            # unambiguous observation, with no timing-dependent sleep.
            if submitted:
                await asyncio.gather(*submitted)
            assert thread_lock_observations == []
            assert submitted == []
    asyncio.run(scenario())
