import asyncio
import httpx
import json
import traceback
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from pydantic import SecretStr

from app.familia.auth import InvalidCredentialsError
from app.my_causes.models import ImportCandidate
from app.my_causes.client import DiscoveryResult
from app.ojv.errors import (
    OjvTimeoutError,
    OjvUpstreamChangedError,
    OjvWafError,
    SessionExpiredError,
)
from app.proxy_billing import ProxyBillingExhaustedError
from app.proxy_cost import ProxyBudgetExceededError, ProxyUsagePersistenceError
from worker.import_jobs import ImportDiscoveryWorker
from worker.trial_scope import TrialScope


JOB = {
    "status": "acquired",
    "job_id": "98200000-0000-4000-8000-000000000041",
    "law_firm_id": "98200000-0000-4000-8000-000000000001",
    "credential_id": "98200000-0000-4000-8000-000000000021",
    "matters": ["civil"],
    "include_closed": False,
    "claim_token": "98200000-0000-4000-8000-000000000099",
    "lease_expires_at": "2026-08-23T13:00:00+00:00",
}
TRIAL_CAPABILITY = "c" * 64
TRIAL_GENERATION = "11111111-1111-4111-8111-111111111111"
TRIAL_GRANT_ID = "98200000-0000-4000-8000-000000000031"
TRIAL_EVIDENCE_SHA256 = "a" * 64
TRIAL_JOB = {
    **JOB,
    "trial_grant_id": TRIAL_GRANT_ID,
    "expected_credentials_updated_at": "2026-08-23T12:00:00.000Z",
}


def trial_close_proof(
    *,
    job_status="needs_selection",
    summary_status="needs_selection",
    discovered_count=1,
    candidate_count=1,
    evidence_sha256=TRIAL_EVIDENCE_SHA256,
):
    return {
        "status": "trial_grant_closed",
        "job_status": job_status,
        "summary_status": summary_status,
        "discovered_count": discovered_count,
        "candidate_count": candidate_count,
        "evidence_sha256": evidence_sha256,
    }


class Rpc:
    def __init__(self, response):
        self.response = response

    def execute(self):
        if isinstance(self.response, BaseException):
            raise self.response
        return SimpleNamespace(data=self.response, error=None)


class FakeSupabase:
    def __init__(self, claim=JOB, trial_claim=None):
        self.claim = claim
        self.trial_claim = TRIAL_JOB if trial_claim is None else trial_claim
        self.trial_claim_results = None
        self.calls = []
        self.renew_result = True
        self.credential_valid = True
        self.finalize_trial_results = [None]
        self.close_results = [trial_close_proof()]
        self.before_close = None

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        if name == "claim_pjud_import_job":
            return Rpc(self.claim)
        if name == "claim_pjud_trial_import_job":
            result = (
                self.trial_claim_results.pop(0)
                if self.trial_claim_results is not None
                else self.trial_claim
            )
            return Rpc(result)
        if name == "renew_pjud_import_job_claim":
            return Rpc(self.renew_result)
        if name == "renew_pjud_trial_import_job_claim":
            return Rpc(self.renew_result)
        if name == "validate_pjud_import_credential_claim":
            return Rpc(self.credential_valid)
        if name == "validate_pjud_trial_import_credential_claim":
            return Rpc(self.credential_valid)
        if name == "finalize_pjud_import_discovery":
            if not self.credential_valid:
                return Rpc(RuntimeError("import_credential_revision_mismatch"))
            return Rpc(None)
        if name == "finalize_pjud_trial_import_discovery":
            if not self.credential_valid:
                return Rpc(RuntimeError("import_credential_revision_mismatch"))
            return Rpc(self.finalize_trial_results.pop(0))
        if name == "close_pjud_runtime_trial_grant":
            if self.before_close is not None:
                self.before_close()
            result = self.close_results.pop(0)
            return Rpc(result)
        raise AssertionError(name)


class FakeSession:
    def __init__(self, *, login_error=None):
        self.login = AsyncMock(side_effect=login_error)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True


class FakePool:
    def __init__(self):
        self.bundle = SimpleNamespace(proxy_url="http://proxy.invalid", cookies=(), user_agent="ua")
        self.slot = object()
        self.acquire_familia_bundle = AsyncMock(return_value=(self.bundle, self.slot))
        self.release_familia_bundle = AsyncMock()


class FakeProxyUsage:
    def __init__(self):
        self.calls = []

    enabled = True

    @asynccontextmanager
    async def track(self, **kwargs):
        self.calls.append(kwargs)
        yield


class SettlingProxyUsage(FakeProxyUsage):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.events = []

    @asynccontextmanager
    async def track(self, **kwargs):
        self.calls.append(kwargs)
        self.active += 1
        self.events.append("reserved")
        try:
            yield
        finally:
            self.active -= 1
            self.events.append("settled")


class RejectingProxyUsage:
    def __init__(self, error):
        self.error = error
        self.calls = []

    enabled = True

    @asynccontextmanager
    async def track(self, **kwargs):
        self.calls.append(kwargs)
        raise self.error
        yield  # pragma: no cover - keeps this an async context manager


def candidate(**overrides):
    values = {
        "matter": "civil",
        "case_type": "rit",
        "case_number": "C-10-2026",
        "court_code": 90,
        "court_label": "C.A. Santiago",
        "tribunal_code": 259,
        "tribunal_label": "1 Civil Santiago",
        "libro": "C",
        "filed_at": None,
        "upstream_status": "Abierta",
        "caption": "Persona A / Persona B",
    }
    values.update(overrides)
    return ImportCandidate.model_validate(values)


def trial_scope() -> TrialScope:
    return TrialScope(
        capability=SecretStr(TRIAL_CAPABILITY),
        runtime_generation=TRIAL_GENERATION,
        trial_grant_id=TRIAL_GRANT_ID,
        job_id=JOB["job_id"],
        claim_token=JOB["claim_token"],
        worker_id="import-worker",
        law_firm_id=JOB["law_firm_id"],
        credential_id=JOB["credential_id"],
        expected_credentials_updated_at="2026-08-23T12:00:00.000Z",
    )


def make_worker(
    *, claim=JOB, discovery=None, credential=None, session=None, concurrency=1,
    proxy_usage=None,
):
    sb = FakeSupabase(claim)
    pool = FakePool()
    discovery = discovery or AsyncMock(return_value=DiscoveryResult(
        candidates=[candidate()], page_count=1, status="ok",
    ))
    credential = credential or AsyncMock(return_value={
        "rut": "11111111-1", "password": "secret-value", "password_type": "clave_poder_judicial",
        "binding_version": "2026-08-23T12:00:00.000Z",
    })
    session = session or FakeSession()
    session_factory = MagicMock(return_value=session)
    proxy_usage = proxy_usage or FakeProxyUsage()
    worker = ImportDiscoveryWorker(
        supabase=sb,
        pool=pool,
        worker_id="import-worker",
        fetch_credential=credential,
        discover=discovery,
        session_factory=session_factory,
        concurrency=concurrency,
        lease_seconds=30,
        renewal_interval_seconds=0.001,
        proxy_usage=proxy_usage,
    )
    return worker, sb, pool, discovery, credential, session_factory, session


