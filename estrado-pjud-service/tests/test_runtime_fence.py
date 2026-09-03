"""Closed protocol, fixed identity and admission boundaries (no external I/O)."""
import asyncio
import httpx
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.runtime_fence import (
    PjudRuntimeError, RuntimeFence, runtime_generation_headers,
    validate_runtime_generation,
)
from tests.helpers import GENERATION_A, GENERATION_B, RuntimeControlDB, runtime_control


@pytest.mark.asyncio
async def test_old_process_does_not_adopt_new_control_generation():
    db = RuntimeControlDB(runtime_control(generation=GENERATION_B))
    fence = RuntimeFence(db, GENERATION_A)
    with pytest.raises(PjudRuntimeError, match="^pjud_runtime_generation_mismatch$"):
        await fence.require()
    assert fence.generation == GENERATION_A
    with pytest.raises(AttributeError):
        fence.generation = GENERATION_B


@pytest.mark.parametrize("value", [
    GENERATION_A.upper(),
    " " + GENERATION_A,
    GENERATION_A + " ",
    "11111111-1111-1111-8111-111111111111",
    "11111111-1111-4111-7111-111111111111",
    "bad",
    1,
    True,
])
def test_generation_rejects_noncanonical_nonblank(value):
    with pytest.raises(ValueError, match="^pjud_runtime_invalid_generation$"):
        validate_runtime_generation(value)


@pytest.mark.parametrize("value", [None, "", "  \t"])
def test_blank_generation_is_legacy(value):
    assert validate_runtime_generation(value) is None
    assert runtime_generation_headers(value) == {}


def test_headers_are_independent_copies():
    one = runtime_generation_headers(GENERATION_A)
    two = runtime_generation_headers(GENERATION_B)
    one.clear()
    assert two == {"x-pjud-runtime-generation": GENERATION_B}
    assert runtime_generation_headers(GENERATION_A) == {"x-pjud-runtime-generation": GENERATION_A}


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("protocol_version", True), ("protocol_version", 2), ("protocol_version", "1"),
    ("revision", True), ("revision", -1), ("revision", 9007199254740992), ("revision", 1.0),
    ("admission_paused", 0), ("generation_required", "false"),
    ("generation", GENERATION_A), ("sealed_at", "2026-08-31T12:00:00Z"), ("bindings", {}),
    ("extra", "secret must not escape"),
])
async def test_closed_legacy_shape_rejects_invalid_values(field, value):
    control = runtime_control()
    control[field] = value
    with pytest.raises(PjudRuntimeError, match="^pjud_runtime_unavailable$"):
        await RuntimeFence(RuntimeControlDB(control), None).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", list(runtime_control()))
async def test_missing_fields_fail_closed(field):
    control = runtime_control()
    del control[field]
    with pytest.raises(PjudRuntimeError, match="^pjud_runtime_unavailable$"):
        await RuntimeFence(RuntimeControlDB(control), None).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("generation", None), ("generation", GENERATION_A.upper()),
    ("sealed_at", None), ("sealed_at", "2026-08-31T12:00:00"),
    ("sealed_at", "2026-13-31T12:00:00Z"), ("sealed_at", 0),
    ("bindings", None), ("bindings", {}),
    ("bindings", dict.fromkeys(("micro_sha", "web_sha", "rollback_micro_sha", "rollback_web_sha"), "A" * 40)),
])
async def test_strict_control_requires_valid_seal(field, value):
    control = runtime_control(generation=GENERATION_A)
    control[field] = value
    with pytest.raises(PjudRuntimeError, match="^pjud_runtime_unavailable$"):
        await RuntimeFence(RuntimeControlDB(control), GENERATION_A).snapshot()


@pytest.mark.asyncio
async def test_snapshot_immutable_and_pause_is_not_cached():
    db = RuntimeControlDB(runtime_control(generation=GENERATION_A))
    fence = RuntimeFence(db, GENERATION_A)
    snap = await fence.require_origin([GENERATION_A])
    with pytest.raises((AttributeError, FrozenInstanceError)):
        snap.admission_paused = True
    with pytest.raises(TypeError):
        snap.bindings["micro_sha"] = "b" * 40
    db.control["admission_paused"] = True
    with pytest.raises(PjudRuntimeError, match="^pjud_admission_paused$"):
        await fence.require()
    assert (await fence.require(admission=False)).admission_paused
    db.control["admission_paused"] = False
    await fence.require()
    assert len(db.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [[], [GENERATION_B], [GENERATION_A, GENERATION_A], [GENERATION_A + "," + GENERATION_A], [GENERATION_A.upper()]])
async def test_origin_cannot_be_laundered(values):
    fence = RuntimeFence(RuntimeControlDB(runtime_control(generation=GENERATION_A)), GENERATION_A)
    with pytest.raises(PjudRuntimeError, match="^pjud_runtime_generation_mismatch$"):
        await fence.require_origin(values)


@pytest.mark.asyncio
async def test_legacy_missing_origin_allowed_and_stale_precedes_pause():
    await RuntimeFence(RuntimeControlDB(), None).require_origin([])
    fence = RuntimeFence(RuntimeControlDB(runtime_control(generation=GENERATION_B, paused=True)), GENERATION_A)
    with pytest.raises(PjudRuntimeError, match="^pjud_runtime_generation_mismatch$"):
        await fence.require()


@pytest.mark.asyncio
async def test_unavailable_rpc_and_missing_client_are_finite():
    db = RuntimeControlDB()
    db.error = RuntimeError("private raw response body")
    for source in (None, db):
        with pytest.raises(PjudRuntimeError, match="^pjud_runtime_unavailable$"):
            await RuntimeFence(source, None).require()


@pytest.mark.asyncio
async def test_rpc_wait_is_bounded_and_cancellation_propagates(monkeypatch):
    import app.runtime_fence as module
    waits = []
    async def timeout(awaitable, *, timeout):
        waits.append(timeout)
        awaitable.close()
        raise asyncio.TimeoutError()
    monkeypatch.setattr(module.asyncio, "wait_for", timeout)
    with pytest.raises(PjudRuntimeError, match="^pjud_runtime_unavailable$"):
        await RuntimeFence(RuntimeControlDB(), None).require()
    assert waits == [5.0]
    async def cancelled(awaitable, *, timeout):
        awaitable.close()
        raise asyncio.CancelledError()
    monkeypatch.setattr(module.asyncio, "wait_for", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await RuntimeFence(RuntimeControlDB(), None).require()


@pytest.mark.asyncio
async def test_case_waiting_for_capacity_rechecks_pause_without_case_failure():
    from worker.__main__ import process_batch
    db = RuntimeControlDB(runtime_control(generation=GENERATION_A))
    engine = MagicMock()
    admitted = []
    async def sync(case):
        admitted.append(case["id"])
        db.control["admission_paused"] = True
    engine.sync_case = AsyncMock(side_effect=sync)
    await process_batch(
        [{"id": "first"}, {"id": "queued"}], engine, 1,
        asyncio.Event(), MagicMock(is_open=False), processing_window=lambda: True,
        runtime_fence=RuntimeFence(db, GENERATION_A),
    )
    assert admitted == ["first"]
    assert len(db.calls) == 2
    engine._metrics.record_error.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_rechecks_pause_on_next_iteration(monkeypatch):
    from worker.__main__ import run_import_discovery_loop
    db = RuntimeControlDB()
    shutdown = asyncio.Event()
    admitted = []
    async def process():
        admitted.append("import")
        db.control["admission_paused"] = True
        asyncio.get_running_loop().call_later(0.02, shutdown.set)
        return True
    metrics = MagicMock()
    await run_import_discovery_loop(
        SimpleNamespace(process_import_job=process), metrics, shutdown,
        runtime_fence=RuntimeFence(db, None), poll_interval=0.001,
    )
    assert admitted == ["import"]
    assert len(db.calls) >= 2
    metrics.record_error.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", [runtime_control(paused=True), runtime_control(generation=GENERATION_B), {"bad": "control"}])
async def test_startup_denial_precedes_reconciliation_and_prewarm(
    monkeypatch, worker_maintenance, control,
):
    from worker import __main__ as worker_main
    from worker.proxy_control import ProxyControlSnapshot
    from tests.test_worker_startup import _entrypoint_config, _patch_entrypoint
    config = _entrypoint_config(validation_once=True)
    config.PJUD_RUNTIME_GENERATION = GENERATION_A
    scheduler = AsyncMock()
    scheduler.get_next_batch.return_value = []
    pool = MagicMock(initialize=AsyncMock(), close_all=AsyncMock())
    metrics = MagicMock(stop=AsyncMock())
    _patch_entrypoint(monkeypatch, worker_main, config=config, scheduler=scheduler,
                      pool=pool, metrics=metrics, backoff=MagicMock(is_open=False),
                      maintenance=worker_maintenance)
    monkeypatch.setattr(worker_main, "create_supabase", lambda _: RuntimeControlDB(control))
    engine_factory = MagicMock(return_value=MagicMock(drain_work=AsyncMock()))
    monkeypatch.setattr(worker_main, "SyncEngine", engine_factory)
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", AsyncMock(return_value=ProxyControlSnapshot(
        allowed=True, status="enabled", reason_code=None, revision=1, source="database")))
    alert = AsyncMock()
    monkeypatch.setattr(worker_main, "send_ops_alert", alert)
    await worker_main.main()
    scheduler.reconcile_stale_runs.assert_not_awaited()
    scheduler.verify_claim_contract.assert_not_awaited()
    scheduler.get_next_batch.assert_not_awaited()
    pool.initialize.assert_not_awaited()
    engine_factory.assert_not_called()
    metrics.record_error.assert_not_called()
    alert.assert_not_awaited()


