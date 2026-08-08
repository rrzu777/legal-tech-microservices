"""Official PJUD lookup catalogs with safe live/cache/snapshot fallback."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Literal

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.failure_kind import BlockedPageError
from app.parsers.normalizer import competencia_code
from app.parsers.search_parser import detect_blocked
from app.proxy_billing import is_proxy_billing_error
from app.proxy_cost import is_proxy_cost_control_error
from worker.proxy_usage import DISABLED_PROXY_USAGE

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 86_400
_SNAPSHOT_PATH = Path(__file__).with_name("catalog_snapshot.json")

CatalogSource = Literal["live", "cache", "snapshot"]
CatalogOptions = list[dict[str, str]]


class CatalogContentError(ValueError):
    """PJUD returned a body that cannot safely be used as a catalog."""


class _OptionFragmentValidator(HTMLParser):
    """Accept only a whitespace-delimited sequence of closed top-level options."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.valid = True
        self._option_open = False
        self._option_count = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "option" or self._option_open:
            self.valid = False
            return
        self._option_open = True
        self._option_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "option" or not self._option_open:
            self.valid = False
            return
        self._option_open = False

    def handle_data(self, data: str) -> None:
        if not self._option_open and data.strip():
            self.valid = False

    def handle_decl(self, decl: str) -> None:
        self.valid = False

    def handle_comment(self, data: str) -> None:
        self.valid = False

    def handle_pi(self, data: str) -> None:
        self.valid = False

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.valid = False

    @property
    def is_complete(self) -> bool:
        return self.valid and not self._option_open and self._option_count > 0


@dataclass(frozen=True)
class CatalogResult:
    options: CatalogOptions
    source: CatalogSource
    fetched_at: str


class CatalogOption(BaseModel):
    code: str
    label: str


class CatalogResponse(BaseModel):
    options: list[CatalogOption]
    source: CatalogSource
    fetched_at: str


@dataclass(frozen=True)
class TribunalIdentity:
    """One official PJUD tribunal identity resolved without network I/O."""

    court_code: int
    tribunal_code: int
    tribunal_label: str


@dataclass(frozen=True)
class _CacheEntry:
    options: CatalogOptions
    fetched_at: str
    cached_at: datetime


def _clean_option(code: object, label: object) -> dict[str, str] | None:
    normalized_code = str(code or "").strip()
    normalized_label = " ".join(str(label or "").split())
    if not normalized_code or not normalized_label:
        return None
    # PJUD's combo endpoints include an instructional option, never a real value.
    if normalized_code in {"0", "-1"} or normalized_label.lower().startswith("seleccione"):
        return None
    return {"code": normalized_code, "label": normalized_label}


def _unique_options(options: list[dict[str, str]]) -> CatalogOptions:
    result: CatalogOptions = []
    seen_codes: dict[str, str] = {}
    for option in options:
        code = option["code"]
        previous_label = seen_codes.get(code)
        if previous_label is not None and previous_label != option["label"]:
            raise CatalogContentError(
                f"PJUD returned conflicting labels for catalog code {code!r}"
            )
        if previous_label is not None:
            continue
        seen_codes[code] = option["label"]
        result.append(option)
    return result


def normalize_catalog_label(value: str) -> str:
    """Stable label comparison shared by catalog consumers."""
    # PJUD alternates between masculine ordinal and degree glyphs (``2º`` /
    # ``2°``) for the same tribunal. They are typography, not identity.
    value = value.replace("º", "").replace("°", "")
    decomposed = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", " ", decomposed).strip().upper()


def resolve_catalog_code(options: CatalogOptions, label: str) -> int | None:
    """Return a code only when one official option exactly matches the label."""
    normalized_label = normalize_catalog_label(label)
    codes = {
        option["code"]
        for option in options
        if normalize_catalog_label(option["label"]) == normalized_label and option["code"].isdigit()
    }
    return int(codes.pop()) if len(codes) == 1 else None