def make_trial_worker(
    *, trial_claim=TRIAL_JOB, discovery=None, credential=None, proxy_usage=None,
):
    sb = FakeSupabase(trial_claim=trial_claim)
    pool = FakePool()
    discovery = discovery or AsyncMock(return_value=DiscoveryResult(
        candidates=[candidate()], page_count=1, status="ok",
    ))
    normal_credential = AsyncMock(
        side_effect=AssertionError("trial must not use normal credential relay"),
    )
    trial_credential = credential or AsyncMock(return_value={
        "rut": "11111111-1",
        "password": "secret-value",
        "password_type": "clave_poder_judicial",
        "binding_version": "2026-08-23T12:00:00.000Z",
    })
    session = FakeSession()
    proxy_usage = proxy_usage or FakeProxyUsage()
    worker = ImportDiscoveryWorker(
        supabase=sb,
        trial_supabase=sb,
        pool=pool,
        worker_id="import-worker",
        fetch_credential=normal_credential,
        fetch_trial_credential=trial_credential,
        discover=discovery,
        session_factory=MagicMock(return_value=session),
        lease_seconds=30,
        renewal_interval_seconds=0.001,
        proxy_usage=proxy_usage,
    )
    return (
        worker,
        sb,
        pool,
        discovery,
        normal_credential,
        trial_credential,
        proxy_usage,
        session,
    )


@pytest.mark.asyncio
async def test_trial_decrypt_revision_mismatch_fails_before_pool_or_provider():
    """Catch the worker deriving authority from decrypt instead of the exact claim."""
    credential = AsyncMock(return_value={
        "rut": "11111111-1",
        "password": "secret-value",
        "password_type": "clave_poder_judicial",
        "binding_version": "2026-08-23T12:00:01.000Z",
    })
    worker, sb, pool, discover, _normal, *_ = make_trial_worker(
        credential=credential,
    )

    with pytest.raises(RuntimeError, match="trial_import_credential_revision_mismatch"):
        await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )

    pool.acquire_familia_bundle.assert_not_awaited()
    discover.assert_not_awaited()
    assert [name for name, _payload in sb.calls] == ["claim_pjud_trial_import_job"]


@pytest.mark.asyncio
async def test_trial_claim_is_exact_and_propagates_one_immutable_scope_everywhere():
    """Catch fallback to the generic queue or tuple loss before provider work."""
    (
        worker,
        sb,
        pool,
        _discover,
        normal_credential,
        trial_credential,
        proxy_usage,
        _session,
    ) = make_trial_worker()

    outcome = await worker.process_trial_next(
        capability=SecretStr(TRIAL_CAPABILITY),
        runtime_generation=TRIAL_GENERATION,
    )

    assert outcome.claimed is True
    assert outcome.successful is True
    assert outcome.job_status == "needs_selection"
    assert outcome.summary_status == "needs_selection"
    assert outcome.discovered_count == 1
    assert outcome.candidate_count == 1
    assert outcome.evidence_sha256 == TRIAL_EVIDENCE_SHA256
    assert outcome.job_id == JOB["job_id"]

    assert sb.calls[0] == (
        "claim_pjud_trial_import_job",
        {
            "p_expected_generation": TRIAL_GENERATION,
            "p_worker_id": "import-worker",
            "p_lease_seconds": 30,
        },
    )
    names = [name for name, _payload in sb.calls]
    assert not {
        "claim_pjud_import_job",
        "renew_pjud_import_job_claim",
        "validate_pjud_import_credential_claim",
        "finalize_pjud_import_discovery",
    }.intersection(names)
    assert "validate_pjud_trial_import_credential_claim" in names
    assert "finalize_pjud_trial_import_discovery" in names
    assert names[-2:] == [
        "finalize_pjud_trial_import_discovery",
        "close_pjud_runtime_trial_grant",
    ]
    normal_credential.assert_not_awaited()
    scope = trial_credential.await_args.args[-1]
    assert isinstance(scope, TrialScope)
    assert str(scope.trial_grant_id) == TRIAL_GRANT_ID
    assert str(scope.job_id) == JOB["job_id"]
    assert str(scope.claim_token) == JOB["claim_token"]
    assert str(scope.law_firm_id) == JOB["law_firm_id"]
    assert str(scope.credential_id) == JOB["credential_id"]
    assert scope.expected_credentials_updated_at.isoformat() == (
        "2026-08-23T12:00:00+00:00"
    )
    assert scope.worker_id == "import-worker"
    assert scope.capability.get_secret_value() == TRIAL_CAPABILITY
    assert TRIAL_CAPABILITY not in repr(scope)
    pool.acquire_familia_bundle.assert_awaited_once_with(trial_scope=scope)
    pool.release_familia_bundle.assert_awaited_once_with(
        pool.slot, disposition="healthy", remint=True, trial_scope=scope,
    )
    assert proxy_usage.calls
    assert all(call["trial_scope"] is scope for call in proxy_usage.calls)
    trial_payloads = [
        payload
        for name, payload in sb.calls
        if name in {
            "validate_pjud_trial_import_credential_claim",
            "finalize_pjud_trial_import_discovery",
        }
    ]
    assert trial_payloads
    assert all(payload["p_trial_grant_id"] == TRIAL_GRANT_ID for payload in trial_payloads)
    assert all(
        payload["p_expected_generation"] == TRIAL_GENERATION
        for payload in trial_payloads
    )
    assert all(
        payload["p_expected_credential_updated_at"]
        == "2026-08-23T12:00:00+00:00"
        for payload in trial_payloads
    )
    assert all("p_generation" not in payload for payload in trial_payloads)
    assert all(
        "p_expected_credentials_updated_at" not in payload
        for payload in trial_payloads
    )
    validation_payloads = [
        payload
        for name, payload in sb.calls
        if name == "validate_pjud_trial_import_credential_claim"
    ]
    assert validation_payloads
    assert all(set(payload) == {
        "p_expected_generation",
        "p_trial_grant_id",
        "p_job_id",
        "p_claim_token",
        "p_worker_id",
        "p_credential_id",
        "p_expected_credential_updated_at",
    } for payload in validation_payloads)
    finalize_payload = next(
        payload
        for name, payload in sb.calls
        if name == "finalize_pjud_trial_import_discovery"
    )
    assert set(finalize_payload) == {
        "p_expected_generation",
        "p_trial_grant_id",
        "p_job_id",
        "p_claim_token",
        "p_worker_id",
        "p_candidates",
        "p_summary",
        "p_expected_credential_updated_at",
    }
    close_payload = sb.calls[-1][1]
    assert close_payload == {
        "p_expected_generation": TRIAL_GENERATION,
        "p_trial_grant_id": TRIAL_GRANT_ID,
        "p_job_id": JOB["job_id"],
    }
    assert all(TRIAL_CAPABILITY not in repr(payload) for payload in trial_payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("discovery", "credential", "job_status", "summary_status", "count"),
    [
        (
            DiscoveryResult(candidates=[], page_count=1, status="ok"),
            None,
            "completed",
            "needs_selection",
            0,
        ),
        (
            DiscoveryResult(candidates=[], page_count=0, status="credential_invalid"),
            AsyncMock(return_value=None),
            "failed",
            "failed",
            0,
        ),
        (
            DiscoveryResult(candidates=[], page_count=0, status="session_expired"),
            None,
            "failed",
            "failed",
            0,
        ),
        (
            DiscoveryResult(candidates=[], page_count=0, status="waf"),
            None,
            "failed",
            "failed",
            0,
        ),
        (
            DiscoveryResult(candidates=[], page_count=0, status="timeout"),
            None,
            "failed",
            "failed",
            0,
        ),
        (
            DiscoveryResult(
                candidates=[candidate()], page_count=1, status="upstream_changed",
            ),
            None,
            "partial",
            "partial",
            1,
        ),
    ],
)
async def test_trial_unsuccessful_discovery_returns_persisted_outcome_after_close(
    discovery, credential, job_status, summary_status, count,
):
    """Catch a terminal discovery failure being relabeled as trial success."""
    worker, sb, *_ = make_trial_worker(
        discovery=AsyncMock(return_value=discovery),
        credential=credential,
    )
    sb.close_results = [trial_close_proof(
        job_status=job_status,
        summary_status=summary_status,
        discovered_count=count,
        candidate_count=count,
    )]

    outcome = await worker.process_trial_next(
        capability=SecretStr(TRIAL_CAPABILITY),
        runtime_generation=TRIAL_GENERATION,
    )

    assert outcome.claimed is True
    assert outcome.successful is False
    assert outcome.job_status == job_status
    assert outcome.summary_status == summary_status
    assert outcome.discovered_count == count
    assert outcome.candidate_count == count
    assert outcome.job_id == JOB["job_id"]
    assert [name for name, _payload in sb.calls][-2:] == [
        "finalize_pjud_trial_import_discovery",
        "close_pjud_runtime_trial_grant",
    ]
    assert sum(
        name == "close_pjud_runtime_trial_grant"
        for name, _payload in sb.calls
    ) == 1


@pytest.mark.asyncio
async def test_trial_candidate_payload_failure_is_persisted_as_failed_and_closed():
    worker, sb, *_ = make_trial_worker(discovery=AsyncMock(return_value=DiscoveryResult(
        candidates=[candidate()], page_count=1, status="ok",
    )))
    sb.close_results = [trial_close_proof(
        job_status="failed",
        summary_status="failed",
        discovered_count=0,
        candidate_count=0,
    )]

    with patch(
        "worker.import_jobs._candidate_payloads",
        side_effect=ValueError("synthetic serialization contract failure"),
    ):
        outcome = await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )

    assert outcome.successful is False
    assert outcome.job_status == "failed"
    assert outcome.summary_status == "failed"
    assert outcome.discovered_count == 0
    assert [name for name, _payload in sb.calls][-2:] == [
        "finalize_pjud_trial_import_discovery",
        "close_pjud_runtime_trial_grant",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_generation",
    [
        "11111111-1111-1111-8111-111111111111",
        "11111111-1111-4111-7111-111111111111",
    ],
)
async def test_trial_rejects_non_v4_generation_before_first_rpc(runtime_generation):
    worker, sb, pool, discover, normal_credential, trial_credential, *_ = (
        make_trial_worker()
    )

    with pytest.raises(ValueError, match="pjud_runtime_invalid_generation"):
        await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=runtime_generation,
        )

    assert sb.calls == []
    normal_credential.assert_not_awaited()
    trial_credential.assert_not_awaited()
    pool.acquire_familia_bundle.assert_not_awaited()
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_claim_replays_once_with_the_exact_payload_after_ambiguous_transport():
    worker, sb, pool, discover, _normal, trial_credential, *_ = make_trial_worker()
    sb.trial_claim_results = [httpx.ReadTimeout("response lost"), TRIAL_JOB]

    with patch("worker.import_jobs.asyncio.sleep", new=AsyncMock()) as sleep:
        outcome = await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )
        assert outcome.successful is True

    claim_calls = [
        entry for entry in sb.calls if entry[0] == "claim_pjud_trial_import_job"
    ]
    assert len(claim_calls) == 2
    assert claim_calls[0] == claim_calls[1]
    assert call(0.1) in sleep.await_args_list
    pool.acquire_familia_bundle.assert_awaited_once()
    trial_credential.assert_awaited_once()
    discover.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second_result",
    [
        httpx.RemoteProtocolError("second response lost"),
        RuntimeError("database detail"),
    ],
)
async def test_trial_claim_persistent_unknown_is_redacted_before_provider(
    second_result,
):
    worker, sb, pool, discover, _normal, trial_credential, *_ = make_trial_worker()
    sb.trial_claim_results = [
        httpx.WriteTimeout("first response lost"),
        second_result,
    ]

    with patch("worker.import_jobs.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(
            RuntimeError, match="^pjud_trial_import_job_claim_unconfirmed$",
        ) as exc_info:
            await worker.process_trial_next(
                capability=SecretStr(TRIAL_CAPABILITY),
                runtime_generation=TRIAL_GENERATION,
            )

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "response lost" not in rendered
    assert "database detail" not in rendered
    assert TRIAL_CAPABILITY not in rendered
    claim_calls = [
        entry for entry in sb.calls if entry[0] == "claim_pjud_trial_import_job"
    ]
    assert len(claim_calls) == 2
    assert claim_calls[0] == claim_calls[1]
    assert call(0.1) in sleep.await_args_list
    pool.acquire_familia_bundle.assert_not_awaited()
    trial_credential.assert_not_awaited()
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_close_waits_until_proxy_reservations_are_settled():
    proxy_usage = SettlingProxyUsage()
    worker, sb, *_ = make_trial_worker(proxy_usage=proxy_usage)

    def assert_settled_before_close():
        assert proxy_usage.calls
        assert proxy_usage.active == 0
        assert proxy_usage.events[-1] == "settled"
        assert sb.calls[-2][0] == "finalize_pjud_trial_import_discovery"
        proxy_usage.events.append("close")

    sb.before_close = assert_settled_before_close

    outcome = await worker.process_trial_next(
        capability=SecretStr(TRIAL_CAPABILITY),
        runtime_generation=TRIAL_GENERATION,
    )
    assert outcome.successful is True
    assert proxy_usage.events[-2:] == ["settled", "close"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_result",
    [
        True,
        False,
        None,
        {"status": "trial_grant_closed"},
        {**trial_close_proof(), "unexpected": "field"},
        trial_close_proof(job_status="unexpected_status"),
        trial_close_proof(summary_status="unexpected_status"),
        trial_close_proof(evidence_sha256="A" * 64),
        trial_close_proof(discovered_count=True),
        trial_close_proof(candidate_count=-1),
        RuntimeError("database detail"),
    ],
)
async def test_trial_close_malformed_contract_or_error_is_terminal_and_redacted(
    close_result,
):
    worker, sb, *_ = make_trial_worker()
    sb.close_results = [close_result]

    with pytest.raises(
        RuntimeError, match="^pjud_trial_grant_close_unconfirmed$",
    ) as exc_info:
        await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "database detail" not in rendered
    assert TRIAL_CAPABILITY not in rendered
    assert [name for name, _payload in sb.calls][-2:] == [
        "finalize_pjud_trial_import_discovery",
        "close_pjud_runtime_trial_grant",
    ]
    assert sum(
        name == "close_pjud_runtime_trial_grant"
        for name, _payload in sb.calls
    ) == 1


