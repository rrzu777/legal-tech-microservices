from supabase import create_client, Client

from worker.config import WorkerConfig


def create_supabase(config: WorkerConfig) -> Client:
    return create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
