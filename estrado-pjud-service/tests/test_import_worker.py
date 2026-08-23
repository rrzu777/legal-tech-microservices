import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from app.familia.auth import InvalidCredentialsError
from app.my_causes.models import ImportCandidate
from app.my_causes.client import DiscoveryResult
from worker.import_jobs import ImportDiscoveryWorker


JOB = {
    "status": "acquired",
    "job_id": "98200000-0000-4000-8000-000000000041",
    "law_firm_id": "98200000-0000-4000-8000-000000000001",
    "credential_id": "98200000-0000-4000-8000-000000000021",
    "matters": ["civil"],
    "include_closed": False,
    "claim_token": "98200000-0000-4000-8000-000000000099",
    "lease_expires_at": "2026-08-23T13:00:00+00:00",
}


class Rpc:
    def __init__(self, response):
        self.response = response

    def execute(self):
        if isinstance(self.response, BaseException):
            raise self.response
        return SimpleNamespace(data=self.response, error=None)


class FakeSupabase:
    def __init__(self, claim=JOB):
        self.claim = claim
        self.calls = []
        self.renew_result = True

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        if name == "claim_pjud_import_job":
            return Rpc(self.claim)
        if name == "renew_pjud_import_job_claim":
            return Rpc(self.renew_result)
        if name == "finalize_pjud_import_discovery":
            return Rpc(None)
        raise AssertionError(name)


class FakeSession:
    def __init__(self, *, login_error=None):
        self.login = AsyncMock(side_effect=login_error)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


class FakePool:
    def __init__(self):
        self.bundle = SimpleNamespace(proxy_url="http://proxy.invalid", cookies=(), user_agent="ua")
        self.slot = object()
        self.acquire_familia_bundle = AsyncMock(return_value=(self.bundle, self.slot))
        self.release_familia_bundle = AsyncMock()


def candidate(**overrides):
    values = {
        "matter": "civil",
        "case_type": "rit",
        "case_number": "C-10-2026",
        "court_code": 90,
        "court_label": "C.A. Santiago",
        "tribunal_code": 259,
        "tribunal_label": "1 Civil Santiago",
        "libro": "C",
        "filed_at": None,
        "upstream_status": "Abierta",
        "caption": "Persona A / Persona B",
    }
    values.update(overrides)
    return ImportCandidate.model_validate(values)


def make_worker(*, claim=JOB, discovery=None, credential=None, session=None, concurrency=1):
    sb = FakeSupabase(claim)
    pool = FakePool()
    discovery = discovery or AsyncMock(return_value=DiscoveryResult(
        candidates=[candidate()], page_count=1, status="ok",
    ))
    credential = credential or AsyncMock(return_value={
        "rut": "11111111-1", "password": "secret-value", "password_type": "clave_poder_judicial",
    })
    session = session or FakeSession()
    session_factory = MagicMock(return_value=session)
    worker = ImportDiscoveryWorker(
        supabase=sb,
        pool=pool,
        worker_id="import-worker",
        fetch_credential=credential,
        discover=discovery,
        session_factory=session_factory,
        concurrency=concurrency,
        lease_seconds=30,
        renewal_interval_seconds=0.001,
    )
    return worker, sb, pool, discovery, credential, session_factory, session


@pytest.mark.asyncio
async def test_claims_fetches_scoped_credential_discovers_and_finalizes_once():
    worker, sb, pool, discover, credential, session_factory, session = make_worker()

    assert await worker.process_next() is True

    credential.assert_awaited_once_with(JOB["credential_id"], JOB["law_firm_id"])
    session.login.assert_awaited_once_with("11111111-1", "secret-value", "clave_pj")
    discover.assert_awaited_once()
    assert sb.calls[0] == (
        "claim_pjud_import_job",
        {"p_worker_id": "import-worker", "p_lease_seconds": 30},
    )
    finalize = [entry for entry in sb.calls if entry[0] == "finalize_pjud_import_discovery"]
    assert len(finalize) == 1
    payload = finalize[0][1]
    assert payload["p_job_id"] == JOB["job_id"]
    assert payload["p_claim_token"] == JOB["claim_token"]
    assert payload["p_summary"] == {
        "status": "needs_selection", "pages": 1, "discovered": 1,
    }
    persisted = payload["p_candidates"][0]
    assert set(persisted) == {
        "matter", "case_type", "case_number", "court_code", "court_label",
        "tribunal_code", "tribunal_label", "libro", "filed_at",
        "upstream_status", "caption", "source_hash",
    }
    assert "secret-value" not in json.dumps(payload)
    pool.release_familia_bundle.assert_awaited_once()
    assert session.closed is True