def test_supabase_client_headers_are_fixed_and_isolated(monkeypatch):
    from worker import supabase_client
    constructed = []
    def create(url, key, *, options):
        client = SimpleNamespace(options=options)
        constructed.append(client)
        return client
    monkeypatch.setattr(supabase_client, "create_client", create)
    config = SimpleNamespace(SUPABASE_URL="https://db.test", SUPABASE_SERVICE_KEY="placeholder", PJUD_RUNTIME_GENERATION=GENERATION_A)
    one = supabase_client.create_supabase(config)
    config.PJUD_RUNTIME_GENERATION = GENERATION_B
    two = supabase_client.create_supabase(config)
    assert one.options.headers["x-pjud-runtime-generation"] == GENERATION_A
    assert two.options.headers["x-pjud-runtime-generation"] == GENERATION_B
    assert one.options.headers is not two.options.headers


def test_trial_supabase_client_is_dedicated_and_normal_headers_never_gain_capability():
    """Catch trial authority leaking to fence, table reads or generic RPCs."""
    from worker import supabase_client

    capability = "b" * 64
    normal = SimpleNamespace(
        SUPABASE_URL="https://db.test",
        SUPABASE_SERVICE_KEY="placeholder",
        PJUD_RUNTIME_GENERATION=GENERATION_A,
        PJUD_IMPORT_TRIAL_ONCE=False,
        PJUD_IMPORT_TRIAL_CAPABILITY=None,
    )
    trial = SimpleNamespace(
        SUPABASE_URL="https://db.test",
        SUPABASE_SERVICE_KEY="placeholder",
        PJUD_RUNTIME_GENERATION=GENERATION_A,
        PJUD_IMPORT_TRIAL_ONCE=True,
        PJUD_IMPORT_TRIAL_CAPABILITY=SecretStr(capability),
    )

    normal_client = supabase_client.create_supabase(normal)
    normal_from_trial_config = supabase_client.create_supabase(trial)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "empty"})

    trial_client = supabase_client.create_trial_supabase(
        trial, transport=httpx.MockTransport(respond),
    )

    assert normal_client.options.headers["x-pjud-runtime-generation"] == GENERATION_A
    assert (
        normal_from_trial_config.options.headers["x-pjud-runtime-generation"]
        == GENERATION_A
    )
    response = trial_client.rpc("claim_pjud_trial_import_job", {}).execute()
    assert response.data == {"status": "empty"}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == (
        "https://db.test/rest/v1/rpc/claim_pjud_trial_import_job"
    )
    assert requests[0].headers["x-pjud-runtime-trial-capability"] == capability
    assert requests[0].headers["x-pjud-runtime-generation"] == GENERATION_A

    for headers in (
        normal_client.options.headers,
        normal_from_trial_config.options.headers,
        trial_client._postgrest.headers,
        trial_client._postgrest.session.headers,
    ):
        assert "x-pjud-runtime-trial-capability" not in headers
        assert capability not in repr(headers)
    assert not hasattr(trial_client, "storage")
    assert not hasattr(trial_client, "auth")
    assert capability not in repr(trial_client)
    assert capability not in repr(vars(trial_client))

    with pytest.raises(ValueError, match="pjud_trial_supabase_client_requires_trial"):
        supabase_client.create_trial_supabase(normal)


