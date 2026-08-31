"""Count-only submit-window observations, never authentication evidence.

Completion events may belong to requests started before the window. No request
identity, endpoint or browser object is retained; these are independent event
counts, not an in-flight ledger or a claim about an authentication endpoint.
"""
from __future__ import annotations

import re
import time


class _SubmitProbe:
    def __init__(self) -> None:
        self.counts = {
            **{f'{event}_{kind}': 0 for event in ('started', 'finished', 'failed')
               for kind in ('navigation', 'fetch', 'other')},
            **{f'http_{family}': 0 for family in ('1xx', '2xx', '3xx', '4xx', '5xx', 'other')},
            **{f'transport_{kind}': 0 for kind in ('reset', 'dns', 'proxy', 'timeout', 'aborted', 'other')},
            'js_errors': 0, 'inspection_attempts': 0, 'observer_errors': 0,
        }
        self.active = False
        self.started_at: float | None = None
        self.click_remaining_ms = self.return_remaining_ms = self.elapsed_ms = -1
        self.inspection = 'inspection_unavailable'
        self.inspection_failed = False
        self.location = 'unavailable'
        self._listeners = ()

    @staticmethod
    def _milliseconds(seconds: float) -> int:
        return max(0, min(45000, int(seconds * 1000)))

    def increment(self, key: str) -> None:
        self.counts[key] = min(999, self.counts[key] + 1)

    def _event(self, event: str, value: object) -> None:
        if not self.active:
            return
        try:
            if event == 'pageerror':
                self.increment('js_errors')  # Never inspect the error, even its type/text.
            elif event == 'response':
                status = value.status
                family = f'{status // 100}xx' if type(status) is int and 100 <= status <= 599 else 'other'
                self.increment(f'http_{family}')
            else:
                kind = ('navigation' if value.is_navigation_request() else
                        'fetch' if value.resource_type in {'xhr', 'fetch'} else 'other')
                self.increment(f'{event}_{kind}')
                if event == 'failed':
                    # Consume only the finite transport token; never retain text.
                    failure = value.failure
                    match = re.search(r'\bnet::(ERR_[A-Z_]+)\b', failure) if type(failure) is str else None
                    category = {
                        'ERR_CONNECTION_RESET': 'reset', 'ERR_NAME_NOT_RESOLVED': 'dns',
                        'ERR_TUNNEL_CONNECTION_FAILED': 'proxy', 'ERR_PROXY_CONNECTION_FAILED': 'proxy',
                        'ERR_TIMED_OUT': 'timeout', 'ERR_CONNECTION_TIMED_OUT': 'timeout',
                        'ERR_ABORTED': 'aborted',
                    }.get(match.group(1) if match else '', 'other')
                    self.increment(f'transport_{category}')
        except Exception:
            self.increment('observer_errors')

    def start(self, page: object, deadline: float) -> None:
        self.started_at = time.monotonic()
        self.click_remaining_ms = self._milliseconds(deadline - self.started_at)
        self.active = True
        self._listeners = tuple(
            (name, lambda value, event=event: self._event(event, value))
            for name, event in (
                ('request', 'started'), ('requestfinished', 'finished'),
                ('requestfailed', 'failed'), ('response', 'response'), ('pageerror', 'pageerror'),
            )
        )
        for name, callback in self._listeners:
            try:
                page.on(name, callback)
            except Exception:
                self.increment('observer_errors')

    def stop(self, page: object, deadline: float) -> None:
        if not self.active:
            return
        self.active = False  # Disable even if a closed page refuses listener removal.
        now = time.monotonic()
        self.return_remaining_ms = self._milliseconds(deadline - now)
        self.elapsed_ms = self._milliseconds(now - self.started_at)
        for name, callback in self._listeners:
            try:
                page.remove_listener(name, callback)
            except Exception:
                self.increment('observer_errors')
        self._listeners = ()

    def summary(self) -> str:
        values = {
            'click_remaining_ms': self.click_remaining_ms,
            'return_remaining_ms': self.return_remaining_ms,
            'elapsed_ms': self.elapsed_ms,
            'inspection': self.inspection, 'location': self.location, **self.counts,
        }
        return ' '.join(f'submit_{key}={value}' for key, value in values.items())
