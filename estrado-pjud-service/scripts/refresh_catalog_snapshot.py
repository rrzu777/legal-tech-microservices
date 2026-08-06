"""Build a secret-free, versioned PJUD catalog fallback from authorized reads."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from app.catalogs import CatalogResult, CatalogService
from app.config import get_settings
from app.session_pool import APISessionPool

FIRST_INSTANCE_COMPETENCIAS = ("apelaciones", "civil", "laboral", "penal", "cobranza")
BOOK_YEARS = range(2022, 2027)


def _record(result: CatalogResult) -> dict[str, object]:
    if result.source != "live" or not result.options:
        raise RuntimeError("Snapshot refresh requires a non-empty live PJUD response")
    return {"fetched_at": result.fetched_at, "options": result.options}


async def build_snapshot() -> dict[str, object]:
    """Read every supported catalog combination, failing closed on incomplete data."""
    pool = APISessionPool(get_settings())
    service = CatalogService(pool, snapshot={})
    try:
        courts = await service.courts(1)
        if courts.source != "live" or len(courts.options) != 18:
            raise RuntimeError(
                f"Expected 18 live courts for tipo_busqueda=1, got {len(courts.options)}"
            )

        snapshot: dict[str, object] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "courts": {"1": _record(courts)},
            "tribunals": {},
            "books": {},
        }
        tribunals = snapshot["tribunals"]
        books = snapshot["books"]
        assert isinstance(tribunals, dict)
        assert isinstance(books, dict)

        # `codCorte` is optional in the official books endpoint, so preserve a
        # fallback for that documented request shape as well as court-scoped UI.
        for competencia in FIRST_INSTANCE_COMPETENCIAS:
            for anno in BOOK_YEARS:
                books[f"{competencia}::{anno}"] = _record(
                    await service.books(competencia, None, anno)
                )

        for court in courts.options:
            corte = int(court["code"])
            for competencia in FIRST_INSTANCE_COMPETENCIAS:
                key = f"{competencia}:{corte}:1"
                tribunals[key] = _record(await service.tribunals(competencia, corte, 1))
                for anno in BOOK_YEARS:
                    book_key = f"{competencia}:{corte}:{anno}"
                    books[book_key] = _record(await service.books(competencia, corte, anno))
        return snapshot
    finally:
        await pool.close_all()


async def main(output: Path) -> None:
    snapshot = await build_snapshot()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    courts = snapshot["courts"]
    tribunals = snapshot["tribunals"]
    books = snapshot["books"]
    assert isinstance(courts, dict) and isinstance(tribunals, dict) and isinstance(books, dict)
    print(
        "catalog snapshot written "
        f"(courts={len(courts['1']['options'])}, tribunals={len(tribunals)}, books={len(books)})"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("app/catalog_snapshot.json"))
    args = parser.parse_args()
    asyncio.run(main(args.output))