@pytest.mark.parametrize(
    "supabase_url,secret_fragment",
    [
        ("http://db.test", None),
        ("https://user:password@db.test", "password"),
        ("https://@db.test", None),
        ("https://db.test/rest/v1", None),
        ("https://db.test?token=query-secret", "query-secret"),
        ("https://db.test?", None),
        ("https://db.test#fragment-secret", "fragment-secret"),
        ("https://db.test#", None),
        ("https:///missing-host", None),
        ("not-a-url", None),
    ],
)
def test_trial_rpc_rejects_non_origin_https_endpoint_before_request(
    supabase_url,
    secret_fragment,
):
    """Catch trial authority being sent to an untrusted or ambiguous endpoint."""
    from worker import supabase_client

    capability = "c" * 64
    requests = []
    trial = SimpleNamespace(
        SUPABASE_URL=supabase_url,
        SUPABASE_SERVICE_KEY="placeholder",
        PJUD_RUNTIME_GENERATION=GENERATION_A,
        PJUD_IMPORT_TRIAL_ONCE=True,
        PJUD_IMPORT_TRIAL_CAPABILITY=SecretStr(capability),
    )

    with pytest.raises(
        ValueError,
        match="^pjud_trial_rpc_invalid_endpoint$",
    ) as exc_info:
        supabase_client.create_trial_supabase(
            trial,
            transport=httpx.MockTransport(
                lambda request: requests.append(request),
            ),
        )

    diagnostic = repr(exc_info.value)
    assert capability not in diagnostic
    if secret_fragment is not None:
        assert secret_fragment not in diagnostic
    assert requests == []


@pytest.mark.parametrize("status_code", [300, 307, 399])
def test_trial_rpc_rejects_redirect_without_replaying_authority(status_code):
    """Catch a 3xx replaying the capability or API key to another origin."""
    from worker import supabase_client

    capability = "d" * 64
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code,
            headers={"location": "https://redirect.invalid/capture"},
        )

    trial = SimpleNamespace(
        SUPABASE_URL="https://db.test",
        SUPABASE_SERVICE_KEY="placeholder",
        PJUD_RUNTIME_GENERATION=GENERATION_A,
        PJUD_IMPORT_TRIAL_ONCE=True,
        PJUD_IMPORT_TRIAL_CAPABILITY=SecretStr(capability),
    )
    trial_client = supabase_client.create_trial_supabase(
        trial,
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        RuntimeError,
        match="^pjud_trial_rpc_redirect_rejected$",
    ) as exc_info:
        trial_client.rpc("claim_pjud_trial_import_job", {}).execute()

    assert capability not in repr(exc_info.value)
    assert len(requests) == 1
    assert requests[0].url.host == "db.test"
    assert requests[0].headers["x-pjud-runtime-trial-capability"] == capability