def parse_json_options(rows: object, code_key: str, label_key: str) -> CatalogOptions:
    """Normalize a PJUD JSON combo without exposing placeholder rows."""
    if not isinstance(rows, list):
        return []
    options: CatalogOptions = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        option = _clean_option(row.get(code_key), row.get(label_key))
        if option is not None:
            options.append(option)
    return _unique_options(options)


def parse_html_options(html: str) -> CatalogOptions:
    """Parse PJUD's books `<option>` fragment; non-options are never catalogs."""
    if not html or detect_blocked(html):
        return []
    validator = _OptionFragmentValidator()
    validator.feed(html)
    validator.close()
    if not validator.is_complete:
        return []
    soup = BeautifulSoup(html, "html.parser")
    # cmbTipos.php returns only an option fragment. A complete page may be a
    # login/WAF response which happens to contain options and is never valid.
    if soup.html is not None or soup.find("select") is not None:
        return []
    options: CatalogOptions = []
    for node in soup.find_all("option"):
        option = _clean_option(node.get("value"), node.get_text(" ", strip=True))
        if option is not None:
            options.append(option)
    return _unique_options(options)


def _snapshot_options(value: object) -> CatalogOptions:
    if not isinstance(value, list):
        return []
    options: CatalogOptions = []
    for option in value:
        if not isinstance(option, dict):
            continue
        cleaned = _clean_option(option.get("code"), option.get("label"))
        if cleaned is not None:
            options.append(cleaned)
    return _unique_options(options)


