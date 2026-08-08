import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_ALERT_COOLDOWN_STORE_PATH = "/var/lib/estrado-pjud/alert-cooldowns.json"


class AlertCooldownStore:
    """Persistent per-event cooldown claims for operational alerts."""

    def __init__(self, path: str):
        self._path = Path(path)

    def claim(self, event: str, cooldown_seconds: int) -> bool:
        """Persist a due event and return whether its alert may be attempted."""
        now = time.time()
        cooldowns = self._load()
        last_sent_at = cooldowns.get(event)
        if last_sent_at is not None and now - last_sent_at < cooldown_seconds:
            return False

        cooldowns[event] = now
        self._write(cooldowns)
        return True

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
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w") as handle:
                json.dump(cooldowns, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
        except BaseException:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise
