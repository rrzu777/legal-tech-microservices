import json
import multiprocessing
import threading
import time

import pytest

from app.cookie_store import CookieStore


def _hold_write_lock(path, ready, release) -> None:
    with CookieStore(path, lock_timeout_s=2.0)._exclusive_write_lock():
        ready.set()
        release.wait(5.0)


def _write_slot_after_start(path, slot_id, start, read_barrier) -> None:
    if not start.wait(5.0):
        raise RuntimeError("concurrent_writer_never_started")
    store = CookieStore(path)
    read_all_raw = store._read_all_raw

    def synchronize_after_read():
        slots = read_all_raw()
        try:
            # With the correct RMW lock, the first writer times out here while
            # the second blocks before reading. If a mutation locks only
            # _write_all(), both snapshots cross this barrier and the last
            # atomic rename deterministically loses the other slot.
            read_barrier.wait(0.2)
        except threading.BrokenBarrierError:
            pass
        return slots

    store._read_all_raw = synchronize_after_read
    store.save_slot(
        slot_id,
        cookies={"TSPD_101": f"cookie-{slot_id}"},
        user_agent=f"UA/{slot_id}",
        proxy_token=f"token-{slot_id}",
    )


@pytest.mark.parametrize("writer", ["save", "save_slot"])
def test_every_writer_times_out_without_overwriting_a_locked_store(tmp_path, writer):
    """Removing either public writer's lock must let this overwrite the store."""
    path = str(tmp_path / "cookies.json")
    store = CookieStore(path)
    store.save_slot("old", {"old": "bundle"}, "old-UA", "old-token")
    before = open(path).read()

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(target=_hold_write_lock, args=(path, ready, release))
    holder.start()
    assert ready.wait(5.0)

    try:
        contender = CookieStore(path, lock_timeout_s=0.05)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="cookie_store_lock_timeout"):
            if writer == "save":
                contender.save({"new": "bundle"}, "new-UA")
            else:
                contender.save_slot("new", {"new": "bundle"}, "new-UA", "new-token")
        assert time.monotonic() - started < 1.0
        assert open(path).read() == before
    finally:
        release.set()
        holder.join(5.0)

    assert holder.exitcode == 0


def test_two_spawned_slot_writers_preserve_both_slots_across_collisions(tmp_path):
    """An unlocked read-modify-write loses one slot in at least one collision."""
    context = multiprocessing.get_context("spawn")

    for collision in range(20):
        path = str(tmp_path / f"cookies-{collision}.json")
        start = context.Event()
        read_barrier = context.Barrier(2)
        writers = [
            context.Process(
                target=_write_slot_after_start,
                args=(path, slot_id, start, read_barrier),
            )
            for slot_id in ("0", "1")
        ]
        for process in writers:
            process.start()
        start.set()
        for process in writers:
            process.join(10.0)
            assert process.exitcode == 0

        assert set(CookieStore(path).load_all()) == {"0", "1"}
        json.loads(open(path).read())
