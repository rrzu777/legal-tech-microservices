import pytest


TRIAL_CAPABILITY = "a" * 64
TRIAL_GENERATION = "11111111-1111-4111-8111-111111111111"


@pytest.mark.parametrize("value", ["bad", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", " aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"])
def test_runtime_generation_rejects_noncanonical_identity(value):
    from worker.config import WorkerConfig
    with pytest.raises(ValueError, match="pjud_runtime_invalid_generation"):
        WorkerConfig(SUPABASE_URL="https://db.test", SUPABASE_SERVICE_KEY="synthetic", PJUD_RUNTIME_GENERATION=value, _env_file=None)


def test_runtime_generation_legacy_blank_and_explicit_identity():
    from worker.config import WorkerConfig
    config = WorkerConfig(SUPABASE_URL="https://db.test", SUPABASE_SERVICE_KEY="synthetic", PJUD_RUNTIME_GENERATION=" ", _env_file=None)
    assert config.PJUD_RUNTIME_GENERATION is None


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
        assert config.PJUD_IMPORT_TRIAL_ONCE is False
        assert config.PJUD_IMPORT_TRIAL_CAPABILITY is None
        assert config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS is False
        assert config.HEARTBEAT_INTERVAL_S == 60
        assert config.SESSION_MAX_AGE_S == 1500
        assert config.WORKER_SESSION_REUSE_VALIDATION_ENABLED is False
        assert config.SESSION_REUSE_ROLLOUT_STARTED_AT is None
        assert config.SESSION_SOFT_VERIFY_AGE_S == 1200
        assert config.SESSION_HARD_MAX_AGE_S == 3000
        assert config.SESSION_STICKY_SAFETY_MARGIN_S == 600
        assert config.session_hard_effective_age_s == 3000
        assert config.OJV_TIMEOUT_S == 25
        assert config.RATE_LIMIT_MS == 2500
        assert config.MINT_TRAFFIC_BUDGET_S == 35.0

    def test_temporary_outside_office_hours_override_loads_from_env(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
        monkeypatch.setenv("PJUD_PROCESS_OUTSIDE_OFFICE_HOURS", "true")

        from worker.config import WorkerConfig

        assert WorkerConfig(_env_file=None).PJUD_PROCESS_OUTSIDE_OFFICE_HOURS is True

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("", False), ("false", False), ("true", True), (False, False), (True, True)],
    )
    def test_import_trial_once_uses_a_blank_safe_literal_boolean(self, value, expected):
        """Catch permissive truthy parsing or a blank deployment value enabling traffic."""
        from worker.config import WorkerConfig

        config = WorkerConfig(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_SERVICE_KEY="eyJtest",
            PJUD_IMPORT_TRIAL_ONCE=value,
            ENABLE_PJUD_MY_CAUSES_IMPORT=True,
            POOL_SIZE=2,
            **(
                {
                    "PJUD_IMPORT_TRIAL_CAPABILITY": TRIAL_CAPABILITY,
                    "PJUD_RUNTIME_GENERATION": TRIAL_GENERATION,
                }
                if expected
                else {}
            ),
            _env_file=None,
        )

        assert config.PJUD_IMPORT_TRIAL_ONCE is expected

    @pytest.mark.parametrize("value", ["TRUE", "False", "1", "yes", 1])
    def test_import_trial_once_rejects_nonliteral_values(self, value):
        """Catch a typo or permissive coercion silently selecting the production trial."""
        from worker.config import WorkerConfig

        with pytest.raises(ValueError, match="pjud_import_trial_once_flag_must_be_literal"):
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                PJUD_IMPORT_TRIAL_ONCE=value,
                ENABLE_PJUD_MY_CAUSES_IMPORT=True,
                POOL_SIZE=2,
                _env_file=None,
            )

    @pytest.mark.parametrize(
        ("settings", "error_code"),
        [
            (
                {"ENABLE_PJUD_MY_CAUSES_IMPORT": False, "POOL_SIZE": 2},
                "pjud_import_trial_requires_imports",
            ),
            (
                {"ENABLE_PJUD_MY_CAUSES_IMPORT": True, "POOL_SIZE": 1},
                "pjud_import_trial_requires_capacity",
            ),
            (
                {
                    "ENABLE_PJUD_MY_CAUSES_IMPORT": True,
                    "POOL_SIZE": 2,
                    "PJUD_OFF_HOURS_VALIDATION_ONCE": True,
                },
                "pjud_import_trial_incompatible_validation_once",
            ),
        ],
    )
    def test_import_trial_once_rejects_invalid_config_combinations(
        self, settings, error_code,
    ):
        """Catch an impossible one-shot combination before entrypoint startup."""
        from worker.config import WorkerConfig

        with pytest.raises(ValueError, match=error_code):
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                PJUD_IMPORT_TRIAL_ONCE=True,
                PJUD_IMPORT_TRIAL_CAPABILITY=TRIAL_CAPABILITY,
                PJUD_RUNTIME_GENERATION=TRIAL_GENERATION,
                _env_file=None,
                **settings,
            )

    def test_import_trial_capability_is_secret_and_requires_exact_lowercase_hex(self):
        """Catch malformed ambient authority reaching any startup boundary."""
        from worker.config import WorkerConfig

        config = WorkerConfig(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_SERVICE_KEY="eyJtest",
            PJUD_IMPORT_TRIAL_ONCE=True,
            PJUD_IMPORT_TRIAL_CAPABILITY=TRIAL_CAPABILITY,
            PJUD_RUNTIME_GENERATION=TRIAL_GENERATION,
            ENABLE_PJUD_MY_CAUSES_IMPORT=True,
            POOL_SIZE=2,
            _env_file=None,
        )

        assert config.PJUD_IMPORT_TRIAL_CAPABILITY.get_secret_value() == TRIAL_CAPABILITY
        assert TRIAL_CAPABILITY not in repr(config)
        assert "**********" in repr(config.PJUD_IMPORT_TRIAL_CAPABILITY)

        malformed_values = [
            "a" * 63,
            "a" * 65,
            "A" * 64,
            "g" * 64,
            " " + ("a" * 64),
        ]
        for malformed in malformed_values:
            with pytest.raises(
                ValueError,
                match="pjud_import_trial_capability_must_be_64_lowercase_hex",
            ) as exc_info:
                WorkerConfig(
                    SUPABASE_URL="https://test.supabase.co",
                    SUPABASE_SERVICE_KEY="eyJtest",
                    PJUD_IMPORT_TRIAL_ONCE=True,
                    PJUD_IMPORT_TRIAL_CAPABILITY=malformed,
                    PJUD_RUNTIME_GENERATION=TRIAL_GENERATION,
                    ENABLE_PJUD_MY_CAUSES_IMPORT=True,
                    POOL_SIZE=2,
                    _env_file=None,
                )
            if malformed:
                assert malformed not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("settings", "error_code"),
        [
            ({}, "pjud_import_trial_requires_capability"),
            (
                {"PJUD_IMPORT_TRIAL_CAPABILITY": TRIAL_CAPABILITY},
                "pjud_import_trial_requires_generation",
            ),
        ],
    )
    def test_import_trial_requires_capability_and_generation(self, settings, error_code):
        """Catch a one-shot that cannot be bound to durable runtime authority."""
        from worker.config import WorkerConfig

        with pytest.raises(ValueError, match=error_code):
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                PJUD_IMPORT_TRIAL_ONCE=True,
                ENABLE_PJUD_MY_CAUSES_IMPORT=True,
                POOL_SIZE=2,
                _env_file=None,
                **settings,
            )

    def test_import_trial_capability_is_rejected_outside_trial_mode(self):
        """Catch a normal worker silently retaining trial authority."""
        from worker.config import WorkerConfig

        with pytest.raises(
            ValueError,
            match="pjud_import_trial_capability_requires_trial_mode",
        ):
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                PJUD_IMPORT_TRIAL_CAPABILITY=TRIAL_CAPABILITY,
                _env_file=None,
            )

    def test_import_trial_capability_is_fully_hidden_in_config_diagnostics(self):
        """Pydantic must not retain even a distinctive capability fragment."""
        from worker.config import WorkerConfig

        capability = "0123456789abcdef" * 4
        with pytest.raises(ValueError) as exc_info:
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                PJUD_IMPORT_TRIAL_CAPABILITY=capability,
                _env_file=None,
            )

        rendered = str(exc_info.value)
        assert capability not in rendered
        assert capability[:16] not in rendered
        assert capability[-16:] not in rendered

    @pytest.mark.parametrize(
        "settings",
        [
            {
                "PJUD_IMPORT_TRIAL_ONCE": True,
                "PJUD_IMPORT_TRIAL_CAPABILITY": "sentinel-capability-not-hex",
                "PJUD_RUNTIME_GENERATION": TRIAL_GENERATION,
                "ENABLE_PJUD_MY_CAUSES_IMPORT": True,
                "POOL_SIZE": 2,
            },
            {
                "PJUD_IMPORT_TRIAL_CAPABILITY": "0123456789abcdef" * 4,
            },
        ],
    )
    def test_import_trial_capability_is_hidden_from_structured_validation_errors(
        self, settings,
    ):
        """Structured collectors must not recover authority from Pydantic input."""
        from pydantic import SecretStr, ValidationError
        from worker.config import WorkerConfig

        sentinel = settings["PJUD_IMPORT_TRIAL_CAPABILITY"]
        recovered: list[str] = []
        seen: set[int] = set()

        def collect(value):
            marker = id(value)
            if marker in seen:
                return
            seen.add(marker)
            if isinstance(value, SecretStr):
                recovered.append(value.get_secret_value())
                return
            if isinstance(value, str):
                recovered.append(value)
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    collect(key)
                    collect(item)
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    collect(item)
                return
            values = getattr(value, "__dict__", None)
            if isinstance(values, dict):
                collect(values)

        if sentinel == "sentinel-capability-not-hex":
            with pytest.raises(ValidationError) as exc_info:
                WorkerConfig(
                    SUPABASE_URL="https://test.supabase.co",
                    SUPABASE_SERVICE_KEY="eyJtest",
                    _env_file=None,
                    **settings,
                )
            assert sentinel not in repr(exc_info.value.args)
        else:
            # Force an unrelated model-level Pydantic error while valid trial
            # authority is present. This is the path that previously retained
            # the SecretStr object in ``errors()[...]['input']``.
            with pytest.raises(ValidationError) as exc_info:
                WorkerConfig(
                    SUPABASE_URL="https://test.supabase.co",
                    SUPABASE_SERVICE_KEY="eyJtest",
                    PJUD_IMPORT_TRIAL_ONCE=True,
                    PJUD_RUNTIME_GENERATION=TRIAL_GENERATION,
                    ENABLE_PJUD_MY_CAUSES_IMPORT=True,
                    POOL_SIZE=2,
                    SESSION_SOFT_VERIFY_AGE_S=3000,
                    SESSION_HARD_MAX_AGE_S=3000,
                    _env_file=None,
                    **settings,
                )

        collect(exc_info.value.errors())
        assert sentinel not in recovered
        assert all(sentinel not in value for value in recovered)

    def test_empty_import_trial_capability_is_unset_for_normal_worker(self):
        from worker.config import WorkerConfig

        config = WorkerConfig(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_SERVICE_KEY="eyJtest",
            PJUD_IMPORT_TRIAL_ONCE="false",
            PJUD_IMPORT_TRIAL_CAPABILITY="",
            _env_file=None,
        )

        assert config.PJUD_IMPORT_TRIAL_ONCE is False
        assert config.PJUD_IMPORT_TRIAL_CAPABILITY is None

    def test_trial_rpc_client_state_repr_never_contains_capability(self):
        from worker.config import WorkerConfig
        from worker.supabase_client import create_trial_supabase

        config = WorkerConfig(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_SERVICE_KEY="eyJtest",
            PJUD_IMPORT_TRIAL_ONCE=True,
            PJUD_IMPORT_TRIAL_CAPABILITY=TRIAL_CAPABILITY,
            PJUD_RUNTIME_GENERATION=TRIAL_GENERATION,
            ENABLE_PJUD_MY_CAUSES_IMPORT=True,
            POOL_SIZE=2,
            _env_file=None,
        )

        client = create_trial_supabase(config)
        assert TRIAL_CAPABILITY not in repr(config)
        assert TRIAL_CAPABILITY not in repr(client)
        assert TRIAL_CAPABILITY not in repr(vars(client))
        assert TRIAL_CAPABILITY not in repr(client._postgrest)
        assert TRIAL_CAPABILITY not in repr(client._postgrest.headers)
        assert TRIAL_CAPABILITY not in repr(client._postgrest.session)
        assert TRIAL_CAPABILITY not in repr(client._postgrest.session.headers)
        assert not hasattr(client, "storage")
        assert not hasattr(client, "auth")

    def test_reuse_canary_requires_an_authoritative_utc_cutoff(self):
        from worker.config import WorkerConfig

        with pytest.raises(ValueError, match="SESSION_REUSE_ROLLOUT_STARTED_AT"):
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                WORKER_SESSION_REUSE_VALIDATION_ENABLED=True,
                _env_file=None,
            )

        with pytest.raises(ValueError, match="timezone-aware"):
            WorkerConfig(
                SUPABASE_URL="https://test.supabase.co",
                SUPABASE_SERVICE_KEY="eyJtest",
                WORKER_SESSION_REUSE_VALIDATION_ENABLED=True,
                SESSION_REUSE_ROLLOUT_STARTED_AT="2026-08-18T08:00:00",
                _env_file=None,
            )

        config = WorkerConfig(
            SUPABASE_URL="https://test.supabase.co",
            SUPABASE_SERVICE_KEY="eyJtest",
            WORKER_SESSION_REUSE_VALIDATION_ENABLED=True,
            SESSION_REUSE_ROLLOUT_STARTED_AT="2026-08-18T12:00:00Z",
            _env_file=None,
        )

        assert config.SESSION_REUSE_ROLLOUT_STARTED_AT.isoformat() == (
            "2026-08-18T12:00:00+00:00"
        )

    def test_empty_rollout_cutoff_is_unset_while_canary_is_off(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "eyJtest")
        monkeypatch.setenv("WORKER_SESSION_REUSE_VALIDATION_ENABLED", "false")
        monkeypatch.setenv("SESSION_REUSE_ROLLOUT_STARTED_AT", "")

        from worker.config import WorkerConfig

        assert WorkerConfig(_env_file=None).SESSION_REUSE_ROLLOUT_STARTED_AT is None

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