@pytest.mark.asyncio
async def test_empty_claim_is_a_noop_without_credential_or_pjud_traffic():
    worker, sb, pool, discover, credential, *_ = make_worker(claim={"status": "empty"})

    assert await worker.process_next() is False

    credential.assert_not_awaited()
    discover.assert_not_awaited()
    pool.acquire_familia_bundle.assert_not_awaited()
    assert [name for name, _ in sb.calls] == ["claim_pjud_import_job"]


@pytest.mark.asyncio
async def test_zero_candidates_finalize_as_terminal_completed_via_rpc_contract():
    result = DiscoveryResult(candidates=[], page_count=1, status="ok")
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_candidates"] == []
    assert final["p_summary"] == {
        "status": "needs_selection", "pages": 1, "discovered": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_status", "error_code"),
    [
        ("credential_invalid", "failed", "credential_invalid"),
        ("session_expired", "failed", "session_expired"),
        ("waf", "failed", "ojv_blocked"),
        ("timeout", "failed", "pjud_timeout"),
        ("upstream_changed", "partial", "upstream_changed"),
    ],
)
async def test_terminal_results_finalize_with_closed_error_taxonomy(status, expected_status, error_code):
    result = DiscoveryResult(candidates=[], page_count=2, status=status)
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_candidates"] == []
    assert final["p_summary"] == {
        "status": expected_status,
        "pages": 2,
        "discovered": 0,
        "error_code": error_code,
        "error_class": "upstream" if status == "upstream_changed" else "authentication" if status in {"credential_invalid", "session_expired"} else "transport",
    }


@pytest.mark.asyncio
async def test_invalid_login_is_terminal_and_never_calls_listing_discovery():
    session = FakeSession(login_error=InvalidCredentialsError("credential rejected"))
    worker, sb, _pool, discover, *_ = make_worker(session=session)

    assert await worker.process_next() is True

    discover.assert_not_awaited()
    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_summary"]["error_code"] == "credential_invalid"