@pytest.mark.parametrize(
    "rpc_name",
    [
        None,
        1,
        ["claim_pjud_trial_import_job"],
        "",
        "foo/bar",
        "foo//bar",
        r"foo\bar",
        ".",
        "..",
        "%",
        "%2e%2e",
        "%252e%252e",
        "foo%2fbar",
        "foo%252fbar",
        "foo%3Fbar",
        "foo%23bar",
        "claim_pjud_trial_import_job/extra",
        "unknown_trial_rpc",
        "get_pjud_runtime_control",
    ],
)
def test_trial_rpc_rejects_name_outside_closed_allowlist_before_transport(
    rpc_name,
):
    """Catch malformed, encoded or non-trial RPC names gaining authority."""
    from worker import supabase_client

    capability = "e" * 64
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"unexpected": True})

    trial = SimpleNamespace(
        SUPABASE_URL="https://db.test",
        SUPABASE_SERVICE_KEY="placeholder",
        PJUD_RUNTIME_GENERATION=GENERATION_A,
        PJUD_IMPORT_TRIAL_ONCE=True,
        PJUD_IMPORT_TRIAL_CAPABILITY=SecretStr(capability),
    )
    trial_client = supabase_client.create_trial_supabase(
        trial,
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(
        RuntimeError,
        match="^pjud_trial_rpc_scope_violation$",
    ) as exc_info:
        trial_client.rpc(rpc_name, {}).execute()

    assert capability not in repr(exc_info.value)
    assert requests == []


@pytest.mark.parametrize(
    "rpc_name",
    [
        "claim_pjud_trial_import_job",
        "close_pjud_runtime_trial_grant",
        "finalize_pjud_trial_import_discovery",
        "pjud_proxy_finalize_trial_budget_reservation",
        "pjud_proxy_record_trial_usage",
        "pjud_proxy_reserve_trial_budget",
        "renew_pjud_trial_import_job_claim",
        "validate_pjud_trial_import_credential_claim",
    ],
)
def test_trial_rpc_closed_allowlist_keeps_each_trial_operation_reachable(
    rpc_name,
):
    """Catch an allowlist omission blocking one of the bounded trial RPCs."""
    from worker import supabase_client

    requests = []
    trial = SimpleNamespace(
        SUPABASE_URL="https://db.test",
        SUPABASE_SERVICE_KEY="placeholder",
        PJUD_RUNTIME_GENERATION=GENERATION_A,
        PJUD_IMPORT_TRIAL_ONCE=True,
        PJUD_IMPORT_TRIAL_CAPABILITY=SecretStr("f" * 64),
    )
    trial_client = supabase_client.create_trial_supabase(
        trial,
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request)
                or httpx.Response(200, json={"status": "ok"})
            ),
        ),
    )

    response = trial_client.rpc(rpc_name, {}).execute()

    assert response.data == {"status": "ok"}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == f"/rest/v1/rpc/{rpc_name}"


@pytest.mark.asyncio
async def test_release_captures_original_token_before_case_row_mutation(
    monkeypatch, worker_maintenance,
):
    from worker import __main__ as worker_main
    from tests.test_worker_startup import _entrypoint_config, _patch_entrypoint
    from worker.proxy_control import ProxyControlSnapshot
    config = _entrypoint_config(validation_once=True)
    config.PJUD_RUNTIME_GENERATION = GENERATION_A
    row = {"id": GENERATION_A, "sync_claim_token": GENERATION_A}
    scheduler = AsyncMock()
    scheduler.get_next_batch.return_value = [row]
    _patch_entrypoint(monkeypatch, worker_main, config=config, scheduler=scheduler,
        pool=MagicMock(initialize=AsyncMock(), close_all=AsyncMock()),
        metrics=MagicMock(stop=AsyncMock()), backoff=MagicMock(is_open=False),
        maintenance=worker_maintenance)
    monkeypatch.setattr(worker_main, "refresh_proxy_gate", AsyncMock(return_value=ProxyControlSnapshot(
        allowed=True, status="enabled", reason_code=None, revision=1, source="database")))
    monkeypatch.setattr(worker_main, "create_supabase", lambda _: RuntimeControlDB(runtime_control(generation=GENERATION_A)))
    engine = MagicMock(drain_work=AsyncMock())
    async def mutate(case):
        case["sync_claim_token"] = GENERATION_B
    engine.sync_case = AsyncMock(side_effect=mutate)
    monkeypatch.setattr(worker_main, "SyncEngine", lambda **_: engine)
    await worker_main.main()
    scheduler.release_batch.assert_awaited_once_with([{"case_id": GENERATION_A, "claim_token": GENERATION_A}])


