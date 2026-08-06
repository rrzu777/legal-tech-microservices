"""Official PJUD lookup catalogs with safe live/cache/snapshot fallback."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel

from app.parsers.normalizer import competencia_code
from app.parsers.search_parser import detect_blocked

logger = logging.getLogger(__name__)

CATALOG_TTL_SECONDS = 86_400
_SNAPSHOT_PATH = Path(__file__).with_name("catalog_snapshot.json")

CatalogSource = Literal["live", "cache", "snapshot"]
CatalogOptions = list[dict[str, str]]


class CatalogContentError(ValueError):
    """PJUD returned a body that cannot safely be used as a catalog."""


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
    seen_codes: set[str] = set()
    for option in options:
        code = option["code"]
        if code in seen_codes:
            continue
        seen_codes.add(code)
        result.append(option)
    return result


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
    soup = BeautifulSoup(html, "html.parser")
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
    ):
        self._pool = session_pool
        self._snapshot = snapshot if snapshot is not None else self._load_snapshot(snapshot_path)
        self._cache: dict[str, _CacheEntry] = {}
        self._now = now or (lambda: datetime.now(UTC))

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

    async def _fetch_live(self, catalog: str, params: dict[str, str]) -> CatalogOptions:
        session = await self._pool.acquire()
        healthy = True
        try:
            if catalog == "courts":
                rows = await session.catalog_json(
                    "/combosJSON/leeCorte.php",
                    {"tipoBusqueda": params["tipo_busqueda"]},
                )
                options = parse_json_options(rows, "COD_CORTE", "GLS_CORTE")
            elif catalog == "tribunals":
                rows = await session.catalog_json(
                    "/combosJSON/leeTrib.php",
                    {
                        "codCompetencia": str(competencia_code(params["competencia"])),
                        "codCorte": params["corte"],
                        "tipoBusqueda": params["tipo_busqueda"],
                    },
                )
                options = parse_json_options(rows, "COD_TRIBUNAL", "GLS_TRIBUNAL")
            elif catalog == "books":
                data = {
                    "codCompetencia": str(competencia_code(params["competencia"])),
                    "codAnho": params["anno"],
                }
                if "corte" in params:
                    data["codCorte"] = params["corte"]
                html = await session.catalog_html("/ADIR_871/json/cmbTipos.php", data)
                if detect_blocked(html):
                    healthy = False
                options = parse_html_options(html)
            else:
                raise ValueError(f"Unknown catalog {catalog!r}")
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
