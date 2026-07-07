import json
import os
import tempfile
import time
from dataclasses import dataclass

DEFAULT_COOKIE_STORE_PATH = "/opt/legal-tech-microservices/estrado-pjud-service/.cookies.json"


@dataclass
class CookieBundle:
    cookies: dict[str, str]
    user_agent: str
    saved_at: float
    proxy_url: str | None = None

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
            # El worker (User=estrado) escribe el store; el servicio API
            # (User=www-data) lo lee. mkstemp crea 0600 → hacerlo legible por
            # ambos. Los cookies TSPD son de baja sensibilidad (tokens anti-bot
            # + sesión invitado, no credenciales). Revisar si Familia (sesión
            # autenticada) llega a persistirse acá.
            os.chmod(tmp, 0o644)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def load(self) -> CookieBundle | None:
        # Tolera archivo ausente, JSON mal formado, o JSON con forma incorrecta
        # (ej. cambio de esquema entre procesos en un deploy) → re-mint en vez de crashear.
        try:
            with open(self._path) as f:
                data = json.load(f)
            return CookieBundle(
                cookies=data["cookies"],
                user_agent=data["user_agent"],
                saved_at=data["saved_at"],
            )
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
            return None

    # -- Multi-bundle API (N slots, uno por IP del pool) ---------------------
    #
    # El worker mantiene N sesiones (una por IP sticky del pool); cada una
    # necesita su propio bundle de cookies TSPD ligado a su proxy_url, porque
    # el cookie está atado a la IP con la que fue minteado. El store completo
    # (todos los slots) se escribe atómicamente para que un lector (API,
    # www-data) nunca vea un archivo a medio escribir ni pierda otros slots
    # por una escritura concurrente de un slot distinto.

    def _write_all(self, slots: dict[str, dict]) -> None:
        payload = {"slots": slots}
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self._path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def _read_all_raw(self) -> dict[str, dict]:
        # Tolera archivo ausente, JSON corrupto, o esquema viejo/incorrecto
        # (incl. el formato single-bundle previo) → vacío, nunca crashea.
        try:
            with open(self._path) as f:
                data = json.load(f)
            slots = data["slots"]
            if not isinstance(slots, dict):
                return {}
            return slots
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
            return {}

    def save_slot(
        self,
        slot_id,
        cookies: dict[str, str],
        user_agent: str,
        proxy_url: str | None,
    ) -> None:
        slot_key = str(slot_id)
        slots = self._read_all_raw()
        slots[slot_key] = {
            "cookies": cookies,
            "user_agent": user_agent,
            "proxy_url": proxy_url,
            "saved_at": time.time(),
        }
        self._write_all(slots)

    def load_slot(self, slot_id) -> CookieBundle | None:
        return self.load_all().get(str(slot_id))

    def load_all(self) -> dict[str, "CookieBundle"]:
        slots = self._read_all_raw()
        result: dict[str, CookieBundle] = {}
        for slot_key, data in slots.items():
            try:
                result[slot_key] = CookieBundle(
                    cookies=data["cookies"],
                    user_agent=data["user_agent"],
                    saved_at=data["saved_at"],
                    proxy_url=data.get("proxy_url"),
                )
            except (KeyError, TypeError):
                continue
        return result
