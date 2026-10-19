"""Authenticated detail for a selected, leased Mis Causas import candidate."""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request

from app.auth import verify_api_key
from app.config import get_settings
from app.failure_kind import MintUnavailableError, PoolUnavailableError
from app.my_causes.private_client import resolve_authenticated_detail
from app.my_causes.private_fence import ImportDetailFence, fetch_import_credential
from app.my_causes.private_models import (
    AuthenticatedImportError, AuthenticatedImportRequest,
    AuthenticatedImportResolution, AuthenticatedImportResponse,
)
from app.ojv.errors import OjvSessionError
from app.ojv.session import OjvSession
from app.rate_limit import limiter
from app.runtime_fence import PjudRuntimeError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/my-causes", tags=["authenticated-import"])


async def resolve_import(req: AuthenticatedImportRequest, state) -> AuthenticatedImportResponse:
    settings = get_settings()
    fence = ImportDetailFence(req, state)
    candidate = await fence.check()
    credential = await fetch_import_credential(req, settings)
    operation_id = uuid4()

    @asynccontextmanager
    async def request_scope(operation: str, key: str):
        await fence.check()
        await state.session_pool.assert_proxy_enabled()
        async with state.proxy_usage.track(
            operation=operation, law_firm_id=str(req.law_firm_id),
            transaction_key=f"authenticated-import:{operation_id}:{key}",
        ):
            yield
        await fence.check()

    try:
        bundle = await state.session_pool.acquire_familia_bundle()
        if bundle is None:
            raise AuthenticatedImportError("pjud_unavailable")
        async with OjvSession(
            bundle.proxy_url, bundle.cookies, bundle.user_agent,
            rate_limit_s=settings.RATE_LIMIT_MS / 1000,
        ) as session:
            async with request_scope("other", "login"):
                await session.login(*credential, "clave_pj")
            credential = None
            resolved, detail = await resolve_authenticated_detail(session, candidate, request_scope)
            await fence.check()
            return AuthenticatedImportResponse(
                ok=True, error_code=None,
                resolution=AuthenticatedImportResolution(
                    candidate=resolved, detail=detail,
                    credential_binding_version=req.credential_binding_version,
                ),
            )
    finally:
        credential = None


@router.post("/resolve-private", response_model=AuthenticatedImportResponse)
@limiter.limit("2/minute")
async def authenticated_import_detail(
    request: Request, req: AuthenticatedImportRequest,
    _api_key: str = verify_api_key,
) -> AuthenticatedImportResponse:
    error_code = "upstream_changed"
    try:
        async with asyncio.timeout(160):
            async with request.app.state.private_resolution_budget.slot():
                return await resolve_import(req, request.app.state)
    except PjudRuntimeError:
        raise
    except AuthenticatedImportError as error:
        error_code = error.code
    except OjvSessionError as error:
        error_code = error.code.value
    except TimeoutError:
        error_code = "timeout"
    except (MintUnavailableError, PoolUnavailableError):
        error_code = "pjud_unavailable"
    except Exception as error:
        # No exception string/traceback: provider errors can retain request data.
        logger.warning("authenticated_import_failed type=%s", type(error).__name__)
    allowed = {
        "private_not_found", "private_ambiguous", "private_identifier_mismatch",
        "private_tribunal_mismatch", "private_evidence_incomplete", "private_fence_unavailable",
        "credential_invalid", "session_expired", "waf", "timeout", "upstream_changed", "pjud_unavailable",
    }
    if error_code not in allowed:
        error_code = "upstream_changed"
    return AuthenticatedImportResponse(ok=False, resolution=None, error_code=error_code)
