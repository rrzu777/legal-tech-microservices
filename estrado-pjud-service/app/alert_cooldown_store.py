import fcntl
import json
import logging
import math
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


logger = logging.getLogger(__name__)

DEFAULT_ALERT_COOLDOWN_STORE_PATH = "/var/lib/estrado-pjud/alert-cooldowns.json"


class AlertCooldownStore:
    """Persistent per-event cooldown claims for operational alerts."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock_path = self._path.with_name(f"{self._path.name}.lock")

    def claim(self, event: str, cooldown_seconds: int) -> bool:
        """Persist a due event and return whether its alert may be attempted."""
        return self.reserve(event, cooldown_seconds) is not None

    def reserve(self, event: str, cooldown_seconds: int) -> float | None:
        """Reserve a due event and return the exact token used for rollback."""
        with self._interprocess_lock():
            now = time.time()
            cooldowns = self._load()
            last_sent_at = cooldowns.get(event)
            if last_sent_at is not None:
                if last_sent_at > now:
                    logger.warning(
                        "Alert cooldown timestamp is in the future; failing open"
                    )
                elif now - last_sent_at < cooldown_seconds:
                    return None

            cooldowns[event] = now
            self._write(cooldowns)
            return now

    def rollback(self, event: str, reservation: float) -> None:
        """Remove only this failed delivery reservation, never a newer claim."""
        with self._interprocess_lock():
            cooldowns = self._load()
            if cooldowns.get(event) != reservation:
                return
            del cooldowns[event]
            self._write(cooldowns)

    @contextmanager
    def _interprocess_lock(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                lock_fd = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o660,
                )
            except FileExistsError:
                try:
                    lock_fd = os.open(self._lock_path, os.O_RDWR)
                except FileNotFoundError:
                    # Otro proceso pudo reemplazar el lock entre ambas
                    # llamadas. Repetir mantiene el create/open atómico.
                    continue
                created = False
            else:
                created = True
            break
        try:
            if created:
                os.fchmod(lock_fd, 0o660)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

    def _load(self) -> dict[str, float]:
        try:
            with self._path.open() as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            logger.info("Alert cooldown store missing; starting without cooldown state")
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning("Alert cooldown store corrupt or unreadable; failing open")
            return {}

        if not isinstance(payload, dict) or any(
            not isinstance(event, str)
            or isinstance(sent_at, bool)
            or not isinstance(sent_at, (int, float))
            or not math.isfinite(sent_at)
            for event, sent_at in payload.items()
        ):
            logger.warning("Alert cooldown store corrupt; failing open")
            return {}

        return {event: float(sent_at) for event, sent_at in payload.items()}

    def _write(self, cooldowns: dict[str, float]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_path = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        try:
            os.fchmod(fd, 0o660)
            with os.fdopen(fd, "w") as handle:
                json.dump(cooldowns, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            directory_fd = os.open(
                self._path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise
