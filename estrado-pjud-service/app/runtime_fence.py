"""Read-only runtime admission observation; PostgreSQL remains write authority."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, fields
from datetime import datetime
from types import MappingProxyType
from typing import Mapping

RUNTIME_GENERATION_HEADER = "x-pjud-runtime-generation"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
_BINDINGS = {"micro_sha", "web_sha", "rollback_micro_sha", "rollback_web_sha"}


def validate_runtime_generation(value: str | None) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ValueError("pjud_runtime_invalid_generation")
    return value


def runtime_generation_headers(value: str | None) -> dict[str, str]:
    generation = validate_runtime_generation(value)
    return {RUNTIME_GENERATION_HEADER: generation} if generation is not None else {}


class PjudRuntimeError(Exception):
    def __init__(self, code: str = "pjud_runtime_unavailable"):
        if code not in {"pjud_runtime_unavailable", "pjud_runtime_generation_mismatch", "pjud_admission_paused"}:
            code = "pjud_runtime_unavailable"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PjudRuntimeControl:
    protocol_version: int
    revision: int
    admission_paused: bool
    generation_required: bool
    generation: str | None
    sealed_at: str | None
    bindings: Mapping[str, str] | None

    @classmethod
    def parse(cls, value) -> PjudRuntimeControl:
        if not isinstance(value, dict) or set(value) != {field.name for field in fields(cls)}:
            raise PjudRuntimeError()
        if (type(value["protocol_version"]) is not int or value["protocol_version"] != 1
                or type(value["revision"]) is not int or not 0 <= value["revision"] <= 9007199254740991
                or type(value["admission_paused"]) is not bool
                or type(value["generation_required"]) is not bool):
            raise PjudRuntimeError()
        generation, sealed_at, bindings = (value[key] for key in ("generation", "sealed_at", "bindings"))
        if not value["generation_required"]:
            if any(item is not None for item in (generation, sealed_at, bindings)):
                raise PjudRuntimeError()
        else:
            if validate_runtime_generation(generation) is None:
                raise PjudRuntimeError()
            if not isinstance(sealed_at, str) or _TIMESTAMP.fullmatch(sealed_at) is None:
                raise PjudRuntimeError()
            if datetime.fromisoformat(sealed_at.replace("Z", "+00:00")).utcoffset() is None:
                raise PjudRuntimeError()
            if (not isinstance(bindings, dict) or set(bindings) != _BINDINGS
                    or any(not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None
                           for sha in bindings.values())):
                raise PjudRuntimeError()
            bindings = MappingProxyType(dict(bindings))
        return cls(**{**value, "bindings": bindings})


class RuntimeFence:
    def __init__(self, supabase, generation: str | None):
        self._supabase = supabase
        self._generation = validate_runtime_generation(generation)

    @property
    def generation(self) -> str | None:
        return self._generation

    async def snapshot(self) -> PjudRuntimeControl:
        try:
            if self._supabase is None:
                raise PjudRuntimeError()
            response = await asyncio.wait_for(
                asyncio.to_thread(lambda: self._supabase.rpc("get_pjud_runtime_control", {}).execute()),
                timeout=5.0,
            )
            return PjudRuntimeControl.parse(response.data)
        except Exception:
            # No response bodies, headers or secrets cross the observation boundary.
            # CancelledError is a BaseException and must propagate unchanged.
            raise PjudRuntimeError() from None

    async def require(self, *, admission: bool = True) -> PjudRuntimeControl:
        snapshot = await self.snapshot()
        if snapshot.generation_required and snapshot.generation != self._generation:
            raise PjudRuntimeError("pjud_runtime_generation_mismatch")
        if admission and snapshot.admission_paused:
            raise PjudRuntimeError("pjud_admission_paused")
        return snapshot

    async def require_origin(self, values: list[str], *, admission: bool = True) -> PjudRuntimeControl:
        snapshot = await self.require(admission=False)
        if snapshot.generation_required and values != [self._generation]:
            raise PjudRuntimeError("pjud_runtime_generation_mismatch")
        if admission and snapshot.admission_paused:
            raise PjudRuntimeError("pjud_admission_paused")
        return snapshot