class CatalogService:
    """Fetch PJUD catalogs with a 24h process-local cache and bundled fallback."""

    def __init__(
        self,
        session_pool,
        *,
        snapshot: dict | None = None,
        snapshot_path: Path = _SNAPSHOT_PATH,
        now: Callable[[], datetime] | None = None,
        proxy_usage=None,
    ):
        self._pool = session_pool
        self._snapshot = snapshot if snapshot is not None else self._load_snapshot(snapshot_path)
        self._cache: dict[str, _CacheEntry] = {}
        self._now = now or (lambda: datetime.now(UTC))
        self._proxy_usage = proxy_usage or DISABLED_PROXY_USAGE

    @staticmethod
    def _load_snapshot(path: Path) -> dict:
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("Catalog snapshot is missing: %s", path)
            return {}
        except (OSError, json.JSONDecodeError):
            logger.exception("Catalog snapshot is unreadable: %s", path)
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _timestamp(self) -> str:
        return self._now().isoformat()

    async def courts(self, tipo_busqueda: int = 1) -> CatalogResult:
        params = {"tipo_busqueda": str(tipo_busqueda)}
        return await self._get("courts", params, str(tipo_busqueda))

    async def tribunals(
        self, competencia: str, corte: int, tipo_busqueda: int = 1
    ) -> CatalogResult:
        params = {
            "competencia": competencia,
            "corte": str(corte),
            "tipo_busqueda": str(tipo_busqueda),
        }
        return await self._get("tribunals", params, f"{competencia}:{corte}:{tipo_busqueda}")

    def resolve_loaded_court(self, court_label: str) -> int | None:
        """Resolve one court from snapshot/cache memory without pool I/O."""
        courts = self._snapshot.get("courts")
        record = courts.get("1") if isinstance(courts, dict) else None
        options = (
            _snapshot_options(record.get("options"))
            if isinstance(record, dict)
            else []
        )
        cached = self._cache.get("courts:1")
        if cached is not None:
            options = cached.options
        return resolve_catalog_code(options, court_label)

    def resolve_loaded_book(
        self, competencia: str, book_code: str, anno: int, *, corte: int
    ) -> dict[str, str] | None:
        """Resolve one official book in a known court/year from local memory."""
        snapshot_key = f"{competencia}:{corte}:{anno}"
        books = self._snapshot.get("books")
        record = books.get(snapshot_key) if isinstance(books, dict) else None
        options = (
            _snapshot_options(record.get("options"))
            if isinstance(record, dict)
            else []
        )
        cached = self._cache.get(f"books:{snapshot_key}")
        if cached is not None:
            options = cached.options
        matches = [option for option in options if option["code"] == book_code]
        return dict(matches[0]) if len(matches) == 1 else None

    def resolve_loaded_tribunal(
        self, competencia: str, tribunal_label: str, *, corte: int | None = None
    ) -> TribunalIdentity | None:
        """Resolve from the loaded snapshot/cache only, never acquiring a session.

        Broad worker searches hold the only PJUD slot while parsing results.  A
        second live catalog fetch here would wait for that same slot forever at
        pool size one, so this method deliberately has no async path and fails
        closed when its official local data is absent or ambiguous.
        """
        target = normalize_catalog_label(tribunal_label)
        if not target:
            return None

        by_court: dict[int, CatalogOptions] = {}
        tribunals = self._snapshot.get("tribunals")
        if isinstance(tribunals, dict):
            for key, record in tribunals.items():
                parts = key.split(":")
                if len(parts) != 3 or parts[0] != competencia or parts[2] != "1":
                    continue
                try:
                    court_code = int(parts[1])
                except ValueError:
                    continue
                if corte is not None and court_code != corte:
                    continue
                if isinstance(record, dict):
                    by_court[court_code] = _snapshot_options(record.get("options"))

        # A fresh live lookup already loaded in this process is just as official
        # as the snapshot and supersedes the stale record for its court. Reading
        # it is local memory; it never turns into a nested pool acquisition.
        prefix = f"tribunals:{competencia}:"
        for cache_key, entry in self._cache.items():
            if not cache_key.startswith(prefix) or not cache_key.endswith(":1"):
                continue
            court_text = cache_key[len(prefix):-2]
            try:
                court_code = int(court_text)
            except ValueError:
                continue
            if corte is None or court_code == corte:
                by_court[court_code] = entry.options

        identities: dict[tuple[int, int], TribunalIdentity] = {}
        for court_code, options in by_court.items():
            for option in options:
                if normalize_catalog_label(option["label"]) != target or not option["code"].isdigit():
                    continue
                tribunal_code = int(option["code"])
                identities[(court_code, tribunal_code)] = TribunalIdentity(
                    court_code=court_code,
                    tribunal_code=tribunal_code,
                    tribunal_label=option["label"],
                )

        return next(iter(identities.values())) if len(identities) == 1 else None

    async def books(
        self, competencia: str, corte: int | None, anno: int
    ) -> CatalogResult:
        params = {"competencia": competencia, "anno": str(anno)}
        if corte is not None:
            params["corte"] = str(corte)
        snapshot_key = f"{competencia}:{corte if corte is not None else ''}:{anno}"
        return await self._get("books", params, snapshot_key)

    async def _get(
        self, catalog: str, params: dict[str, str], snapshot_key: str
    ) -> CatalogResult:
        cache_key = f"{catalog}:{snapshot_key}"
        cached = self._cache.get(cache_key)
        if (
            cached is not None
            and (self._now() - cached.cached_at).total_seconds() < CATALOG_TTL_SECONDS
        ):
            return CatalogResult(cached.options, "cache", cached.fetched_at)

        try:
            options = await self._fetch_live(catalog, params)
            if not options:
                raise CatalogContentError(f"PJUD returned no usable {catalog} options")
        except Exception as exc:
            if is_proxy_billing_error(exc) or is_proxy_cost_control_error(exc):
                raise
            logger.warning(
                "Catalog live fetch failed; using snapshot (catalog=%s, error=%s)",
                catalog,
                type(exc).__name__,
            )
            snapshot = self._from_snapshot(catalog, snapshot_key)
            if snapshot is None:
                raise CatalogContentError(
                    f"No valid snapshot catalog for {catalog}:{snapshot_key}"
                ) from exc
            return snapshot

        fetched_at = self._timestamp()
        self._cache[cache_key] = _CacheEntry(options, fetched_at, self._now())
        return CatalogResult(options, "live", fetched_at)

    def _from_snapshot(self, catalog: str, key: str) -> CatalogResult | None:
        group = self._snapshot.get(catalog)
        if not isinstance(group, dict):
            return None
        record = group.get(key)
        if not isinstance(record, dict):
            return None
        options = _snapshot_options(record.get("options"))
        if not options:
            return None
        fetched_at = record.get("fetched_at")
        if not isinstance(fetched_at, str) or not fetched_at:
            fetched_at = self._snapshot.get("generated_at")
        if not isinstance(fetched_at, str) or not fetched_at:
            return None
        return CatalogResult(options, "snapshot", fetched_at)

    def snapshot_options(self, catalog: str, params: dict[str, str]) -> CatalogOptions:
        """Return one bundled baseline slice without cache or network access."""
        if catalog == "tribunals":
            key = (
                f"{params['competencia']}:{params['corte']}:"
                f"{params.get('tipo_busqueda', '1')}"
            )
        elif catalog == "books":
            key = (
                f"{params['competencia']}:{params.get('corte', '')}:"
                f"{params['anno']}"
            )
        else:
            raise ValueError(f"Unsupported opportunistic catalog {catalog!r}")
        result = self._from_snapshot(catalog, key)
        return list(result.options) if result is not None else []

    async def fetch_with_session(
        self,
        session,
        catalog: str,
        params: dict[str, str],
        *,
        retry_transport: bool = True,
    ) -> CatalogOptions:
        """Fetch one slice through a caller-owned, already-ready session."""
        if catalog not in {"courts", "tribunals", "books"}:
            raise ValueError(f"Unknown catalog {catalog!r}")
        try:
            options = await self._request_catalog(
                session,
                catalog,
                params,
                retry_transport=retry_transport,
            )
        except BlockedPageError:
            raise
        except httpx.HTTPStatusError as exc:
            if 300 <= exc.response.status_code < 400:
                raise CatalogContentError(
                    f"PJUD redirected {catalog} catalog unexpectedly"
                ) from exc
            raise
        except (TypeError, ValueError) as exc:
            raise CatalogContentError(
                f"PJUD returned malformed {catalog} catalog"
            ) from exc
        if not options:
            raise CatalogContentError(f"PJUD returned invalid {catalog} catalog")
        return options

    async def _request_catalog(
        self,
        session,
        catalog: str,
        params: dict[str, str],
        *,
        retry_transport: bool,
    ) -> CatalogOptions:
        if catalog == "courts":
            rows = await session.catalog_json(
                "/combosJSON/leeCorte.php",
                {"tipoBusqueda": params["tipo_busqueda"]},
                retry_transport=retry_transport,
            )
            return parse_json_options(rows, "COD_CORTE", "GLS_CORTE")
        if catalog == "tribunals":
            rows = await session.catalog_json(
                "/combosJSON/leeTrib.php",
                {
                    "codCompetencia": str(competencia_code(params["competencia"])),
                    "codCorte": params["corte"],
                    "tipoBusqueda": params["tipo_busqueda"],
                },
                retry_transport=retry_transport,
            )
            return parse_json_options(rows, "COD_TRIBUNAL", "GLS_TRIBUNAL")
        if catalog == "books":
            data = {
                "codCompetencia": str(competencia_code(params["competencia"])),
                "codAnho": params["anno"],
            }
            if "corte" in params:
                data["codCorte"] = params["corte"]
            html = await session.catalog_html(
                "/ADIR_871/json/cmbTipos.php",
                data,
                retry_transport=retry_transport,
            )
            return parse_html_options(html)
        raise ValueError(f"Unknown catalog {catalog!r}")

    async def _fetch_live(self, catalog: str, params: dict[str, str]) -> CatalogOptions:
        session = await self._pool.acquire()
        healthy = True
        try:
            # Reserve immediately before the catalog call. Pool acquisition can
            # mint for tens of seconds and has its own separately metered scope.
            async with self._proxy_usage.track(operation="catalog"):
                options = await self.fetch_with_session(session, catalog, params)
                if not options:
                    healthy = False
                    raise CatalogContentError(f"PJUD returned invalid {catalog} catalog")
                return options
        except Exception:
            # A malformed, empty or WAF response is bound to this OJV session;
            # returning it to the pool would make the next catalog request reuse
            # the same blocked cookie/IP instead of rotating through the pool.
            healthy = False
            raise
        finally:
            await self._pool.release(session, healthy=healthy)
