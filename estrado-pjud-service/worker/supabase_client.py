from supabase import create_client, Client, ClientOptions

from app.runtime_fence import runtime_generation_headers

from worker.config import WorkerConfig


def create_supabase(config: WorkerConfig) -> Client:
    return create_client(
        config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY,
        options=ClientOptions(headers=runtime_generation_headers(config.PJUD_RUNTIME_GENERATION)),
    )
