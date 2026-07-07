"""Process-global bandwidth meter for residential proxy egress (G5).

This is a *secondary* early-warning signal, not the source of truth — the
IPRoyal dashboard is authoritative for billing. It's an in-memory counter
that resets on process restart; that's an accepted trade-off for a cheap
first cut (see docs/plans/2026-07-07-residential-proxy-pool.md, gap G5).
"""


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
