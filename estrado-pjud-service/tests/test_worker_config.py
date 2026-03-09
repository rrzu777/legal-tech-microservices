import os
import pytest


class TestWorkerConfig:
    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
        monkeypatch.setenv("WORKER_ID", "test-worker-1")
        monkeypatch.setenv("POOL_SIZE", "2")
        monkeypatch.setenv("PJUD_BASE_URL", "https://ojv.pjud.cl")

        from worker.config import WorkerConfig
        config = WorkerConfig()

        assert config.SUPABASE_URL == "https://test.supabase.co"
        assert config.SUPABASE_SERVICE_KEY == "eyJtest"
        assert config.WORKER_ID == "test-worker-1"
        assert config.POOL_SIZE == 2
        assert config.PJUD_BASE_URL == "https://ojv.pjud.cl"

    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")

        from worker.config import WorkerConfig
        config = WorkerConfig()

        assert config.WORKER_ID == "worker-1"
        assert config.POOL_SIZE == 1
        assert config.BATCH_SIZE == 10
        assert config.HEARTBEAT_INTERVAL_S == 60
        assert config.SESSION_MAX_AGE_S == 1500
        assert config.OJV_TIMEOUT_S == 25
        assert config.RATE_LIMIT_MS == 2500
