import os
import pytest


@pytest.mark.parametrize("value", [
    "bad",
    "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
    " aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "aaaaaaaa-aaaa-1aaa-8aaa-aaaaaaaaaaaa",
    "aaaaaaaa-aaaa-4aaa-7aaa-aaaaaaaaaaaa",
])
def test_runtime_generation_rejects_noncanonical_identity(value):
    from app.config import Settings
    with pytest.raises(ValueError, match="pjud_runtime_invalid_generation"):
        Settings(API_KEY="synthetic", PJUD_RUNTIME_GENERATION=value, _env_file=None)


def test_runtime_generation_legacy_blank_and_explicit_identity():
    from app.config import Settings
    assert Settings(API_KEY="synthetic", PJUD_RUNTIME_GENERATION=" ", _env_file=None).PJUD_RUNTIME_GENERATION is None
    assert Settings(API_KEY="synthetic", PJUD_RUNTIME_GENERATION="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", _env_file=None).PJUD_RUNTIME_GENERATION == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key-123")
    monkeypatch.setenv("OJV_BASE_URL", "https://example.com")
    monkeypatch.setenv("RATE_LIMIT_MS", "3000")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    from app.config import Settings
    s = Settings()

    assert s.API_KEY == "test-key-123"
    assert s.OJV_BASE_URL == "https://example.com"
    assert s.RATE_LIMIT_MS == 3000
    assert s.LOG_LEVEL == "DEBUG"


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("API_KEY", "key")

    from app.config import Settings
    s = Settings(_env_file=None)

    assert s.OJV_BASE_URL == "https://oficinajudicialvirtual.pjud.cl"
    assert s.RATE_LIMIT_MS == 2500
    assert s.LOG_LEVEL == "INFO"
    assert s.SESSION_POOL_SIZE == 2
    assert s.SESSION_MAX_AGE_S == 1200


@pytest.mark.parametrize("obsolete_env", [
    "PJUD_CATALOG_OPPORTUNISTIC_ENABLED",
    "PJUD_CATALOG_QUEUE_SIZE",
    "PJUD_CATALOG_LEASE_SECONDS",
    "PJUD_CATALOG_COOLDOWN_SECONDS",
])
def test_obsolete_catalog_refresh_env_cannot_configure_runtime(
    monkeypatch, obsolete_env,
):
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv(obsolete_env, "1")

    from app.config import Settings

    settings = Settings(_env_file=None)

    assert not hasattr(settings, obsolete_env)


def test_config_rejects_cookie_store_inside_production_checkout(monkeypatch):
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv(
        "COOKIE_STORE_PATH",
        "/opt/legal-tech-microservices/estrado-pjud-service/.cookies.json",
    )

    from app.config import Settings

    with pytest.raises(ValueError, match="outside the git checkout"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        ".cookies.json",
        "cookies.json",
        "/var/lib/../../opt/legal-tech-microservices/estrado-pjud-service/.cookies.json",
    ],
)
def test_config_rejects_relative_or_normalized_checkout_path(monkeypatch, unsafe_path):
    monkeypatch.setenv("API_KEY", "key")
    monkeypatch.setenv("COOKIE_STORE_PATH", unsafe_path)

    from app.config import Settings

    with pytest.raises(ValueError, match="absolute and outside the git checkout"):
        Settings(_env_file=None)


def test_telegram_config_defaults(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings, Settings
    get_settings.cache_clear()
    settings = Settings(API_KEY="test", _env_file=None)
    assert settings.TELEGRAM_BOT_TOKEN == ""
    assert settings.TELEGRAM_CHAT_ID == ""
    assert settings.TELEGRAM_BLOCKED_RATE_THRESHOLD == 0.3
    assert settings.TELEGRAM_COOLDOWN_S == 300


# --- Canal interno de credenciales: TLS exigido en la config, no solo por request ---
#
# Las dos configs comparten el tipo porque las dos alimentan llamadas al MISMO
# endpoint interno que devuelve la contraseña PJUD en claro. Dejar el chequeo
# solo en el punto de uso lo degradaba a `infra_unavailable`, el mismo balde que
# una caída del PJUD: un error de configuración nuestro disfrazado de outage del
# Poder Judicial. El porqué completo está en app/internal_url.py.
#
# `worker.config:WorkerConfig` NO está en la lista de abajo, y no es un olvido:
# ese archivo es uno de los 17 de `wm_contract_paths` en ops/worker-maintenance.sh.
# `ops/deploy.sh` compara sus bytes contra la revisión entrante y aborta si
# difieren, y el bootstrap que instalaría otro contrato está documentado como no
# implementado (ops/worker-maintenance.md). Así que el fail-fast de arranque del
# worker viaja en una operación autorizada aparte.
#
# Esto NO deja el canal del worker sin protección ni sin cobertura: el rechazo
# real está en el punto de uso, en worker/sync_credentials.py, y lo afirma
# test_sync_credentials.py. Lo diferido es que falle al arrancar en vez de al
# primer pedido. Cuando ese contrato se despliegue, agregar
# "worker.config:WorkerConfig" acá y borrar este párrafo.

TLS_CHECKED_CONFIGS = ["app.config:Settings"]

REQUIRED_CONFIG_FIELDS = {
    "API_KEY": "synthetic",
    "WORKER_ID": "synthetic",
    "SUPABASE_URL": "https://fixture.supabase.co",
    "SUPABASE_SERVICE_KEY": "synthetic",
}


@pytest.mark.parametrize("config_name", TLS_CHECKED_CONFIGS)
@pytest.mark.parametrize("url", [
    "http://app.test",
    "http://127.0.0.1:3000",
    "HTTPS://app.test",
    "//app.test",
    "app.test",
    "ftp://app.test",
])
def test_vercel_app_url_sin_tls_no_arranca(config_name, url):
    module_name, attribute = config_name.split(":")
    import importlib
    config_class = getattr(importlib.import_module(module_name), attribute)
    with pytest.raises(ValueError, match="internal_base_url_requires_https"):
        config_class(**REQUIRED_CONFIG_FIELDS, VERCEL_APP_URL=url, _env_file=None)


@pytest.mark.parametrize("config_name", TLS_CHECKED_CONFIGS)
@pytest.mark.parametrize("url", ["", "https://app.test", "https://juristrack.cl/"])
def test_vercel_app_url_vacio_o_con_tls_es_valido(config_name, url):
    # Vacío tiene que seguir siendo válido: es el estado de un despliegue que no
    # usa el canal privado, y hacerlo obligatorio rompería ese arranque.
    module_name, attribute = config_name.split(":")
    import importlib
    config_class = getattr(importlib.import_module(module_name), attribute)
    config = config_class(**REQUIRED_CONFIG_FIELDS, VERCEL_APP_URL=url, _env_file=None)
    assert config.VERCEL_APP_URL == url
