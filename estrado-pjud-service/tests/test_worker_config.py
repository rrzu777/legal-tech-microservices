class TestWorkerConfig:
    def test_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
        monkeypatch.setenv("WORKER_ID", "test-worker-1")
        monkeypatch.setenv("POOL_SIZE", "2")
        monkeypatch.setenv("PJUD_BASE_URL", "https://ojv.pjud.cl")

        from worker.config import WorkerConfig
        config = WorkerConfig(_env_file=None)

        assert config.SUPABASE_URL == "https://test.supabase.co"
        assert config.SUPABASE_SERVICE_KEY == "eyJtest"
        assert config.WORKER_ID == "test-worker-1"
        assert config.POOL_SIZE == 2
        assert config.PJUD_BASE_URL == "https://ojv.pjud.cl"

    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")

        from worker.config import WorkerConfig
        config = WorkerConfig(_env_file=None)

        assert config.WORKER_ID == "worker-1"
        assert config.POOL_SIZE == 1
        assert config.BATCH_SIZE == 10
        assert config.PJUD_OFF_HOURS_VALIDATION_ONCE is False
        assert config.HEARTBEAT_INTERVAL_S == 60
        assert config.SESSION_MAX_AGE_S == 1500
        assert config.WORKER_SESSION_REUSE_VALIDATION_ENABLED is False
        assert config.SESSION_SOFT_VERIFY_AGE_S == 1200
        assert config.SESSION_HARD_MAX_AGE_S == 3000
        assert config.SESSION_STICKY_SAFETY_MARGIN_S == 600
        assert config.session_hard_effective_age_s == 3000
        assert config.OJV_TIMEOUT_S == 25
        assert config.RATE_LIMIT_MS == 2500
        assert config.MINT_TRAFFIC_BUDGET_S == 35.0

    def test_hard_age_is_capped_before_one_hour_sticky_expires(self):
        from worker.config import WorkerConfig

        config = WorkerConfig(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_SERVICE_KEY="eyJtest",
            SESSION_SOFT_VERIFY_AGE_S=1200,
            SESSION_HARD_MAX_AGE_S=3300,
            SESSION_STICKY_SAFETY_MARGIN_S=600,
            OJV_PROXY_STICKY_LIFETIME="1h",
            _env_file=None,
        )

        assert config.session_hard_effective_age_s == 3000

    def test_rejects_soft_age_at_or_beyond_effective_hard_age(self):
        import pytest

        from worker.config import WorkerConfig

        with pytest.raises(ValueError, match="SESSION_SOFT_VERIFY_AGE_S"):
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                SESSION_SOFT_VERIFY_AGE_S=1200,
                SESSION_HARD_MAX_AGE_S=3000,
                SESSION_STICKY_SAFETY_MARGIN_S=600,
                OJV_PROXY_STICKY_LIFETIME="30m",
                _env_file=None,
            )

    def test_mint_traffic_budget_is_configurable_and_bounded(self, monkeypatch):
        import pytest

        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
        monkeypatch.setenv("MINT_TRAFFIC_BUDGET_S", "42")

        from worker.config import WorkerConfig

        assert WorkerConfig(_env_file=None).MINT_TRAFFIC_BUDGET_S == 42.0

        monkeypatch.setenv("MINT_TRAFFIC_BUDGET_S", "61")
        with pytest.raises(ValueError):
            WorkerConfig(_env_file=None)

    def test_rejects_cookie_store_inside_production_checkout(self, monkeypatch):
        import pytest

        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
        monkeypatch.setenv(
            "COOKIE_STORE_PATH",
            "/opt/legal-tech-microservices/estrado-pjud-service/.cookies.json",
        )

        from worker.config import WorkerConfig

        with pytest.raises(ValueError, match="outside the git checkout"):
            WorkerConfig(_env_file=None)
