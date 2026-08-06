"""Persistent fail-closed proxy control backed by Supabase."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from worker.config import run_query

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProxyControlSnapshot:
    allowed: bool
    status: str
    reason_code: str | None
    revision: int | None
    source: str


class ProxyControl:
    def __init__(
        self,
        supabase,
        provider: str = "iproyal",
        actor: str = "estrado-pjud-worker",
    ):
        self._sb = supabase
        self._provider = provider
        self._actor = actor
        self._lock = asyncio.Lock()
        self._snapshot = ProxyControlSnapshot(
            allowed=False,
            status="unavailable",
            reason_code="not_loaded",
            revision=None,
            source="local",
        )
        self._local_billing_trip_revision: int | None = None

    @property
    def snapshot(self) -> ProxyControlSnapshot:
        return self._snapshot

    async def refresh(self) -> ProxyControlSnapshot:
        async with self._lock:
            return await self._refresh_unlocked()

    async def _refresh_unlocked(self) -> ProxyControlSnapshot:
        try:
            response = await run_query(
                self._sb.from_("pjud_proxy_control")
                .select("provider,status,reason_code,revision")
                .eq("provider", self._provider)
                .limit(1)
            )
            rows = response.data if isinstance(response.data, list) else []
            if len(rows) != 1:
                raise LookupError("proxy control row missing")
            row = rows[0]
            revision = int(row["revision"])
            status = str(row["status"])

            current_revision = self._snapshot.revision
            if current_revision is not None and revision < current_revision:
                logger.warning(
                    "Ignoring stale proxy control revision %d; current revision is %d",
                    revision,
                    current_revision,
                )
                return self._snapshot

            if (
                self._local_billing_trip_revision is not None
                and status == "enabled"
                and revision <= self._local_billing_trip_revision
            ):
                self._snapshot = ProxyControlSnapshot(
                    allowed=False,
                    status="billing_exhausted",
                    reason_code="local_billing_trip_unconfirmed",
                    revision=revision,
                    source="local",
                )
                return self._snapshot

            if (
                self._local_billing_trip_revision is not None
                and revision > self._local_billing_trip_revision
            ):
                self._local_billing_trip_revision = None

            self._snapshot = ProxyControlSnapshot(
                allowed=status == "enabled",
                status=status,
                reason_code=row.get("reason_code"),
                revision=revision,
                source="database",
            )
        except Exception:
            logger.exception("Proxy control read failed")
            self._snapshot = ProxyControlSnapshot(
                allowed=False,
                status="unavailable",
                reason_code="control_read_failed",
                revision=self._snapshot.revision,
                source="local",
            )
        return self._snapshot

    async def trip_billing_exhausted(self) -> ProxyControlSnapshot:
        async with self._lock:
            return await self._trip_billing_exhausted_unlocked()

    async def _trip_billing_exhausted_unlocked(self) -> ProxyControlSnapshot:
        if (
            self._snapshot.status == "billing_exhausted"
            and self._snapshot.source == "database"
        ):
            return self._snapshot

        base_revision = self._snapshot.revision
        self._local_billing_trip_revision = base_revision
        self._snapshot = ProxyControlSnapshot(
            allowed=False,
            status="billing_exhausted",
            reason_code="proxy_balance_exhausted",
            revision=base_revision,
            source="local",
        )

        if base_revision is None:
            logger.error("Proxy billing trip could not persist without a known revision")
            return self._snapshot

        payload = {
            "status": "billing_exhausted",
            "reason_code": "proxy_balance_exhausted",
            "changed_at": datetime.now(timezone.utc).isoformat(),
            "changed_by": self._actor,
            "revision": base_revision + 1,
        }
        try:
            response = await run_query(
                self._sb.from_("pjud_proxy_control")
                .update(payload)
                .eq("provider", self._provider)
                .eq("revision", base_revision)
            )
            rows = response.data if isinstance(response.data, list) else []
            if len(rows) != 1:
                raise RuntimeError("proxy control compare-and-set did not update one row")
            row = rows[0]
            revision = int(row["revision"])
            self._local_billing_trip_revision = revision
            self._snapshot = ProxyControlSnapshot(
                allowed=False,
                status="billing_exhausted",
                reason_code="proxy_balance_exhausted",
                revision=revision,
                source="database",
            )
        except Exception:
            logger.exception("Proxy billing trip persistence failed; keeping local fail-closed state")
        return self._snapshot
