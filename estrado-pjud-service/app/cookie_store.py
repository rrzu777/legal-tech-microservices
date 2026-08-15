import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping, Sequence

import fcntl

from app.cookie_scope import (
    CookieRecord,
    cookie_record_from_json,
    legacy_cookie_records,
    normalize_cookie_records,
    legacy_cookie_scope,
)

DEFAULT_COOKIE_STORE_PATH = "/var/lib/estrado-pjud/cookies.json"
PRODUCTION_CHECKOUT = Path("/opt/legal-tech-microservices")
DEFAULT_LEGACY_COOKIE_DOMAIN = "oficinajudicialvirtual.pjud.cl"


class CookieStoreLockTimeoutError(TimeoutError):
    """The local shared-store lock remained unavailable within its safe bound."""

    def __init__(self):
        super().__init__("cookie_store_lock_timeout")


class CookieStoreConcurrentUpdateError(CookieStoreLockTimeoutError):
    """A revalidated candidate lost the compare-and-set for its bundle."""

    def __init__(self):
        TimeoutError.__init__(self, "cookie_store_compare_and_set_failed")


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
    cookies: tuple[CookieRecord, ...]
    user_agent: str
    saved_at: float
    proxy_url: str | None = None
    proxy_token: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.cookies, Mapping):
            self.cookies = legacy_cookie_records(
                self.cookies,
                domain=DEFAULT_LEGACY_COOKIE_DOMAIN,
                secure=True,
            )
        else:
            self.cookies = normalize_cookie_records(self.cookies)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.saved_at


