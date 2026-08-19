#!/usr/bin/env python3
"""Evaluate persistent resource alerts and deliver them through Telegram."""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sys
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

try:
    from .alert_policy import (
        AlertEvent,
        advance_state,
        evaluate_rules,
        load_state,
        record_delivery_failure,
        record_delivery_success,
        save_state,
    )
    from .resource_metrics import ResourceSnapshot, collect_resource_snapshot
except ImportError:  # Flat installation in /opt/legaltech-monitoring.
    from alert_policy import (
        AlertEvent,
        advance_state,
        evaluate_rules,
        load_state,
        record_delivery_failure,
        record_delivery_success,
        save_state,
    )
    from resource_metrics import ResourceSnapshot, collect_resource_snapshot


class TelegramDeliveryError(RuntimeError):
    """A deliberately sanitized Telegram delivery failure."""


class TelegramTransport:
    def __init__(
        self,
        token: str,
        chat_id: str,
        opener: Callable[..., Any],
        timeout: float = 5.0,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram credentials are required")
        self._token = token
        self._chat_id = chat_id
        self._opener = opener
        self._timeout = timeout

    def send(self, message: str) -> None:
        response = None
        close_attempted = False
        try:
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            payload = json.dumps(
                {"chat_id": self._chat_id, "text": message},
                separators=(",", ":"),
            ).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            response = self._opener(request, timeout=self._timeout)
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise TelegramDeliveryError("Telegram delivery failed")
            body = json.loads(response.read().decode("utf-8"))
            if not isinstance(body, dict) or body.get("ok") is not True:
                raise TelegramDeliveryError("Telegram delivery failed")
            close = getattr(response, "close", None)
            if callable(close):
                close_attempted = True
                close()
        except Exception as error:
            if not close_attempted:
                _safe_close(response if response is not None else error)
            raise TelegramDeliveryError("Telegram delivery failed") from None


class CliUsageError(Exception):
    pass


class SecretSafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError("Invalid monitoring arguments")


def resolve_hermes_user_slice() -> str:
    return f"user-{pwd.getpwnam('hermes').pw_uid}.slice"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_transport_factory(token: str, chat_id: str) -> TelegramTransport:
    return TelegramTransport(token, chat_id, urllib.request.urlopen)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    collect: Callable[..., ResourceSnapshot] = collect_resource_snapshot,
    clock: Callable[[], datetime] = utc_now,
    state_loader: Callable[[Path], dict[str, Any]] = load_state,
    state_writer: Callable[[Path, dict[str, Any]], None] = save_state,
    transport_factory: Callable[[str, str], Any] = default_transport_factory,
    slice_resolver: Callable[[], str] = resolve_hermes_user_slice,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = SecretSafeArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="evaluate once and exit")
    modes.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate without network or state mutation",
    )
    modes.add_argument(
        "--test-alert",
        action="store_true",
        help="send one clearly labeled synthetic alert",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("/var/lib/legaltech-monitor"),
        help="persistent alert state directory",
    )
    parser.add_argument(
        "--hermes-user-slice",
        help="resolved systemd user slice (defaults to the local hermes UID)",
    )
    try:
        arguments = parser.parse_args(argv)
    except CliUsageError:
        stderr.write("Invalid monitoring arguments\n")
        return 2

    environment = os.environ if environ is None else environ
    token = environment.get("LEGALTECH_TELEGRAM_BOT_TOKEN")
    chat_id = environment.get("LEGALTECH_TELEGRAM_CHAT_ID")

    if arguments.test_alert:
        if not token or not chat_id:
            stderr.write("Telegram credentials are required\n")
            return 2
        try:
            transport_factory(token, chat_id).send(
                "JurisTrack synthetic monitoring test"
            )
        except Exception:
            stderr.write("Telegram delivery failed\n")
            return 1
        _write_json(stdout, {"status": "synthetic-alert-sent"})
        return 0

    now = clock()
    state_path = arguments.state_dir / "state.json"
    try:
        state = state_loader(state_path)
        hermes_user_slice = arguments.hermes_user_slice or slice_resolver()
        sample = collect(hermes_user_slice=hermes_user_slice)
        events, candidate = advance_state(evaluate_rules(sample, state), state, now)
    except Exception:
        stderr.write("Resource monitoring evaluation failed\n")
        return 1

    if arguments.dry_run:
        _write_json(
            stdout,
            {"dry_run": True, "events": [asdict(event) for event in events]},
        )
        return 0

    try:
        state_writer(state_path, candidate)
    except Exception:
        stderr.write("Monitoring state write failed\n")
        return 1

    delivery_failed = False
    transport = None
    if events and token and chat_id:
        try:
            transport = transport_factory(token, chat_id)
        except Exception:
            transport = None

    for event in events:
        delivered = False
        if transport is not None:
            try:
                transport.send(_format_event(event))
                delivered = True
            except Exception:
                delivered = False
        if delivered:
            candidate = record_delivery_success(candidate, event, now)
        else:
            delivery_failed = True
            candidate = record_delivery_failure(candidate, event)
            stderr.write("Telegram delivery failed\n")
        try:
            state_writer(state_path, candidate)
        except Exception:
            stderr.write("Monitoring state write failed\n")
            return 1

    _write_json(
        stdout,
        {"dry_run": False, "events": [asdict(event) for event in events]},
    )
    return 1 if delivery_failed else 0


def _format_event(event: AlertEvent) -> str:
    return f"JurisTrack [{event.severity.upper()}] {event.message} ({event.kind})"


def _safe_close(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _write_json(output: TextIO, value: dict[str, Any]) -> None:
    json.dump(value, output, sort_keys=True, separators=(",", ":"))
    output.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
