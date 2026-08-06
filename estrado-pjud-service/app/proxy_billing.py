"""Strict detection of residential-proxy billing failures."""

from __future__ import annotations

import re

import httpx

_BILLING_SIGNAL = re.compile(r"(?:\b402\b|payment required)", re.IGNORECASE)


def is_proxy_billing_error(error: BaseException) -> bool:
    """Return true only for a 402 signal carried by ``httpx.ProxyError``.

    PJUD's own HTTP status must never trip provider billing. Causes and
    contexts are inspected because httpx may wrap CONNECT/tunnel failures.
    """
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.ProxyError) and _BILLING_SIGNAL.search(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False