@pytest.mark.asyncio
async def test_trial_close_replays_the_exact_rpc_after_ambiguous_transport():
    worker, sb, *_ = make_trial_worker()
    sb.close_results = [
        httpx.ReadTimeout("response lost"),
        trial_close_proof(),
    ]

    with patch("worker.import_jobs.asyncio.sleep", new=AsyncMock()) as sleep:
        outcome = await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )
        assert outcome.successful is True

    close_calls = [
        call for call in sb.calls if call[0] == "close_pjud_runtime_trial_grant"
    ]
    assert len(close_calls) == 2
    assert close_calls[0] == close_calls[1]
    assert call(0.1) in sleep.await_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_result",
    [
        trial_close_proof(
            discovered_count=1,
            candidate_count=2,
        ),
        trial_close_proof(
            job_status="completed",
            summary_status="needs_selection",
            discovered_count=0,
            candidate_count=0,
        ),
        trial_close_proof(
            job_status="partial",
            summary_status="partial",
        ),
        trial_close_proof(
            job_status="failed",
            summary_status="failed",
            discovered_count=0,
            candidate_count=0,
        ),
    ],
)
async def test_trial_close_proof_requires_selectable_coherent_evidence_for_success(
    close_result,
):
    worker, sb, *_ = make_trial_worker()
    sb.close_results = [close_result]

    outcome = await worker.process_trial_next(
        capability=SecretStr(TRIAL_CAPABILITY),
        runtime_generation=TRIAL_GENERATION,
    )

    assert outcome.claimed is True
    assert outcome.successful is False
    assert outcome.job_status == close_result["job_status"]
    assert outcome.candidate_count == close_result["candidate_count"]
    assert outcome.evidence_sha256 == TRIAL_EVIDENCE_SHA256


@pytest.mark.asyncio
async def test_trial_close_persistent_ambiguity_is_terminal_and_redacted():
    worker, sb, *_ = make_trial_worker()
    sb.close_results = [
        httpx.WriteTimeout("first response lost"),
        httpx.ReadTimeout("second response lost"),
    ]

    with patch("worker.import_jobs.asyncio.sleep", new=AsyncMock()) as sleep:
        with pytest.raises(
            RuntimeError, match="^pjud_trial_grant_close_unconfirmed$",
        ) as exc_info:
            await worker.process_trial_next(
                capability=SecretStr(TRIAL_CAPABILITY),
                runtime_generation=TRIAL_GENERATION,
            )

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "response lost" not in rendered
    assert TRIAL_CAPABILITY not in rendered
    close_calls = [
        call for call in sb.calls if call[0] == "close_pjud_runtime_trial_grant"
    ]
    assert len(close_calls) == 2
    assert close_calls[0] == close_calls[1]
    assert call(0.1) in sleep.await_args_list


@pytest.mark.asyncio
async def test_trial_finalize_failure_never_attempts_close():
    worker, sb, *_ = make_trial_worker()
    sb.finalize_trial_results = [RuntimeError("finalize unavailable")]

    with pytest.raises(RuntimeError, match="finalize unavailable"):
        await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )

    assert "close_pjud_runtime_trial_grant" not in [
        name for name, _payload in sb.calls
    ]


@pytest.mark.asyncio
async def test_trial_finalize_replays_exact_rpc_after_ambiguous_transport():
    worker, sb, *_ = make_trial_worker()
    sb.finalize_trial_results = [httpx.ReadTimeout("response lost"), None]

    with patch("worker.import_jobs.asyncio.sleep", new=AsyncMock()) as sleep:
        outcome = await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )
        assert outcome.successful is True

    finalize_calls = [
        call for call in sb.calls
        if call[0] == "finalize_pjud_trial_import_discovery"
    ]
    assert len(finalize_calls) == 2
    assert finalize_calls[0] == finalize_calls[1]
    assert sb.calls[-1][0] == "close_pjud_runtime_trial_grant"
    assert call(0.1) in sleep.await_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replay_result",
    [
        RuntimeError("scope mismatch"),
        httpx.WriteTimeout("second response also lost"),
    ],
)
async def test_trial_finalize_unknown_replay_never_attempts_close(replay_result):
    worker, sb, *_ = make_trial_worker()
    sb.finalize_trial_results = [
        httpx.ReadTimeout("first response lost"),
        replay_result,
    ]

    with patch("worker.import_jobs.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(
            RuntimeError,
            match="^pjud_trial_discovery_finalize_unconfirmed$",
        ) as exc_info:
            await worker.process_trial_next(
                capability=SecretStr(TRIAL_CAPABILITY),
                runtime_generation=TRIAL_GENERATION,
            )

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert "response lost" not in rendered
    assert "scope mismatch" not in rendered
    assert TRIAL_CAPABILITY not in rendered
    assert len([
        call for call in sb.calls
        if call[0] == "finalize_pjud_trial_import_discovery"
    ]) == 2
    assert "close_pjud_runtime_trial_grant" not in [
        name for name, _payload in sb.calls
    ]


@pytest.mark.asyncio
async def test_sync_engine_trial_entrypoint_passes_only_validated_secret_and_generation():
    from worker.engine import SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._config = SimpleNamespace(
        PJUD_IMPORT_TRIAL_CAPABILITY=SecretStr(TRIAL_CAPABILITY),
        PJUD_RUNTIME_GENERATION=TRIAL_GENERATION,
    )
    expected = SimpleNamespace(
        claimed=True,
        successful=True,
        job_status="needs_selection",
        summary_status="needs_selection",
        discovered_count=1,
        candidate_count=1,
        evidence_sha256=TRIAL_EVIDENCE_SHA256,
        job_id=JOB["job_id"],
    )
    engine._import_worker = MagicMock(
        process_trial_next=AsyncMock(return_value=expected),
        process_next=AsyncMock(
            side_effect=AssertionError("generic claim must not be reachable"),
        ),
    )

    assert await engine.process_trial_import_job() is expected

    engine._import_worker.process_trial_next.assert_awaited_once_with(
        capability=engine._config.PJUD_IMPORT_TRIAL_CAPABILITY,
        runtime_generation=TRIAL_GENERATION,
    )
    engine._import_worker.process_next.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trial_claim",
    [JOB, [TRIAL_JOB, {**TRIAL_JOB, "job_id": "98200000-0000-4000-8000-000000000042"}]],
)
async def test_trial_claim_contract_fails_closed_before_any_provider_boundary(trial_claim):
    """Catch a missing grant or multi-job response being processed as exact authority."""
    worker, sb, pool, discover, normal_credential, trial_credential, *_ = (
        make_trial_worker(trial_claim=trial_claim)
    )

    with pytest.raises(RuntimeError, match="invalid_trial_import_job_claim_contract"):
        await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )

    assert [name for name, _payload in sb.calls] == ["claim_pjud_trial_import_job"]
    normal_credential.assert_not_awaited()
    trial_credential.assert_not_awaited()
    pool.acquire_familia_bundle.assert_not_awaited()
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_claim_contract_error_does_not_expose_authority_tuple():
    malformed = {
        **TRIAL_JOB,
        "unexpected_secretish_field": TRIAL_CAPABILITY,
    }
    worker, *_ = make_trial_worker(trial_claim=malformed)

    with pytest.raises(RuntimeError) as exc_info:
        await worker.process_trial_next(
            capability=SecretStr(TRIAL_CAPABILITY),
            runtime_generation=TRIAL_GENERATION,
        )

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert rendered.strip().endswith("invalid_trial_import_job_claim_contract")
    for sensitive in (
        TRIAL_CAPABILITY,
        TRIAL_GRANT_ID,
        JOB["job_id"],
        JOB["claim_token"],
        JOB["credential_id"],
    ):
        assert sensitive not in rendered


