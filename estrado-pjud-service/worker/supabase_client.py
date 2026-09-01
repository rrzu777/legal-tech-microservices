import httpx
from postgrest import SyncPostgrestClient
from supabase import create_client, Client, ClientOptions
from pydantic import SecretStr

from app.runtime_fence import runtime_generation_headers

from worker.config import WorkerConfig
from worker.trial_scope import PJUD_RUNTIME_TRIAL_CAPABILITY_HEADER

_TRIAL_RPC_BASE_PATH = "/rest/v1"
_TRIAL_RPC_PATH_PREFIX = f"{_TRIAL_RPC_BASE_PATH}/rpc/"
_TRIAL_RPC_ALLOWLIST = frozenset({
    "claim_pjud_trial_import_job",
    "close_pjud_runtime_trial_grant",
    "finalize_pjud_trial_import_discovery",
    "pjud_proxy_finalize_trial_budget_reservation",
    "pjud_proxy_record_trial_usage",
    "pjud_proxy_reserve_trial_budget",
    "renew_pjud_trial_import_job_claim",
    "validate_pjud_trial_import_credential_claim",
})


def _trial_rpc_endpoint(supabase_url: str) -> httpx.URL:
    """Return the one trusted PostgREST base without echoing bad input."""
    if (
        not isinstance(supabase_url, str)
        or any(delimiter in supabase_url for delimiter in ("@", "?", "#"))
    ):
        raise ValueError("pjud_trial_rpc_invalid_endpoint")
    try:
        origin = httpx.URL(supabase_url)
    except (TypeError, httpx.InvalidURL):
        raise ValueError("pjud_trial_rpc_invalid_endpoint") from None
    if (
        origin.scheme != "https"
        or not origin.host
        or origin.userinfo
        or origin.raw_path != b"/"
        or origin.query
        or origin.fragment
    ):
        raise ValueError("pjud_trial_rpc_invalid_endpoint")
    return origin.copy_with(path=_TRIAL_RPC_BASE_PATH)


def _reject_trial_redirect(response: httpx.Response) -> None:
    if 300 <= response.status_code < 400:
        response.close()
        raise RuntimeError("pjud_trial_rpc_redirect_rejected")


class _TrialCapabilityAuth(httpx.Auth):
    """Add trial authority only when the dedicated PostgREST request is sent."""

    def __init__(self, capability: SecretStr, endpoint: httpx.URL):
        self._capability = capability
        self._origin = (endpoint.scheme, endpoint.host, endpoint.port)

    def auth_flow(self, request):
        request_url = request.url
        if (
            request.method != "POST"
            or (request_url.scheme, request_url.host, request_url.port)
            != self._origin
            or request_url.userinfo
            or request_url.query
            or request_url.fragment
            or not request_url.path.startswith(_TRIAL_RPC_PATH_PREFIX)
            or request_url.path == _TRIAL_RPC_PATH_PREFIX
        ):
            raise RuntimeError("pjud_trial_rpc_scope_violation")
        request.headers[PJUD_RUNTIME_TRIAL_CAPABILITY_HEADER] = (
            self._capability.get_secret_value()
        )
        yield request


class TrialRpcClient:
    """Capability-scoped RPC-only client; no auth, table or storage surface."""

    def __init__(
        self,
        *,
        supabase_url: str,
        service_key: str,
        generation: str | None,
        capability: SecretStr,
        transport: httpx.BaseTransport | None = None,
    ):
        endpoint = _trial_rpc_endpoint(supabase_url)
        session = httpx.Client(
            transport=transport,
            follow_redirects=False,
            http2=transport is None,
            event_hooks={"response": [_reject_trial_redirect]},
        )
        headers = {
            **runtime_generation_headers(generation),
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        }
        self._postgrest = SyncPostgrestClient(
            str(endpoint),
            headers=headers,
            http_client=session,
        )
        # postgrest-py passes an explicit per-request ``auth`` argument, so a
        # session default would be bypassed. Keep the secret-bearing auth hook
        # on this RPC-only client and out of every persistent header mapping.
        self._postgrest.basic_auth = _TrialCapabilityAuth(capability, endpoint)

    def rpc(self, name: str, payload: dict):
        if not isinstance(name, str) or name not in _TRIAL_RPC_ALLOWLIST:
            raise RuntimeError("pjud_trial_rpc_scope_violation")
        return self._postgrest.rpc(name, payload)

    def close(self) -> None:
        self._postgrest.session.close()


def _trial_capability(config: WorkerConfig) -> SecretStr:
    capability = getattr(config, "PJUD_IMPORT_TRIAL_CAPABILITY", None)
    if (
        getattr(config, "PJUD_IMPORT_TRIAL_ONCE", False) is not True
        or not isinstance(capability, SecretStr)
    ):
        raise ValueError("pjud_trial_supabase_client_requires_trial")
    return capability


def create_supabase(config: WorkerConfig) -> Client:
    return create_client(
        config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY,
        options=ClientOptions(
            headers=runtime_generation_headers(config.PJUD_RUNTIME_GENERATION),
        ),
    )


def create_trial_supabase(
    config: WorkerConfig,
    *,
    transport: httpx.BaseTransport | None = None,
) -> TrialRpcClient:
    capability = _trial_capability(config)
    return TrialRpcClient(
        supabase_url=config.SUPABASE_URL,
        service_key=config.SUPABASE_SERVICE_KEY,
        generation=config.PJUD_RUNTIME_GENERATION,
        capability=capability,
        transport=transport,
    )