class CookieStore:
    """Store de cookies TSPD compartido entre procesos (worker + API).

    Escritura atómica (write-temp + rename) para que un lector nunca vea
    un JSON a medio escribir. Un lock persistente interproceso protege cada
    escritura y el read-modify-write de los slots contra actualizaciones
    perdidas entre worker y API.
    """

    def __init__(
        self,
        path: str,
        *,
        lock_timeout_s: float = 2.0,
        legacy_cookie_domain: str = DEFAULT_LEGACY_COOKIE_DOMAIN,
        legacy_cookie_secure: bool = True,
    ):
        self._path = path
        self._lock_timeout_s = min(max(lock_timeout_s, 0.0), 2.0)
        self._legacy_cookie_domain = legacy_cookie_domain
        self._legacy_cookie_secure = legacy_cookie_secure

    def _normalize_cookies(
        self, cookies: Sequence[CookieRecord] | Mapping[str, str],
    ) -> tuple[CookieRecord, ...]:
        if isinstance(cookies, Mapping):
            return legacy_cookie_records(
                cookies,
                domain=self._legacy_cookie_domain,
                secure=self._legacy_cookie_secure,
            )
        return normalize_cookie_records(cookies)

    def _serialize_cookies(self, cookies) -> list[dict]:
        return [record.to_json() for record in self._normalize_cookies(cookies)]

    def _parse_cookies(self, cookies) -> tuple[CookieRecord, ...]:
        if isinstance(cookies, Mapping):
            return self._normalize_cookies(cookies)
        if not isinstance(cookies, list):
            raise ValueError("invalid_cookie_record")
        return normalize_cookie_records(cookie_record_from_json(item) for item in cookies)

    def _bundle_from_raw(self, data) -> CookieBundle:
        if not isinstance(data, dict):
            raise ValueError("invalid_cookie_bundle")
        user_agent = data["user_agent"]
        saved_at = data["saved_at"]
        proxy_token = data.get("proxy_token")
        try:
            saved_at_is_finite = math.isfinite(saved_at)
        except (OverflowError, TypeError):
            saved_at_is_finite = False
        if (
            not isinstance(user_agent, str) or not user_agent
            or isinstance(saved_at, bool)
            or not isinstance(saved_at, (int, float))
            or not saved_at_is_finite
            or (proxy_token is not None and not isinstance(proxy_token, str))
        ):
            raise ValueError("invalid_cookie_bundle")
        return CookieBundle(
            cookies=self._parse_cookies(data["cookies"]),
            user_agent=user_agent,
            saved_at=saved_at,
            proxy_url=None,
            proxy_token=proxy_token,
        )

    def configure_legacy_scope(self, base_url: str) -> None:
        domain, secure = legacy_cookie_scope(base_url)
        self._legacy_cookie_domain = domain
        self._legacy_cookie_secure = secure

    def _serialize_bundle(self, bundle: CookieBundle) -> dict:
        return {
            "cookies": self._serialize_cookies(bundle.cookies),
            "user_agent": bundle.user_agent,
            "proxy_token": bundle.proxy_token,
            "saved_at": bundle.saved_at,
        }

    @contextmanager
    def _exclusive_write_lock(self):
        """Hold the stable per-store lock inode for one complete write.

        The lock file is intentionally never removed, chmod'd, or chown'd
        after its first creation: systemd may assign group ownership to it.
        """
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        lock_path = f"{self._path}.lock"
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o640)
        except FileExistsError:
            # The API is in the estrado group but only has group-read access
            # to a worker-owned 0640 inode. flock supports LOCK_EX on this
            # read-only descriptor, so do not require a write permission
            # merely to coordinate the writers.
            fd = os.open(lock_path, os.O_RDONLY)
        else:
            os.fchmod(fd, 0o640)

        locked = False
        deadline = time.monotonic() + self._lock_timeout_s
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except (BlockingIOError, InterruptedError):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CookieStoreLockTimeoutError()
                    time.sleep(min(0.01, remaining))
            yield
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def save(self, cookies, user_agent: str) -> None:
        payload = {
            "version": 2,
            "cookies": self._serialize_cookies(cookies),
            "user_agent": user_agent,
            "saved_at": time.time(),
        }
        with self._exclusive_write_lock():
            d = os.path.dirname(self._path) or "."
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f)
                # Worker y API comparten Group=estrado y el StateDirectory 0770.
                # El archivo queda legible sólo por dueño/grupo, nunca por el resto.
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
            return self._bundle_from_raw(data)
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
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
        payload = {"version": 2, "slots": slots}
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
        # Tolera archivo ausente, JSON corrupto o esquema incorrecto. El formato
        # single-bundle previo se expone como slot 0 para migrarlo al primer write.
        try:
            with open(self._path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            if "slots" not in data and {"cookies", "user_agent", "saved_at"} <= set(data):
                return {"0": data}
            slots = data["slots"]
            if not isinstance(slots, dict):
                return {}
            return slots
        except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError):
            return {}

    def save_slot(
        self,
        slot_id,
        cookies: Sequence[CookieRecord] | Mapping[str, str],
        user_agent: str,
        proxy_token: str | None,
    ) -> None:
        slot_key = str(slot_id)
        with self._exclusive_write_lock():
            existing = self.load_all()
            slots = {
                key: self._serialize_bundle(bundle)
                for key, bundle in existing.items()
            }
            slots[slot_key] = {
                "cookies": self._serialize_cookies(cookies),
                "user_agent": user_agent,
                # Persistir el URL completo filtraba usuario/password del proveedor.
                # El API reconstruye el URL sticky desde su OJV_PROXY_URL secreto y
                # este token opaco; un store robado no alcanza para usar el proxy.
                "proxy_token": proxy_token,
                "saved_at": time.time(),
            }
            self._write_all(slots)

    def replace_slot_cookies_if_current(
        self,
        slot_id,
        *,
        expected_saved_at: float,
        expected_proxy_token: str | None,
        cookies: Sequence[CookieRecord] | Mapping[str, str],
    ) -> bool:
        """Replace only a still-current bundle's cookies under one RMW lock.

        Revalidation may race another process replacing the same slot.  The
        durable mint identity (``saved_at`` + sticky token) is therefore the
        compare key and is never renewed by this write.
        """
        slot_key = str(slot_id)
        with self._exclusive_write_lock():
            slots = self._read_all_raw()
            try:
                current = self._bundle_from_raw(slots.get(slot_key))
            except (KeyError, TypeError, ValueError):
                return False
            if (
                current.saved_at != expected_saved_at
                or current.proxy_token != expected_proxy_token
            ):
                return False
            slots[slot_key] = {
                **self._serialize_bundle(current),
                "cookies": self._serialize_cookies(cookies),
            }
            self._write_all(slots)
            return True

    def load_slot(self, slot_id) -> CookieBundle | None:
        return self.load_all().get(str(slot_id))

    def load_all(self) -> dict[str, "CookieBundle"]:
        slots = self._read_all_raw()
        result: dict[str, CookieBundle] = {}
        for slot_key, data in slots.items():
            try:
                result[slot_key] = self._bundle_from_raw(data)
            except (KeyError, TypeError, ValueError):
                continue
        return result
