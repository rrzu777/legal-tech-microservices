import json
import os
import tempfile
import time
from dataclasses import dataclass


@dataclass
class CookieBundle:
    cookies: dict[str, str]
    user_agent: str
    saved_at: float

    @property
    def age_seconds(self) -> float:
        return time.time() - self.saved_at


class CookieStore:
    """Store de cookies TSPD compartido entre procesos (worker + API).

    Escritura atómica (write-temp + rename) para que un lector nunca vea
    un JSON a medio escribir. El lock efectivo lo da el rename atómico
    del sistema de archivos POSIX.
    """

    def __init__(self, path: str):
        self._path = path

    def save(self, cookies: dict[str, str], user_agent: str) -> None:
        payload = {"cookies": cookies, "user_agent": user_agent, "saved_at": time.time()}
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def load(self) -> CookieBundle | None:
        try:
            with open(self._path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return CookieBundle(
            cookies=data["cookies"],
            user_agent=data["user_agent"],
            saved_at=data["saved_at"],
        )
