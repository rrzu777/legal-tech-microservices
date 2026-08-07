"""Request-local attribution for paid PJUD proxy operations."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import has_valid_authorization_header

LAW_FIRM_ID_HEADER = "X-JurisTrack-Law-Firm-ID"
CASE_ID_HEADER = "X-JurisTrack-Case-ID"
SYNC_RUN_ID_HEADER = "X-JurisTrack-Sync-Run-ID"

_SCOPE: ContextVar[dict[str, str | None]] = ContextVar(
    "pjud_usage_scope",
    default={"law_firm_id": None, "case_id": None, "sync_run_id": None},
)


def current_usage_scope() -> dict[str, str | None]:
    return dict(_SCOPE.get())


@contextmanager
def usage_scope(*, law_firm_id: str, case_id: str, sync_run_id: str | None = None):
    token = _SCOPE.set({
        "law_firm_id": law_firm_id,
        "case_id": case_id,
        "sync_run_id": sync_run_id,
    })
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _uuid(value: str | None) -> str | None:
    if value is None:
        return None
    return str(UUID(value))


class PjudUsageContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw_firm = headers.get(LAW_FIRM_ID_HEADER)
        raw_case = headers.get(CASE_ID_HEADER)
        raw_run = headers.get(SYNC_RUN_ID_HEADER)
        valid_auth = has_valid_authorization_header(headers.get("Authorization"))
        if not any((raw_firm, raw_case, raw_run)):
            app = scope.get("app")
            proxy_required = bool(
                app is not None and getattr(app.state, "proxy_control_required", False)
            )
            paid_case_path = scope.get("path") in {
                "/api/v1/search",
                "/api/v1/detail",
                "/api/v1/familia/sync",
            }
            if proxy_required and paid_case_path and valid_auth:
                await JSONResponse(
                    {"detail": "Missing PJUD usage attribution"}, status_code=422,
                )(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        try:
            firm_id = _uuid(raw_firm)
            case_id = _uuid(raw_case)
            run_id = _uuid(raw_run)
        except (ValueError, AttributeError):
            if not valid_auth:
                await self.app(scope, receive, send)
                return
            await JSONResponse({"detail": "Invalid PJUD usage attribution"}, status_code=422)(
                scope, receive, send,
            )
            return
        if firm_id is None or case_id is None:
            if not valid_auth:
                await self.app(scope, receive, send)
                return
            await JSONResponse({"detail": "Incomplete PJUD usage attribution"}, status_code=422)(
                scope, receive, send,
            )
            return

        with usage_scope(law_firm_id=firm_id, case_id=case_id, sync_run_id=run_id):
            await self.app(scope, receive, send)