@pytest.mark.asyncio
async def test_claims_fetches_scoped_credential_discovers_and_finalizes_once():
    worker, sb, pool, discover, credential, session_factory, session = make_worker()
    proxy_usage = worker._proxy_usage

    assert await worker.process_next() is True

    credential.assert_awaited_once_with(
        JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
        JOB["claim_token"], "import-worker",
    )
    login_args = session.login.await_args.args
    assert login_args[2] == "clave_pj"
    assert isinstance(login_args[0], SecretStr)
    assert isinstance(login_args[1], SecretStr)
    assert login_args[0].get_secret_value() == "11111111-1"
    assert login_args[1].get_secret_value() == "secret-value"
    discover.assert_awaited_once()
    assert sb.calls[0] == (
        "claim_pjud_import_job",
        {"p_worker_id": "import-worker", "p_lease_seconds": 30},
    )
    finalize = [entry for entry in sb.calls if entry[0] == "finalize_pjud_import_discovery"]
    assert len(finalize) == 1
    payload = finalize[0][1]
    assert payload["p_job_id"] == JOB["job_id"]
    assert payload["p_claim_token"] == JOB["claim_token"]
    assert payload["p_expected_credential_updated_at"] == "2026-08-23T12:00:00.000Z"
    assert payload["p_summary"] == {
        "status": "needs_selection", "pages": 1, "discovered": 1,
    }
    persisted = payload["p_candidates"][0]
    assert set(persisted) == {
        "matter", "case_type", "case_number", "court_code", "court_label",
        "tribunal_code", "tribunal_label", "libro", "filed_at",
        "upstream_status", "caption", "source_hash",
    }
    assert "secret-value" not in json.dumps(payload)
    pool.release_familia_bundle.assert_awaited_once()
    assert session.closed is True
    validations = [payload for name, payload in sb.calls if name == "validate_pjud_import_credential_claim"]
    assert len(validations) == 2
    assert all(payload == {
        "p_job_id": JOB["job_id"],
        "p_claim_token": JOB["claim_token"],
        "p_worker_id": "import-worker",
        "p_credential_id": JOB["credential_id"],
        "p_expected_credential_updated_at": "2026-08-23T12:00:00.000Z",
    } for payload in validations)
    assert len(proxy_usage.calls) == 1
    assert proxy_usage.calls[0] == {
        "operation": "other",
        "law_firm_id": JOB["law_firm_id"],
        "import_job_id": JOB["job_id"],
        "import_claim_token": JOB["claim_token"],
        "import_worker_id": "import-worker",
        "transaction_key": f"{JOB['job_id']}:{JOB['claim_token']}:session-1:login",
    }
    request_scope = discover.await_args.kwargs["request_scope"]
    async with request_scope("civil:1:1"):
        pass
    assert proxy_usage.calls[-1]["operation"] == "search"
    assert proxy_usage.calls[-1]["transaction_key"].endswith(":session-1:page:civil:1:1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary_error",
    [
        ProxyBudgetExceededError("proxy budget exceeded"),
        ProxyUsagePersistenceError("proxy usage persistence unavailable"),
    ],
)
async def test_paid_boundary_failure_stops_before_import_login_or_listing(boundary_error):
    proxy_usage = RejectingProxyUsage(boundary_error)
    worker, sb, pool, discover, _credential, _factory, session = make_worker(
        proxy_usage=proxy_usage,
    )

    with pytest.raises(type(boundary_error)):
        await worker.process_next()

    session.login.assert_not_awaited()
    discover.assert_not_awaited()
    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)
    assert proxy_usage.calls[0]["operation"] == "other"
    pool.release_familia_bundle.assert_awaited_once_with(
        pool.slot, disposition="healthy", remint=False,
    )


@pytest.mark.asyncio
async def test_revoke_after_fetch_blocks_login_and_never_publishes_candidates():
    worker, sb, _pool, discover, *_ = make_worker()

    async def revoke_during_login(*_args):
        sb.credential_valid = False

    worker._session_factory.return_value.login.side_effect = revoke_during_login

    with pytest.raises(RuntimeError, match="import_credential_revision_lost"):
        await worker.process_next()

    discover.assert_not_awaited()
    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)


@pytest.mark.asyncio
async def test_replace_during_listing_is_rejected_by_atomic_finalize():
    worker, sb, _pool, discover, *_ = make_worker()

    async def replace_during_listing(*_args, **_kwargs):
        sb.credential_valid = False
        return DiscoveryResult(candidates=[candidate()], page_count=1, status="ok")

    discover.side_effect = replace_during_listing

    with pytest.raises(RuntimeError, match="import_credential_revision_mismatch"):
        await worker.process_next()

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"]
    assert len(final) == 1
    assert final[0]["p_expected_credential_updated_at"] == "2026-08-23T12:00:00.000Z"


@pytest.mark.asyncio
async def test_empty_claim_is_a_noop_without_credential_or_pjud_traffic():
    worker, sb, pool, discover, credential, *_ = make_worker(claim={"status": "empty"})

    assert await worker.process_next() is False

    credential.assert_not_awaited()
    discover.assert_not_awaited()
    pool.acquire_familia_bundle.assert_not_awaited()
    assert [name for name, _ in sb.calls] == ["claim_pjud_import_job"]


@pytest.mark.asyncio
async def test_zero_candidates_finalize_as_terminal_completed_via_rpc_contract():
    result = DiscoveryResult(candidates=[], page_count=1, status="ok")
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_candidates"] == []
    assert final["p_summary"] == {
        "status": "needs_selection", "pages": 1, "discovered": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_status", "error_code"),
    [
        ("credential_invalid", "failed", "credential_invalid"),
        ("session_expired", "failed", "session_expired"),
        ("waf", "failed", "ojv_blocked"),
        ("timeout", "failed", "pjud_timeout"),
        ("upstream_changed", "partial", "upstream_changed"),
    ],
)
async def test_terminal_results_finalize_with_closed_error_taxonomy(status, expected_status, error_code):
    result = DiscoveryResult(candidates=[], page_count=2, status=status)
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_candidates"] == []
    assert final["p_summary"] == {
        "status": expected_status,
        "pages": 2,
        "discovered": 0,
        "error_code": error_code,
        "error_class": "upstream" if status == "upstream_changed" else "authentication" if status in {"credential_invalid", "session_expired"} else "transport",
    }


@pytest.mark.asyncio
async def test_invalid_login_is_terminal_and_never_calls_listing_discovery():
    session = FakeSession(login_error=InvalidCredentialsError("credential rejected"))
    worker, sb, _pool, discover, *_ = make_worker(session=session)

    assert await worker.process_next() is True

    discover.assert_not_awaited()
    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_summary"]["error_code"] == "credential_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "error_code", "expected_sessions", "expected_disposition"),
    [
        (SessionExpiredError(), "session_expired", 2, "replace_before_reuse"),
        (OjvWafError(), "ojv_blocked", 1, "replace_before_reuse"),
        (OjvTimeoutError(), "pjud_timeout", 1, "replace_before_reuse"),
        (OjvUpstreamChangedError(), "upstream_changed", 1, "healthy"),
    ],
)
async def test_login_uses_closed_taxonomy_and_only_expired_session_retries(
    error, error_code, expected_sessions, expected_disposition
):
    session = FakeSession(login_error=error)
    worker, sb, _pool, discover, _credential, session_factory, _ = make_worker(
        session=session
    )

    assert await worker.process_next() is True

    discover.assert_not_awaited()
    assert session_factory.call_count == expected_sessions
    assert _pool.release_familia_bundle.await_count == expected_sessions
    assert all(
        release.kwargs["disposition"] == expected_disposition
        for release in _pool.release_familia_bundle.await_args_list
    )
    final = [
        payload
        for name, payload in sb.calls
        if name == "finalize_pjud_import_discovery"
    ][0]
    assert final["p_summary"]["error_code"] == error_code


