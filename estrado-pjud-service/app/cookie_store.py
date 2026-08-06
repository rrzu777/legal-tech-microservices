import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_COOKIE_STORE_PATH = "/var/lib/estrado-pjud/cookies.json"
PRODUCTION_CHECKOUT = Path("/opt/legal-tech-microservices")


def validate_cookie_store_path(value: str) -> str:
    """Fail closed if production would persist runtime state inside Git."""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("COOKIE_STORE_PATH must be absolute and outside the git checkout")
    lexical = Path(os.path.normpath(value))
    normalized = path.resolve(strict=False)
    if lexical.is_relative_to(PRODUCTION_CHECKOUT) or normalized.is_relative_to(
        PRODUCTION_CHECKOUT.resolve(strict=False)
    ):
        raise ValueError("COOKIE_STORE_PATH must be absolute and outside the git checkout")
    return value


@dataclass
class CookieBundle:
    cookies: dict[str, str]
    user_agent: str
    saved_at: float
    proxy_url: str | None = None
    proxy_token: str | None = None

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
            # El worker (User/Group=estrado) escribe; la API (www-data con
            # SupplementaryGroups=estrado) lee. Nunca exponer cookies al resto.
            os.chmod(tmp, 0o640)
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
            os.chmod(tmp, 0o640)
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
        proxy_token: str | None,
    ) -> None:
        slot_key = str(slot_id)
        slots = self._read_all_raw()
        slots[slot_key] = {
            "cookies": cookies,
            "user_agent": user_agent,
            # Persistir el URL completo filtraba usuario/password del proveedor.
            # El API reconstruye el URL sticky desde su OJV_PROXY_URL secreto y
            # este token opaco; un store robado no alcanza para usar el proxy.
            "proxy_token": proxy_token,
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
                    # Deliberadamente ignoramos proxy_url del esquema legacy:
                    # nunca volver a cargar credenciales persistidas en disco.
                    proxy_url=None,
                    proxy_token=data.get("proxy_token"),
                )
            except (KeyError, TypeError):
                continue
        return result
