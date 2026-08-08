"""Tests for Telegram alerting."""
import fcntl
import json
import logging
import multiprocessing
import os
import queue
import stat
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from starlette.datastructures import State


def _claim_event_in_process(path, event, ready_queue, result_queue):
    from app.alert_cooldown_store import AlertCooldownStore

    ready_queue.put(event)
    try:
        result_queue.put((event, AlertCooldownStore(path).claim(event, 300), None))
    except BaseException as exc:
        result_queue.put((event, None, repr(exc)))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()


class TestTelegramAlerter:
    def test_future_event_timestamp_fails_open_and_is_repaired(self, tmp_path, caplog):
        from app.alert_cooldown_store import AlertCooldownStore

        cooldown_path = tmp_path / "alert-cooldowns.json"
        cooldown_path.write_text(json.dumps({"pool_unavailable": 1_800_000_001.0}))
        store = AlertCooldownStore(str(cooldown_path))

        with (
            caplog.at_level(logging.WARNING),
            patch("app.alert_cooldown_store.time.time", return_value=1_800_000_000.0),
        ):
            assert store.claim("pool_unavailable", 300) is True

        assert json.loads(cooldown_path.read_text()) == {
            "pool_unavailable": 1_800_000_000.0
        }
        assert "future" in caplog.text.lower()

    def test_claim_is_atomic_across_processes_and_preserves_distinct_events(
        self, tmp_path
    ):
        cooldown_path = tmp_path / "alert-cooldowns.json"
        lock_path = tmp_path / "alert-cooldowns.json.lock"
        lock_path.touch(mode=0o640)
        context = multiprocessing.get_context("spawn")
        ready_queue = context.Queue()
        result_queue = context.Queue()
        events = ["pool_unavailable", "pool_unavailable", "mint_failed"]
        processes = [
            context.Process(
                target=_claim_event_in_process,
                args=(str(cooldown_path), event, ready_queue, result_queue),
            )
            for event in events
        ]
        results = []

        with lock_path.open("r+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                for process in processes:
                    process.start()
                for _ in events:
                    ready_queue.get(timeout=10)
                try:
                    results.append(result_queue.get(timeout=1))
                except queue.Empty:
                    pass
                result_arrived_while_locked = bool(results)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

        try:
            while len(results) < len(events):
                results.append(result_queue.get(timeout=10))
            for process in processes:
                process.join(timeout=10)
            assert all(process.exitcode == 0 for process in processes)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)

        assert result_arrived_while_locked is False
        assert all(error is None for _, _, error in results)
        pool_claims = [
            claimed for event, claimed, _ in results if event == "pool_unavailable"
        ]
        mint_claims = [claimed for event, claimed, _ in results if event == "mint_failed"]
        assert sorted(pool_claims) == [False, True]
        assert mint_claims == [True]
        assert set(json.loads(cooldown_path.read_text())) == {
            "pool_unavailable",
            "mint_failed",
        }

    def test_store_fsyncs_file_and_parent_directory(self, tmp_path, monkeypatch):
        from app import alert_cooldown_store

        synced_types = []
        real_fsync = alert_cooldown_store.os.fsync

        def record_fsync(fd):
            synced_types.append(stat.S_IFMT(os.fstat(fd).st_mode))
            real_fsync(fd)

        monkeypatch.setattr(alert_cooldown_store.os, "fsync", record_fsync)
        store = alert_cooldown_store.AlertCooldownStore(
            str(tmp_path / "alert-cooldowns.json")
        )

        assert store.claim("pool_unavailable", 300) is True
        assert stat.S_IFREG in synced_types
        assert stat.S_IFDIR in synced_types

    @pytest.mark.asyncio
    async def test_event_cooldown_survives_process_restart(self, tmp_path, caplog):
        from app.alert_cooldown_store import AlertCooldownStore
        from app.alerting import TelegramAlerter

        cooldown_path = tmp_path / "alert-cooldowns.json"
        first = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            cooldown_seconds=300,
            event_cooldown_store=AlertCooldownStore(str(cooldown_path)),
        )
        second = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            cooldown_seconds=300,
            event_cooldown_store=AlertCooldownStore(str(cooldown_path)),
        )

        try:
            with (
                caplog.at_level(logging.INFO),
                patch.object(first, "_send", new_callable=AsyncMock),
                patch.object(second, "_send", new_callable=AsyncMock),
            ):
                assert await first.alert_event("pool_unavailable", "pool exhausted") is True
                assert await second.alert_event("pool_unavailable", "pool exhausted") is False
            assert "alert cooldown store" in caplog.text.lower()
        finally:
            await first.close()
            await second.close()

    @pytest.mark.asyncio
    async def test_event_cooldowns_remain_independent_after_restart(self, tmp_path):
        from app.alert_cooldown_store import AlertCooldownStore
        from app.alerting import TelegramAlerter

        cooldown_path = tmp_path / "alert-cooldowns.json"
        first = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            cooldown_seconds=300,
            event_cooldown_store=AlertCooldownStore(str(cooldown_path)),
        )
        second = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            cooldown_seconds=300,
            event_cooldown_store=AlertCooldownStore(str(cooldown_path)),
        )

        try:
            with (
                patch.object(first, "_send", new_callable=AsyncMock),
                patch.object(second, "_send", new_callable=AsyncMock),
            ):
                assert await first.alert_event("pool_unavailable", "pool exhausted") is True
                assert await second.alert_event("mint_failed", "browser unavailable") is True
                assert await second.alert_event("pool_unavailable", "pool exhausted") is False
        finally:
            await first.close()
            await second.close()

    @pytest.mark.asyncio
    async def test_corrupt_cooldown_store_fails_open_logs_and_repairs_without_detail(
        self, tmp_path, caplog
    ):
        from app.alert_cooldown_store import AlertCooldownStore
        from app.alerting import TelegramAlerter

        cooldown_path = tmp_path / "alert-cooldowns.json"
        cooldown_path.write_text("{not-json")
        detail = "postgres://ops:secret@database/pjud"
        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            cooldown_seconds=300,
            event_cooldown_store=AlertCooldownStore(str(cooldown_path)),
        )

        try:
            with (
                caplog.at_level(logging.WARNING),
                patch.object(alerter, "_send", new_callable=AsyncMock),
            ):
                assert await alerter.alert_event("pool_unavailable", detail) is True

            persisted = cooldown_path.read_text()
            assert json.loads(persisted)["pool_unavailable"] > 0
            assert detail not in persisted
            assert stat.S_IMODE(cooldown_path.stat().st_mode) == 0o640
            lock_path = cooldown_path.with_name(f"{cooldown_path.name}.lock")
            assert stat.S_IMODE(lock_path.stat().st_mode) == 0o640
            assert "corrupt" in caplog.text.lower()
        finally:
            await alerter.close()

    @pytest.mark.asyncio
    async def test_alert_fires_when_blocked_rate_exceeds_threshold(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(4):
            api_metrics.record_blocked("search")

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            mock_send.assert_awaited_once()
            assert "blocked" in mock_send.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_alert_does_not_fire_below_threshold(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        api_metrics.record_blocked("search")  # 10% < 30%

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alert_respects_cooldown(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        for _ in range(10):
            api_metrics.record_request("search")
        for _ in range(5):
            api_metrics.record_blocked("search")

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            await alerter.check_and_alert()
            assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_no_alert_when_no_requests(self):
        from app.alerting import TelegramAlerter
        from app.metrics import api_metrics
        api_metrics.reset()

        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
            blocked_rate_threshold=0.3,
            cooldown_seconds=60,
        )

        with patch.object(alerter, "_send", new_callable=AsyncMock) as mock_send:
            await alerter.check_and_alert()
            mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alerter_close_cleans_up_client(self):
        from app.alerting import TelegramAlerter
        alerter = TelegramAlerter(
            bot_token="fake-token",
            chat_id="-123456",
        )
        assert alerter._client is not None
        await alerter.close()
        assert alerter._client.is_closed