@pytest.mark.asyncio
async def test_safe_proxy_billing_signal_never_remints_or_finalizes_claim():
    session = FakeSession(login_error=ProxyBillingExhaustedError())
    worker, sb, pool, discover, *_ = make_worker(session=session)

    with pytest.raises(ProxyBillingExhaustedError):
        await worker.process_next()

    discover.assert_not_awaited()
    pool.release_familia_bundle.assert_awaited_once_with(
        pool.slot,
        disposition="replace_before_reuse",
        remint=False,
    )
    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)


@pytest.mark.asyncio
async def test_crash_leaves_claim_unfinalized_for_database_reclaim():
    worker, sb, pool, *_ = make_worker(discovery=AsyncMock(side_effect=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        await worker.process_next()

    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)
    pool.release_familia_bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_renews_exact_claim_while_discovery_is_running():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_discovery(*_args, **_kwargs):
        started.set()
        await finish.wait()
        return DiscoveryResult(candidates=[], page_count=1, status="ok")

    worker, sb, *_ = make_worker(discovery=AsyncMock(side_effect=slow_discovery))
    task = asyncio.create_task(worker.process_next())
    await started.wait()
    await asyncio.sleep(0.01)
    finish.set()
    assert await task is True

    renewals = [payload for name, payload in sb.calls if name == "renew_pjud_import_job_claim"]
    assert renewals
    assert renewals[0] == {
        "p_job_id": JOB["job_id"],
        "p_claim_token": JOB["claim_token"],
        "p_worker_id": "import-worker",
        "p_lease_seconds": 30,
    }


@pytest.mark.asyncio
async def test_lease_heartbeat_starts_before_slow_internal_credential_fetch():
    started = asyncio.Event()
    finish = asyncio.Event()

    async def slow_fetch(*_args):
        started.set()
        await finish.wait()
        return {
            "rut": "11111111-1",
            "password": "secret-value",
            "password_type": "clave_poder_judicial",
            "binding_version": "2026-08-23T12:00:00.000Z",
        }

    worker, sb, *_ = make_worker(credential=AsyncMock(side_effect=slow_fetch))
    task = asyncio.create_task(worker.process_next())
    await started.wait()
    await asyncio.sleep(0.01)
    assert any(name == "renew_pjud_import_job_claim" for name, _ in sb.calls)
    finish.set()
    assert await task is True


@pytest.mark.asyncio
async def test_lost_lease_cancels_discovery_and_stale_worker_never_finalizes():
    started = asyncio.Event()

    async def never_finishes(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    worker, sb, *_ = make_worker(discovery=AsyncMock(side_effect=never_finishes))
    sb.renew_result = False
    task = asyncio.create_task(worker.process_next())
    await started.wait()

    with pytest.raises(RuntimeError, match="import_job_claim_lost"):
        await asyncio.wait_for(task, timeout=0.1)

    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)


@pytest.mark.asyncio
async def test_lost_trial_claim_cancels_provider_work_without_remint_or_finalize():
    started = asyncio.Event()

    async def never_finishes(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    (
        worker,
        sb,
        pool,
        _discover,
        _normal_credential,
        trial_credential,
        proxy_usage,
        _session,
    ) = make_trial_worker(discovery=AsyncMock(side_effect=never_finishes))
    sb.renew_result = False
    task = asyncio.create_task(worker.process_trial_next(
        capability=SecretStr(TRIAL_CAPABILITY),
        runtime_generation=TRIAL_GENERATION,
    ))
    await started.wait()

    with pytest.raises(RuntimeError, match="import_job_claim_lost"):
        await asyncio.wait_for(task, timeout=0.1)

    names = [name for name, _payload in sb.calls]
    assert names.count("claim_pjud_trial_import_job") == 1
    assert "renew_pjud_trial_import_job_claim" in names
    assert "finalize_pjud_trial_import_discovery" not in names
    assert "claim_pjud_import_job" not in names
    scope = trial_credential.await_args.args[-1]
    pool.release_familia_bundle.assert_awaited_once_with(
        pool.slot,
        disposition="healthy",
        remint=False,
        trial_scope=scope,
    )
    assert all(call["trial_scope"] is scope for call in proxy_usage.calls)
    renewal_payload = next(
        payload
        for name, payload in sb.calls
        if name == "renew_pjud_trial_import_job_claim"
    )
    assert renewal_payload == {
        "p_expected_generation": TRIAL_GENERATION,
        "p_trial_grant_id": TRIAL_GRANT_ID,
        "p_job_id": JOB["job_id"],
        "p_claim_token": JOB["claim_token"],
        "p_worker_id": "import-worker",
        "p_lease_seconds": 30,
    }


@pytest.mark.asyncio
async def test_canonical_source_hash_ignores_caption_and_row_position():
    discovery = AsyncMock(return_value=DiscoveryResult(
        candidates=[candidate(caption="First"), candidate(caption="Changed")],
        page_count=1,
        status="ok",
    ))
    worker, sb, *_ = make_worker(discovery=discovery)

    await worker.process_next()

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert len(final["p_candidates"]) == 1
    assert final["p_candidates"][0]["caption"] == "Changed"


@pytest.mark.asyncio
async def test_candidate_allowlist_rejects_nested_or_oversized_values_before_rpc():
    bad = candidate().model_dump()
    bad["caption"] = {"raw": "forbidden"}
    discovery = AsyncMock(return_value=SimpleNamespace(candidates=[bad], page_count=1, status="ok"))
    worker, sb, *_ = make_worker(discovery=discovery)

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"]
    assert final[0]["p_candidates"] == []
    assert final[0]["p_summary"]["error_code"] == "invalid_candidate_payload"


@pytest.mark.asyncio
async def test_duplicate_rows_are_collapsed_before_candidate_limit_is_applied():
    result = SimpleNamespace(candidates=[candidate()] * 1001, page_count=2, status="ok")
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert len(final["p_candidates"]) == 1
    assert final["p_summary"] == {
        "status": "needs_selection",
        "pages": 2,
        "discovered": 1,
    }


@pytest.mark.asyncio
async def test_unique_candidate_overflow_keeps_first_thousand_as_selectable_review():
    result = SimpleNamespace(
        candidates=[candidate(case_number=f"C-{number}-2026") for number in range(1001)],
        page_count=51,
        status="ok",
    )
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert len(final["p_candidates"]) == 1000
    assert final["p_candidates"][0]["case_number"] == "C-0-2026"
    assert final["p_candidates"][-1]["case_number"] == "C-999-2026"
    assert final["p_summary"] == {
        "status": "needs_selection",
        "pages": 51,
        "discovered": 1000,
        "error_code": "candidate_limit_reached",
        "error_class": "limit",
    }


@pytest.mark.asyncio
async def test_payload_byte_limit_keeps_a_selectable_prefix_instead_of_failing_review():
    result = SimpleNamespace(
        candidates=[candidate(
            case_number=f"C-{number}-2026",
            court_label="C" * 200,
            tribunal_label="T" * 200,
            caption="A" * 500,
        ) for number in range(1000)],
        page_count=50,
        status="ok",
    )
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert 0 < len(final["p_candidates"]) < 1000
    assert len(json.dumps(final["p_candidates"], ensure_ascii=False).encode("utf-8")) <= 1_048_576
    assert final["p_summary"] == {
        "status": "needs_selection",
        "pages": 50,
        "discovered": len(final["p_candidates"]),
        "error_code": "candidate_limit_reached",
        "error_class": "limit",
    }


@pytest.mark.asyncio
async def test_incomplete_same_number_different_tribunal_labels_are_not_collapsed():
    result = DiscoveryResult(
        candidates=[
            candidate(tribunal_code=None, tribunal_label="1 Civil Santiago"),
            candidate(tribunal_code=None, tribunal_label="2 Civil Santiago"),
        ],
        page_count=1,
        status="ok",
    )
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    await worker.process_next()

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert len(final["p_candidates"]) == 2
    assert final["p_candidates"][0]["source_hash"] != final["p_candidates"][1]["source_hash"]


@pytest.mark.asyncio
async def test_discovery_concurrency_budget_is_independent_and_bounded():
    active = 0
    max_active = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def discover(*_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        entered.set()
        await release.wait()
        active -= 1
        return DiscoveryResult(candidates=[], page_count=1, status="ok")

    worker, *_ = make_worker(discovery=AsyncMock(side_effect=discover), concurrency=1)
    first = asyncio.create_task(worker.process_claimed(dict(JOB)))
    second = asyncio.create_task(worker.process_claimed({**JOB, "job_id": "98200000-0000-4000-8000-000000000042"}))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert max_active == 1
    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1


@pytest.mark.asyncio
async def test_concurrent_pollers_claim_only_after_entering_concurrency_budget():
    worker, sb, *_ = make_worker(concurrency=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_process(_job):
        entered.set()
        await release.wait()

    worker._process_claimed_with_budget = blocked_process
    first = asyncio.create_task(worker.process_next())
    await entered.wait()
    second = asyncio.create_task(worker.process_next())
    await asyncio.sleep(0.01)

    assert [name for name, _ in sb.calls].count("claim_pjud_import_job") == 1
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_cancelling_outer_processing_cancels_operation_and_heartbeat():
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def cancellable_discovery(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    worker, sb, pool, *_ = make_worker(discovery=AsyncMock(side_effect=cancellable_discovery))
    task = asyncio.create_task(worker.process_next())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    renewals_at_cancel = [name for name, _ in sb.calls].count("renew_pjud_import_job_claim")
    await asyncio.sleep(0.01)

    assert stopped.is_set()
    assert [name for name, _ in sb.calls].count("renew_pjud_import_job_claim") == renewals_at_cancel
    assert not any(name == "finalize_pjud_import_discovery" for name, _ in sb.calls)
    pool.release_familia_bundle.assert_awaited_once_with(
        pool.slot, disposition="healthy", remint=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_number", "C" * 129),
        ("court_label", "C" * 201),
        ("tribunal_label", "T" * 201),
        ("libro", "L" * 81),
        ("upstream_status", "S" * 101),
    ],
)
async def test_sql_field_limits_are_rejected_deterministically_before_finalize(field, value):
    result = DiscoveryResult(
        candidates=[candidate(**{field: value})], page_count=1, status="ok",
    )
    worker, sb, *_ = make_worker(discovery=AsyncMock(return_value=result))

    assert await worker.process_next() is True

    final = [payload for name, payload in sb.calls if name == "finalize_pjud_import_discovery"][0]
    assert final["p_candidates"] == []
    assert final["p_summary"]["error_code"] == "invalid_candidate_payload"


@pytest.mark.asyncio
async def test_engine_import_poll_does_not_use_paid_sync_batch_semaphore():
    from worker.engine import SyncEngine

    import_worker = SimpleNamespace(process_next=AsyncMock(return_value=False))
    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {}
    engine._import_worker = import_worker

    assert await engine.process_import_job() is False
    import_worker.process_next.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_main_loop_import_poll_contains_failure_without_crashing_paid_sync(caplog):
    from worker.__main__ import safe_process_import_job

    engine = SimpleNamespace(
        process_import_job=AsyncMock(side_effect=RuntimeError("secret RIT C-10-2026")),
    )
    metrics = SimpleNamespace(record_error=MagicMock())

    assert await safe_process_import_job(engine, metrics) is False
    metrics.record_error.assert_called_once_with("infra")
    assert "secret" not in caplog.text
    assert "C-10-2026" not in caplog.text


@pytest.mark.asyncio
async def test_main_loop_import_billing_signal_trips_global_cost_controls():
    from worker.__main__ import safe_process_import_job

    engine = SimpleNamespace(
        process_import_job=AsyncMock(side_effect=ProxyBillingExhaustedError()),
        _proxy_control=SimpleNamespace(trip_billing_exhausted=AsyncMock()),
        _backoff=SimpleNamespace(open_permanently=MagicMock()),
    )
    metrics = SimpleNamespace(record_error=MagicMock())

    assert await safe_process_import_job(engine, metrics) is False
    engine._proxy_control.trip_billing_exhausted.assert_awaited_once()
    engine._backoff.open_permanently.assert_called_once_with("billing_exhausted")


@pytest.mark.asyncio
async def test_import_completion_log_is_aggregate_only(caplog):
    caplog.set_level("INFO", logger="worker.import_jobs")
    worker, *_ = make_worker()
    await worker.process_next()

    assert JOB["job_id"] not in caplog.text
    assert JOB["law_firm_id"] not in caplog.text
    assert "status=ok" in caplog.text
    assert "pages=1" in caplog.text
    assert "count=1" in caplog.text


@pytest.mark.asyncio
async def test_import_poll_loop_is_independent_and_shutdown_cancels_inflight_work():
    from tests.helpers import legacy_runtime_fence
    from worker.__main__ import run_import_discovery_loop

    shutdown = asyncio.Event()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def process():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    engine = SimpleNamespace(process_import_job=AsyncMock(side_effect=process))
    metrics = SimpleNamespace(record_error=MagicMock())
    loop = asyncio.create_task(
        run_import_discovery_loop(engine, metrics, shutdown, runtime_fence=legacy_runtime_fence(), poll_interval=0.001),
    )
    await entered.wait()
    shutdown.set()
    loop.cancel()
    await asyncio.gather(loop, return_exceptions=True)

    assert cancelled.is_set()


def test_import_budget_reserves_at_least_one_session_for_public_sync():
    from worker.__main__ import public_sync_concurrency

    assert public_sync_concurrency(3, imports_enabled=True) == 2
    assert public_sync_concurrency(2, imports_enabled=True) == 1


def test_import_budget_is_not_reserved_while_import_flag_is_disabled():
    from worker.__main__ import public_sync_concurrency

    assert public_sync_concurrency(3, imports_enabled=False) == 3
    assert public_sync_concurrency(1, imports_enabled=False) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["https://app.test", "https://app.test/", "https://app.test///"])
async def test_import_credential_url_has_one_path_separator(monkeypatch, base_url):
    import httpx
    from worker.engine import SyncEngine

    seen = []
    path = f"/api/internal/pjud-import/credentials/{JOB['credential_id']}/decrypt"

    def respond(request):
        seen.append(request)
        # Match Next's normalization redirect for double slash paths.
        status = 404 if request.url.path == path else 308
        return httpx.Response(status, json={"error": "unavailable"})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(respond), **kw,
    ))
    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {}
    engine._config = SimpleNamespace(VERCEL_APP_URL=base_url, INTERNAL_CREDENTIALS_API_KEY="test-only")
    result = await engine._get_import_credential(
        JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"], JOB["claim_token"], "import-worker",
    )
    assert result is None
    assert len(seen) == 1
    assert str(seen[0].url) == "https://app.test" + path
    assert seen[0].headers["authorization"] == "Bearer test-only"
    assert seen[0].headers["x-pjud-import-claim-token"] == JOB["claim_token"]


@pytest.mark.asyncio
async def test_import_credential_never_follows_redirect_with_internal_key(monkeypatch):
    import httpx
    from worker.engine import ImportCredentialInfrastructureError, SyncEngine

    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(308, headers={"location": "https://other.test/credential"})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_class(
        transport=httpx.MockTransport(respond), **kw,
    ))
    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {}
    engine._config = SimpleNamespace(VERCEL_APP_URL="https://app.test", INTERNAL_CREDENTIALS_API_KEY="test-only")
    with pytest.raises(ImportCredentialInfrastructureError):
        await engine._get_import_credential(
            JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"], JOB["claim_token"], "import-worker",
        )
    assert len(seen) == 1
    assert seen[0].startswith("https://app.test/")


