import httpx
from postgrest import SyncPostgrestClient
from supabase import create_client, Client, ClientOptions
from pydantic import SecretStr

from app.runtime_fence import runtime_generation_headers

from worker.config import WorkerConfig
from worker.trial_scope import PJUD_RUNTIME_TRIAL_CAPABILITY_HEADER


class _TrialCapabilityAuth(httpx.Auth):
    """Add trial authority only when the dedicated PostgREST request is sent."""

    def __init__(self, capability: SecretStr):
        self._capability = capability

    def auth_flow(self, request):
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
        session = httpx.Client(
            transport=transport,
            follow_redirects=True,
            http2=transport is None,
        )
        headers = {
            **runtime_generation_headers(generation),
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        }
        self._postgrest = SyncPostgrestClient(
            f"{supabase_url.rstrip('/')}/rest/v1",
            headers=headers,
            http_client=session,
        )
        # postgrest-py passes an explicit per-request ``auth`` argument, so a
        # session default would be bypassed. Keep the secret-bearing auth hook
        # on this RPC-only client and out of every persistent header mapping.
        self._postgrest.basic_auth = _TrialCapabilityAuth(capability)

    def rpc(self, name: str, payload: dict):
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
