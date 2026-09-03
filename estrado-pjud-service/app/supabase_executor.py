"""Non-blocking serialization for each synchronous Supabase HTTP session."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import threading
import weakref


_SESSION_EXECUTORS_GUARD = threading.Lock()
_SESSION_EXECUTORS: weakref.WeakKeyDictionary[object, ThreadPoolExecutor] = (
    weakref.WeakKeyDictionary()
)
_UNKNOWN_SESSION_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="supabase-unknown-session",
)


def _new_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="supabase-session")


def _query_executor(query) -> ThreadPoolExecutor:
    request = getattr(query, "request", None)
    session = getattr(request, "session", None)
    if session is None:
        return _UNKNOWN_SESSION_EXECUTOR
    try:
        with _SESSION_EXECUTORS_GUARD:
            executor = _SESSION_EXECUTORS.get(session)
            if executor is None:
                executor = _new_executor()
                _SESSION_EXECUTORS[session] = executor
                weakref.finalize(
                    session,
                    executor.shutdown,
                    wait=False,
                    cancel_futures=False,
                )
            return executor
    except TypeError:
        # Test doubles or a future client may not support weak references. The
        # conservative fallback keeps those executions serialized.
        return _UNKNOWN_SESSION_EXECUTOR


def submit_supabase_query(query) -> asyncio.Future:
    """Queue one query on its session without occupying unrelated executors."""
    return asyncio.get_running_loop().run_in_executor(
        _query_executor(query),
        copy_context().run,
        query.execute,
    )


async def execute_supabase_query(query):
    """Await a serialized query for callers that do not register auxiliaries."""
    return await submit_supabase_query(query)