@pytest.mark.asyncio
async def test_import_credential_fetch_treats_internal_outage_as_retryable():
    from worker.engine import ImportCredentialInfrastructureError, SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {}
    engine._call_app_internal = AsyncMock(return_value=None)

    with pytest.raises(ImportCredentialInfrastructureError):
        await engine._get_import_credential(
            JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
            JOB["claim_token"], "import-worker",
        )


@pytest.mark.asyncio
async def test_import_credential_fetch_keeps_terminal_not_found_distinct():
    from worker.engine import SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {}
    engine._call_app_internal = AsyncMock(
        return_value=SimpleNamespace(status_code=404, json=lambda: {"error": "not found"}),
    )

    assert await engine._get_import_credential(
        JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
        JOB["claim_token"], "import-worker",
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"rut": "11111111-1"}, {
    "rut": "11111111-1", "password": "secret", "password_type": "clave_unica",
}, {
    "rut": "11111111-1", "password": "secret",
    "password_type": "clave_poder_judicial", "binding_version": "not-a-date",
}])
async def test_import_credential_fetch_retries_malformed_200_contract(payload):
    from worker.engine import ImportCredentialInfrastructureError, SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {}
    engine._call_app_internal = AsyncMock(
        return_value=SimpleNamespace(status_code=200, json=lambda: payload),
    )

    with pytest.raises(ImportCredentialInfrastructureError):
        await engine._get_import_credential(
            JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
            JOB["claim_token"], "import-worker",
        )


