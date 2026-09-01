import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch
from zoneinfo import ZoneInfo


OFFICE_NOW = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
NIGHT_NOW = datetime(2026, 3, 2, 22, 0, tzinfo=ZoneInfo("America/Santiago"))


def _mock_config(worker_id="test-worker", batch_size=10):
    config = MagicMock()
    config.WORKER_ID = worker_id
    config.BATCH_SIZE = batch_size
    config.PJUD_OFF_HOURS_VALIDATION_ONCE = False
    config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS = False
    return config


class TestScheduler:
    @pytest.mark.asyncio
    async def test_preflight_verifies_rpc_without_claiming_cases(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        await Scheduler(_mock_config(), mock_sb).verify_claim_contract(now=OFFICE_NOW)

        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 0,
            "p_now": OFFICE_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_periodic_reconciliation_runs_without_claiming_outside_office(self):
        """The maintenance RPC must not need a case claim or caller cutoff."""
        from worker.scheduler import RECONCILE_INTERVAL_S, Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{
            "reconciled_count": 1,
            "historical_unowned_count": 2,
        }])
        scheduler = Scheduler(_mock_config(), mock_sb)
        scheduler._last_reconciliation_monotonic = 0.0

        with patch(
            "worker.scheduler.time.monotonic",
            return_value=RECONCILE_INTERVAL_S,
        ):
            assert await scheduler.reconcile_stale_runs() == {
                "reconciled_count": 1,
                "historical_unowned_count": 2,
            }

        mock_sb.rpc.assert_called_once_with(
            "reconcile_stale_pjud_sync_runs", {},
        )

    @pytest.mark.asyncio
    async def test_first_unforced_reconciliation_runs_then_is_interval_bounded(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{
            "reconciled_count": 0,
            "historical_unowned_count": 0,
        }])
        scheduler = Scheduler(_mock_config(), mock_sb)

        assert await scheduler.reconcile_stale_runs() == {
            "reconciled_count": 0,
            "historical_unowned_count": 0,
        }
        assert await scheduler.reconcile_stale_runs() is None
        mock_sb.rpc.assert_called_once_with("reconcile_stale_pjud_sync_runs", {})

    @pytest.mark.asyncio
    async def test_get_next_batch_builds_correct_query(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[])

        scheduler = Scheduler(_mock_config(), mock_sb)
        result = await scheduler.get_next_batch(now=OFFICE_NOW)

        assert result == []
        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 10,
            "p_now": OFFICE_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_filters_by_priority_during_office_hours(self):
        from worker.scheduler import is_scheduled_processing_window

        dt_office = datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert is_scheduled_processing_window(dt_office) is True

        dt_night = datetime(2026, 3, 2, 22, 0, tzinfo=ZoneInfo("America/Santiago"))
        assert is_scheduled_processing_window(dt_night) is False

    @pytest.mark.asyncio
    async def test_outside_office_returns_before_querying_or_claiming(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        scheduler = Scheduler(_mock_config(), mock_sb)

        assert await scheduler.get_next_batch(now=NIGHT_NOW) == []
        mock_sb.rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_shot_validation_claims_at_most_one_case_outside_office(self):
        from worker.scheduler import Scheduler

        config = _mock_config(batch_size=10)
        config.PJUD_OFF_HOURS_VALIDATION_ONCE = True
        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"id": "case-1"}])

        assert await Scheduler(config, mock_sb).get_next_batch(now=NIGHT_NOW) == [
            {"id": "case-1"},
        ]
        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 1,
            "p_now": NIGHT_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_temporary_override_claims_normal_batch_outside_office(self):
        from worker.scheduler import Scheduler

        config = _mock_config(batch_size=10)
        config.PJUD_PROCESS_OUTSIDE_OFFICE_HOURS = True
        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"id": "case-1"}])

        assert await Scheduler(config, mock_sb).get_next_batch(now=NIGHT_NOW) == [
            {"id": "case-1"},
        ]
        mock_sb.rpc.assert_called_once_with("claim_pjud_sync_cases", {
            "p_worker_id": "test-worker",
            "p_limit": 10,
            "p_now": NIGHT_NOW.isoformat(),
        })

    @pytest.mark.asyncio
    async def test_marks_batch_with_worker_id(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain

        fake_cases = [{"id": "case-1"}, {"id": "case-2"}]
        chain.execute.return_value = MagicMock(data=fake_cases)

        config = _mock_config()
        scheduler = Scheduler(config, mock_sb)
        result = await scheduler.get_next_batch(now=OFFICE_NOW)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_returns_only_rows_won_by_atomic_claim(self):
        from worker.scheduler import Scheduler

        mock_sb = MagicMock()
        chain = MagicMock()
        mock_sb.rpc.return_value = chain
        chain.execute.return_value = MagicMock(data=[{"id": "case-2"}])

        result = await Scheduler(_mock_config(), mock_sb).get_next_batch(now=OFFICE_NOW)

        assert result == [{"id": "case-2"}]

    def test_aware_datetime_is_converted_to_santiago(self):
        from worker.scheduler import is_scheduled_processing_window

        # 19:00 UTC = 15:00 Santiago en agosto; evaluar 19:00 sin convertir
        # daría falsamente fuera de horario.
        assert is_scheduled_processing_window(
            datetime(2026, 8, 6, 19, 0, tzinfo=timezone.utc)
        ) is True

    @pytest.mark.asyncio
    async def test_release_exact_claim_uses_only_v2(self):
        from worker.scheduler import Scheduler
        from tests.helpers import GENERATION_A, GENERATION_B
        sb = MagicMock()
        claims = [{"case_id": GENERATION_A, "claim_token": GENERATION_B}]
        await Scheduler(_mock_config(), sb).release_batch(claims)
        sb.rpc.assert_called_once_with("release_pjud_sync_claims_v2", {
            "p_worker_id": "test-worker", "p_claims": claims,
        })
        sb.from_.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [
        RuntimeError({"code": "PGRST202", "message": "missing"}),
        RuntimeError({"code": "42501", "message": "fenced"}),
        TimeoutError("timeout"), RuntimeError("database unavailable"),
    ])
    async def test_release_error_never_retries_fetches_or_falls_back(self, error):
        from worker.scheduler import Scheduler
        from tests.helpers import GENERATION_A, GENERATION_B
        sb = MagicMock()
        sb.rpc.return_value.execute.side_effect = error
        with pytest.raises(type(error)) as raised:
            await Scheduler(_mock_config(), sb).release_batch([
                {"case_id": GENERATION_A, "claim_token": GENERATION_B},
            ])
        assert str(raised.value) == str(error)
        assert sb.rpc.call_count == 1
        sb.from_.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("claims", [
        None, {}, ["case-1"], [{"case_id": "invalid", "claim_token": "invalid"}],
        [{"case_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}],
        [{"case_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "claim_token": None}],
        [{"case_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          "claim_token": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "extra": True}],
        [{"case_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          "claim_token": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}] * 2,
        [{"case_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          "claim_token": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}] * 101,
    ])
    async def test_release_rejects_invalid_whole_batch_before_any_rpc(self, claims):
        from worker.scheduler import Scheduler
        sb = MagicMock()
        with pytest.raises(ValueError, match="^invalid_pjud_release_claims$"):
            await Scheduler(_mock_config(), sb).release_batch(claims)
        sb.rpc.assert_not_called()
        sb.from_.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_release_is_local_noop(self):
        from worker.scheduler import Scheduler
        sb = MagicMock()
        await Scheduler(_mock_config(), sb).release_batch([])
        sb.rpc.assert_not_called()
        sb.from_.assert_not_called()
