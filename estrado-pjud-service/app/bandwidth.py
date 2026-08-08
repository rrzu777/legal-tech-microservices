"""Global alert meter plus task-local proxy usage attribution.

This is a *secondary* early-warning signal, not the source of truth — the
IPRoyal dashboard is authoritative for billing. It's an in-memory counter
that resets on process restart; that's an accepted trade-off for a cheap
first cut (see docs/plans/2026-07-07-residential-proxy-pool.md, gap G5).
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable, Literal
from urllib.parse import urlencode


@dataclass
class ProxyUsageCapture:
    bytes_up: int = 0
    bytes_down: int = 0
    request_count: int = 0
    retry_count: int = 0
    documents_downloaded: int = 0
    documents_skipped: int = 0
    status: str | None = None
    error_kind: str | None = None
    cause_operation: Literal["opportunistic_catalog_refresh"] | None = None
    cause_session_id: uuid.UUID | None = None
    causal_event_persisted: bool = False
    on_first_request: Callable[["ProxyUsageCapture"], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def record_request(self, bytes_up: int = 0) -> None:
        if self.request_count == 0:
            on_first_request = self.on_first_request
            self.on_first_request = None
            if on_first_request is not None:
                on_first_request(self)
        self.request_count += 1
        self.bytes_up += max(0, bytes_up)


_ACTIVE_CAPTURE: ContextVar[ProxyUsageCapture | None] = ContextVar(
    "pjud_proxy_usage_capture", default=None,
)


@contextmanager
def capture_proxy_usage():
    capture = ProxyUsageCapture()
    token = _ACTIVE_CAPTURE.set(capture)
    try:
        yield capture
    finally:
        _ACTIVE_CAPTURE.reset(token)


def estimate_request_bytes(kwargs: dict) -> int:
    """Estimate payload bytes without retaining any request content."""
    content = kwargs.get("content")
    if isinstance(content, bytes):
        return len(content)
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    data = kwargs.get("data")
    if isinstance(data, dict):
        return len(urlencode(data, doseq=True).encode("utf-8"))
    if isinstance(data, bytes):
        return len(data)
    if isinstance(data, str):
        return len(data.encode("utf-8"))
    payload = kwargs.get("json")
    if payload is not None:
        return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    params = kwargs.get("params")
    if isinstance(params, dict):
        return len(urlencode(params, doseq=True).encode("utf-8"))
    return 0


def record_proxy_request(bytes_up: int = 0) -> None:
    capture = _ACTIVE_CAPTURE.get()
    if capture is not None:
        capture.record_request(bytes_up)


def record_proxy_response(bytes_down: int) -> None:
    METER.add(bytes_down)
    capture = _ACTIVE_CAPTURE.get()
    if capture is not None:
        capture.bytes_down += max(0, bytes_down)


def record_proxy_retry() -> None:
    capture = _ACTIVE_CAPTURE.get()
    if capture is not None:
        capture.retry_count += 1


class BandwidthMeter:
    def __init__(self):
        self._total_bytes = 0

    def add(self, nbytes: int) -> None:
        # CPython `+=` on an int is atomic under asyncio's single-threaded
        # event loop (no context switch happens mid-statement), so no lock
        # is needed here even though multiple coroutines call add().
        if nbytes and nbytes > 0:
            self._total_bytes += nbytes

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def total_gb(self) -> float:
        return self._total_bytes / (1024 ** 3)

    def reset(self) -> None:
        self._total_bytes = 0


METER = BandwidthMeter()  # process-global, in-memory (resets on restart — documented above)
