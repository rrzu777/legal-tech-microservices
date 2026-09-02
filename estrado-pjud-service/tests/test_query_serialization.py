import asyncio
import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime_fence import RuntimeFence
from worker.config import run_query


class Session:
    pass


def query(execute, session):
    return SimpleNamespace(
        execute=execute,
        request=SimpleNamespace(session=session),
    )


def test_runtime_sources_cannot_bypass_serialized_execute_helper():
    service_root = Path(__file__).resolve().parents[1]
    helper = service_root / "app" / "supabase_executor.py"
    violations = []
    for source_root in (service_root / "app", service_root / "worker"):
        for source in source_root.rglob("*.py"):
            if source == helper:
                continue
            tree = ast.parse(source.read_text(), filename=str(source))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "execute"
                ):
                    violations.append(f"{source.relative_to(service_root)}:{node.lineno}")
    assert violations == []


@pytest.mark.asyncio
async def test_cancelled_query_keeps_shared_client_serialized_until_thread_finishes():
    session = Session()
    first_started = threading.Event()
    finish_first = threading.Event()
    second_started = threading.Event()

    def first_execute():
        first_started.set()
        assert finish_first.wait(2), "test did not release the first query"
        return "first"

    def second_execute():
        second_started.set()
        return "second"

    first = asyncio.create_task(
        run_query(query(first_execute, session)),
    )
    try:
        assert await asyncio.to_thread(first_started.wait, 1)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

        second = asyncio.create_task(
            run_query(query(second_execute, session)),
        )
        await asyncio.sleep(0.05)
        assert not second_started.is_set()

        finish_first.set()
        assert await asyncio.wait_for(second, 1) == "second"
    finally:
        finish_first.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_fence_and_worker_query_share_client_serialization():
    session = Session()
    first_started = threading.Event()
    finish_first = threading.Event()
    fence_started = threading.Event()

    def first_execute():
        first_started.set()
        assert finish_first.wait(2), "test did not release the worker query"
        return "worker"

    class FenceQuery:
        request = SimpleNamespace(session=session)

        def execute(self):
            fence_started.set()
            return SimpleNamespace(data={
                "protocol_version": 1,
                "revision": 0,
                "admission_paused": False,
                "generation_required": False,
                "generation": None,
                "sealed_at": None,
                "bindings": None,
            })

    supabase = SimpleNamespace(rpc=lambda *_args, **_kwargs: FenceQuery())
    worker_query = asyncio.create_task(
        run_query(query(first_execute, session)),
    )
    try:
        assert await asyncio.to_thread(first_started.wait, 1)
        fence_query = asyncio.create_task(RuntimeFence(supabase, None).snapshot())
        await asyncio.sleep(0.05)
        assert not fence_started.is_set()

        finish_first.set()
        snapshot = await asyncio.wait_for(fence_query, 1)
        assert snapshot.revision == 0
        assert await worker_query == "worker"
    finally:
        finish_first.set()
        await asyncio.gather(worker_query, return_exceptions=True)


@pytest.mark.asyncio
async def test_independent_supabase_sessions_do_not_block_each_other():
    finish_first = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()

    def first_execute():
        first_started.set()
        assert finish_first.wait(2), "test did not release the first session"

    def second_execute():
        second_started.set()
        return "second"

    first = asyncio.create_task(run_query(query(first_execute, Session())))
    try:
        assert await asyncio.to_thread(first_started.wait, 1)
        second = asyncio.create_task(run_query(query(second_execute, Session())))
        assert await asyncio.to_thread(second_started.wait, 1)
        assert await asyncio.wait_for(second, 1) == "second"
    finally:
        finish_first.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_same_session_backlog_cannot_starve_an_independent_session():
    session_a = Session()
    session_b = Session()
    finish_holder = threading.Event()
    holder_started = threading.Event()
    independent_started = threading.Event()

    def hold_session_a():
        holder_started.set()
        assert finish_holder.wait(2), "test did not release session A"

    holder = asyncio.create_task(run_query(query(hold_session_a, session_a)))
    waiters = []
    independent = None
    try:
        assert await asyncio.to_thread(holder_started.wait, 1)
        waiters = [
            asyncio.create_task(run_query(query(lambda: None, session_a)))
            for _ in range(64)
        ]
        await asyncio.sleep(0.05)
        independent = asyncio.create_task(
            run_query(query(lambda: independent_started.set(), session_b)),
        )
        for _ in range(100):
            if independent_started.is_set():
                break
            await asyncio.sleep(0.005)
        assert independent_started.is_set()
        await asyncio.wait_for(independent, 1)
    finally:
        finish_holder.set()
        await asyncio.gather(holder, *waiters, return_exceptions=True)
        if independent is not None:
            await asyncio.gather(independent, return_exceptions=True)
