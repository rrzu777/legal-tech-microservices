from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from worker.config import WorkerConfig
from worker.import_jobs import ImportDiscoveryWorker


BASE = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_KEY": "service-key",
}


def test_import_and_excel_flags_fail_closed_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_PJUD_MY_CAUSES_IMPORT", raising=False)
    monkeypatch.delenv("ENABLE_PJUD_MY_CAUSES_EXCEL", raising=False)
    config = WorkerConfig(**BASE)
    assert config.ENABLE_PJUD_MY_CAUSES_IMPORT is False
    assert config.ENABLE_PJUD_MY_CAUSES_EXCEL is False


@pytest.mark.parametrize("value", ["false"])
def test_import_flag_accepts_only_explicit_false_values_as_disabled(value):
    config = WorkerConfig(**BASE, ENABLE_PJUD_MY_CAUSES_IMPORT=value)
    assert config.ENABLE_PJUD_MY_CAUSES_IMPORT is False


def test_import_flag_can_be_enabled_explicitly():
    config = WorkerConfig(**BASE, ENABLE_PJUD_MY_CAUSES_IMPORT="true")
    assert config.ENABLE_PJUD_MY_CAUSES_IMPORT is True


@pytest.mark.parametrize("value", ["0", "no", "1", "yes", "on", "TRUE", 1])
def test_import_flag_rejects_noncanonical_truthy_values(value):
    with pytest.raises(ValidationError, match="pjud_my_causes_import_flag_must_be_literal"):
        WorkerConfig(**BASE, ENABLE_PJUD_MY_CAUSES_IMPORT=value)


def test_excel_endpoint_cannot_be_enabled_even_by_environment():
    with pytest.raises(ValidationError, match="pjud_my_causes_excel_must_remain_disabled"):
        WorkerConfig(**BASE, ENABLE_PJUD_MY_CAUSES_EXCEL="true")


@pytest.mark.asyncio
async def test_disabled_worker_never_claims_but_already_claimed_work_can_finish():
    supabase = AsyncMock()
    worker = ImportDiscoveryWorker(
        supabase=supabase,
        pool=AsyncMock(),
        worker_id="worker-1",
        fetch_credential=AsyncMock(),
        enabled=False,
    )

    assert await worker.process_next() is False
    supabase.rpc.assert_not_called()

    worker._process_claimed_with_budget = AsyncMock()
    await worker.process_claimed({
        "status": "acquired",
        "job_id": "98200000-0000-4000-8000-000000000041",
        "law_firm_id": "98200000-0000-4000-8000-000000000001",
        "credential_id": "98200000-0000-4000-8000-000000000021",
        "matters": ["civil"],
        "include_closed": False,
        "claim_token": "98200000-0000-4000-8000-000000000099",
        "lease_expires_at": "2026-08-23T13:00:00+00:00",
    })
    worker._process_claimed_with_budget.assert_awaited_once()