@pytest.mark.asyncio
async def test_crash_leaves_claim_unfinalized_for_database_reclaim():
    worker, sb, pool, *_ = make_worker(discovery=AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await worker.process_next()

    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)
    pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_renews_exact_claim_while_discovery_is_running():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_discovery(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return DiscoveryResult(candidates=[], page_count=1, status="ok")

    worker, sb, *_ = make_worker(discovery=AsyncMock(side_effect=slow_discovery))
    task = asyncio.create_task(worker.process_next())
    await started.wait()
    await asyncio.sleep(0.01)
    finish.set()
    assert await task is True

    renewals = [payload for name, payload in sb.calls if name == "renew_pjud_import_job_claim"]
    assert renewals
    assert renewals[0] == {
        "p_job_id": JOB["job_id"],
        "p_claim_token": JOB["claim_token"],
        "p_worker_id": "import-worker",
        "p_lease_seconds": 30,
    }


@pytest.mark.asyncio
async def test_lease_heartbeat_starts_before_slow_internal_credential_fetch():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_fetch(*_args):
        started.set()
        await finish.wait()
        return {
            "rut": "11111111-1",
            "password": "secret-value",
            "password_type": "clave_poder_judicial",
        }

    worker, sb, *_ = make_worker(credential=AsyncMock(side_effect=slow_fetch))
    task = asyncio.create_task(worker.process_next())
    await started.wait()
    await asyncio.sleep(0.01)
    assert any(name == "renew_pjud_import_job_claim" for name, _ in sb.calls)
    finish.set()
    assert await task is True


@pytest.mark.asyncio
async def test_lost_lease_cancels_discovery_and_stale_worker_never_finalizes():
    started = asyncio.Event()

    async def never_finishes(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    worker, sb, *_ = make_worker(discovery=AsyncMock(side_effect=never_finishes))
    sb.renew_result = False
    task = asyncio.create_task(worker.process_next())
    await started.wait()

    with pytest.raises(RuntimeError, match="import_job_claim_lost"):
        await asyncio.wait_for(task, timeout=0.1)

    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)


@pytest.mark.asyncio
async def test_canonical_source_hash_ignores_caption_and_row_position():
    discovery = AsyncMock(return_value=DiscoveryResult(
        candidates=[candidate(caption="First"), candidate(caption="Changed")],
        page_count=1,
        status="ok",
    ))
    worker, sb, *_ = make_worker(discovery=discovery)

    await worker.process_next()

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert len(final["p_candidates"]) == 1
    assert final["p_candidates"][0]["caption"] == "Changed"


@pytest.mark.asyncio
async def test_candidate_allowlist_rejects_nested_or_oversized_values_before_rpc():
    bad = candidate().model_dump()
    bad["caption"] = {"raw": "forbidden"}
    discovery = AsyncMock(return_value=SimpleNamespace(candidates=[bad], page_count=1, status="ok"))
    worker, sb, *_ = make_worker(discovery=discovery)

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"]
    assert final[0]["p_candidates"] == []
    assert final[0]["p_summary"]["error_code"] == "invalid_candidate_payload"


@pytest.mark.asyncio
async def test_deterministic_candidate_overflow_finalizes_failed_instead_of_reclaim_loop():
    result = SimpleNamespace(candidates=[candidate()] * 1001, page_count=2, status="ok")
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"]
    assert len(final) == 1
    assert final[0]["p_candidates"] == []
    assert final[0]["p_summary"] == {
        "status": "failed",
        "pages": 2,
        "discovered": 0,
        "error_code": "invalid_candidate_payload",
        "error_class": "contract",
    }


@pytest.mark.asyncio
async def test_incomplete_same_number_different_tribunal_labels_are_not_collapsed():
    result = DiscoveryResult(
        candidates=[
            candidate(tribunal_code=None, tribunal_label="1 Civil Santiago"),
            candidate(tribunal_code=None, tribunal_label="2 Civil Santiago"),
        ],
        page_count=1,
        status="ok",
    )
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    await worker.process_next()

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert len(final["p_candidates"]) == 2
    assert final["p_candidates"][0]["source_hash"] != final["p_candidates"][1]["source_hash"]


@pytest.mark.asyncio
async def test_discovery_concurrency_budget_is_independent_and_bounded():
    active = 0
    max_active = 0
    release = asyncio.Event()

    async def discover(*_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await release.wait()
        active -= 1
        return DiscoveryResult(candidates=[], page_count=1, status="ok")

    worker, *_ = make_worker(discovery=AsyncMock(side_effect=discover), concurrency=1)
    first = asyncio.create_task(worker.process_claimed(dict(JOB)))
    second = asyncio.create_task(worker.process_claimed({**JOB, "job_id": "98200000-0000-4000-8000-000000000042"}))
    await asyncio.sleep(0.01)
    assert max_active == 1
    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1


@pytest.mark.asyncio
async def test_concurrent_pollers_claim_only_after_entering_concurrency_budget():
    worker, sb, *_ = make_worker(concurrency=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_process(_job):
        entered.set()
        await release.wait()

    worker._process_claimed_with_budget = blocked_process
    first = asyncio.create_task(worker.process_next())
    await entered.wait()
    second = asyncio.create_task(worker.process_next())
    await asyncio.sleep(0.01)

    assert [name for name, _ in sb.calls].count("claim_pjud_import_job") == 1
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_cancelling_outer_processing_cancels_operation_and_heartbeat():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def cancellable_discovery(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    worker, sb, pool, *_ = make_worker(discovery=AsyncMock(side_effect=cancellable_discovery))
    task = asyncio.create_task(worker.process_next())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    renewals_at_cancel = [name for name, _ in sb.calls].count("renew_pjud_import_job_claim")
    await asyncio.sleep(0.01)

    assert stopped.is_set()
    assert [name for name, _ in sb.calls].count("renew_pjud_import_job_claim") == renewals_at_cancel
    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)
    pool.release_familia_bundle.assert_awaited_once_with(
        pool.slot, disposition="healthy", remint=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_number", "C" * 129),
        ("court_label", "C" * 201),
        ("tribunal_label", "T" * 201),
        ("libro", "L" * 81),
        ("upstream_status", "S" * 101),
    ],
)
async def test_sql_field_limits_are_rejected_deterministically_before_finalize(field, value):
    result = DiscoveryResult(
        candidates=[candidate(**{field: value})], page_count=1, status="ok",
    )
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_candidates"] == []
    assert final["p_summary"]["error_code"] == "invalid_candidate_payload"


@pytest.mark.asyncio
async def test_engine_import_poll_does_not_use_paid_sync_batch_semaphore():
    from worker.engine import SyncEngine

    import_worker = SimpleNamespace(process_next=AsyncMock(return_value=False))
    engine = SyncEngine.__new__(SyncEngine)
    engine._import_worker = import_worker

    assert await engine.process_import_job() is False
    import_worker.process_next.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_main_loop_import_poll_contains_failure_without_crashing_paid_sync(caplog):
    from worker.__main__ import safe_process_import_job

    engine = SimpleNamespace(
        process_import_job=AsyncMock(side_effect=RuntimeError("secret RIT C-10-2026")),
    )
    metrics = SimpleNamespace(record_error=MagicMock())

    assert await safe_process_import_job(engine, metrics) is False
    metrics.record_error.assert_called_once_with("infra")
    assert "secret" not in caplog.text
    assert "C-10-2026" not in caplog.text


@pytest.mark.asyncio
async def test_import_completion_log_is_aggregate_only(caplog):
    caplog.set_level("INFO", logger="worker.import_jobs")
    worker, *_ = make_worker()
    await worker.process_next()

    assert JOB["job_id"] not in caplog.text
    assert JOB["law_firm_id"] not in caplog.text
    assert "status=ok" in caplog.text
    assert "pages=1" in caplog.text
    assert "count=1" in caplog.text


@pytest.mark.asyncio
async def test_import_poll_loop_is_independent_and_shutdown_cancels_inflight_work():
    from worker.__main__ import run_import_discovery_loop

    shutdown = asyncio.Event()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def process():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    engine = SimpleNamespace(process_import_job=AsyncMock(side_effect=process))
    metrics = SimpleNamespace(record_error=MagicMock())
    loop = asyncio.create_task(
        run_import_discovery_loop(engine, metrics, shutdown, poll_interval=0.001),
    )
    await entered.wait()
    shutdown.set()
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)

    assert cancelled.is_set()


def test_import_budget_reserves_at_least_one_session_for_public_sync():
    from worker.__main__ import public_sync_concurrency

    assert public_sync_concurrency(3, imports_enabled=True) == 2
    assert public_sync_concurrency(2, imports_enabled=True) == 1


def test_import_budget_is_not_reserved_while_import_flag_is_disabled():
    from worker.__main__ import public_sync_concurrency

    assert public_sync_concurrency(3, imports_enabled=False) == 3
    assert public_sync_concurrency(1, imports_enabled=False) == 1


@pytest.mark.asyncio
async def test_import_credential_fetch_treats_internal_outage_as_retryable():
    from worker.engine import ImportCredentialInfrastructureError, SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._call_app_internal = AsyncMock(return_value=None)

    with pytest.raises(ImportCredentialInfrastructureError):
        await engine._get_import_credential(JOB["credential_id"], JOB["law_firm_id"])


@pytest.mark.asyncio
async def test_import_credential_fetch_keeps_terminal_not_found_distinct():
    from worker.engine import SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._call_app_internal = AsyncMock(
        return_value=SimpleNamespace(status_code=404, json=lambda: {"error": "not found"}),
    )

    assert await engine._get_import_credential(JOB["credential_id"], JOB["law_firm_id"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"rut": "11111111-1"}, {
    "rut": "11111111-1", "password": "secret", "password_type": "clave_unica",
}])
async def test_import_credential_fetch_retries_malformed_200_contract(payload):
    from worker.engine import ImportCredentialInfrastructureError, SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._call_app_internal = AsyncMock(
        return_value=SimpleNamespace(status_code=200, json=lambda: payload),
    )

    with pytest.raises(ImportCredentialInfrastructureError):
        await engine._get_import_credential(JOB["credential_id"], JOB["law_firm_id"])