@pytest.mark.asyncio
async def test_import_credential_fetch_uses_import_specific_claim_boundary():
    from worker.engine import SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {}
    engine._call_app_internal = AsyncMock(
        return_value=SimpleNamespace(status_code=404, json=lambda: {"error": "closed"}),
    )

    await engine._get_import_credential(
        JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
        JOB["claim_token"], "import-worker",
    )

    engine._call_app_internal.assert_awaited_once_with(
        "GET",
        f"/api/internal/pjud-import/credentials/{JOB['credential_id']}/decrypt",
        "Import decrypt endpoint",
        law_firm_id=JOB["law_firm_id"],
        extra_headers={
            "X-Pjud-Import-Job-Id": JOB["job_id"],
            "X-Pjud-Import-Claim-Token": JOB["claim_token"],
            "X-Pjud-Import-Worker-Id": "import-worker",
        },
    )


@pytest.mark.asyncio
async def test_trial_import_credential_relay_adds_only_exact_capability_and_grant_headers():
    """Catch a trial decrypt losing grant authority or falling back to normal relay."""
    from worker.engine import SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {"x-pjud-runtime-generation": TRIAL_GENERATION}
    engine._call_app_internal = AsyncMock(
        return_value=SimpleNamespace(status_code=404, json=lambda: {"error": "closed"}),
    )
    scope = trial_scope()

    assert await engine._get_trial_import_credential(
        JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
        JOB["claim_token"], "import-worker", scope,
    ) is None

    engine._call_app_internal.assert_awaited_once_with(
        "GET",
        f"/api/internal/pjud-import/credentials/{JOB['credential_id']}/decrypt",
        "Trial import decrypt endpoint",
        law_firm_id=JOB["law_firm_id"],
        extra_headers={
            "X-Pjud-Import-Job-Id": JOB["job_id"],
            "X-Pjud-Import-Claim-Token": JOB["claim_token"],
            "X-Pjud-Import-Worker-Id": "import-worker",
            "X-Pjud-Import-Credential-Updated-At": (
                "2026-08-23T12:00:00+00:00"
            ),
            "x-pjud-runtime-trial-capability": TRIAL_CAPABILITY,
            "X-Pjud-Runtime-Trial-Grant-Id": TRIAL_GRANT_ID,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("position", "replacement"),
    [
        (0, "98200000-0000-4000-8000-000000000022"),
        (1, "98200000-0000-4000-8000-000000000002"),
        (2, "98200000-0000-4000-8000-000000000042"),
        (3, "98200000-0000-4000-8000-000000000098"),
        (4, "other-worker"),
    ],
)
async def test_trial_import_credential_relay_rejects_any_wrong_tuple_before_http(
    position, replacement,
):
    """Catch one field being trusted independently from the immutable trial scope."""
    from worker.engine import ImportCredentialInfrastructureError, SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {"x-pjud-runtime-generation": TRIAL_GENERATION}
    engine._call_app_internal = AsyncMock()
    args = [
        JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
        JOB["claim_token"], "import-worker",
    ]
    args[position] = replacement

    with pytest.raises(
        ImportCredentialInfrastructureError,
        match="trial_import_scope_mismatch",
    ):
        await engine._get_trial_import_credential(*args, trial_scope())

    engine._call_app_internal.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_import_credential_relay_rejects_wrong_runtime_generation_before_http():
    from worker.engine import ImportCredentialInfrastructureError, SyncEngine

    engine = SyncEngine.__new__(SyncEngine)
    engine._runtime_headers = {
        "x-pjud-runtime-generation": "99999999-9999-4999-8999-999999999999",
    }
    engine._call_app_internal = AsyncMock()

    with pytest.raises(
        ImportCredentialInfrastructureError,
        match="trial_import_scope_mismatch",
    ):
        await engine._get_trial_import_credential(
            JOB["credential_id"], JOB["law_firm_id"], JOB["job_id"],
            JOB["claim_token"], "import-worker", trial_scope(),
        )

    engine._call_app_internal.assert_not_awaited()