@pytest.mark.asyncio
async def test_internal_engine_relay_fixes_generation_and_protects_headers(monkeypatch):
    import httpx
    from worker.engine import SyncEngine
    from worker.config import WorkerConfig
    from worker.sync_credentials import SyncCredentialClaim
    from tests.sync_claim_helpers import CLAIM, CREDENTIAL
    config = WorkerConfig(SUPABASE_URL="https://db.test", SUPABASE_SERVICE_KEY="k", _env_file=None,
        PJUD_RUNTIME_GENERATION=GENERATION_A, VERCEL_APP_URL="https://web.test", INTERNAL_CREDENTIALS_API_KEY="relay")
    engine = SyncEngine(MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), config)
    assert engine._sync_credentials._runtime_headers == {"x-pjud-runtime-generation": GENERATION_A}
    config.PJUD_RUNTIME_GENERATION = GENERATION_B
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=CREDENTIAL if request.url.path.endswith("decrypt") else {})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    await engine._sync_credentials.decrypt(SyncCredentialClaim.model_validate(CLAIM))
    await engine._call_app_internal("POST", "/api/internal/test", "test", law_firm_id="tenant")
    assert [request.headers["x-pjud-runtime-generation"] for request in seen] == [GENERATION_A, GENERATION_A]
    assert seen[1].headers["authorization"] == "Bearer relay"
    assert seen[1].headers["x-law-firm-id"] == "tenant"
    for name in ("X-PjUd-RuNtImE-GeNeRaTiOn", "AUTHORIZATION", "x-Law-Firm-id"):
        assert await engine._call_app_internal("POST", "/api/internal/test", "test",
            law_firm_id="tenant", extra_headers={name: "override"}) is None
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_prewarm_retry_rechecks_runtime_before_second_mint(monkeypatch):
    from worker.__main__ import safe_initialize_pool
    db = RuntimeControlDB()
    async def failed_mint():
        db.control["admission_paused"] = True
        raise RuntimeError("synthetic mint failure")
    pool = MagicMock(initialize=AsyncMock(side_effect=failed_mint))
    monkeypatch.setattr("worker.__main__.asyncio.sleep", AsyncMock())
    with pytest.raises(PjudRuntimeError, match="^pjud_admission_paused$"):
        await safe_initialize_pool(pool, max_retries=2,
            runtime_fence=RuntimeFence(db, None))
    assert pool.initialize.await_count == 1


@pytest.mark.asyncio
async def test_missing_worker_fence_cannot_admit_cases_or_discovery():
    from worker.__main__ import process_batch, run_import_discovery_loop
    engine = MagicMock(sync_case=AsyncMock(), process_import_job=AsyncMock())
    shutdown = asyncio.Event()
    await process_batch([{"id": "case"}], engine, 1, shutdown,
        MagicMock(is_open=False), runtime_fence=None, processing_window=lambda: True)
    asyncio.get_running_loop().call_later(0.01, shutdown.set)
    await run_import_discovery_loop(engine, MagicMock(), shutdown,
        runtime_fence=None, poll_interval=0.001)
    engine.sync_case.assert_not_awaited()
    engine.process_import_job.assert_not_awaited()
