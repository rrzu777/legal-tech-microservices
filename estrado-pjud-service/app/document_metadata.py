"""Stable, non-secret PJUD document metadata shared by sync producers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


_MOVEMENT_PAYLOAD_FIELDS = (
    "folio",
    "cuaderno",
    "etapa",
    "tramite",
    "descripcion",
    "fecha",
    "foja",
    "sala",
    "estado",
)
_CANONICAL_WHITESPACE_RE = re.compile(
    r"[\u0009-\u000D\u0020\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000\uFEFF]+"
)
_SENSITIVE_KEYS = {
    "anexo_func",
    "anexo_token",
    "cookie",
    "cookies",
    "document_content_type",
    "document_storage_key",
    "document_url",
    "documento_param",
    "documento_token",
    "documento_url",
    "documentos_adicionales",
    "download_function",
    "download_url",
    "ebook_token",
    "funcion_descarga",
    "jwt",
    "param",
    "query_string",
    "storage_key",
    "token",
    "url",
}
_REMOVED = object()


def _normalize_display_part(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str("" if value is None else value))
    return _CANONICAL_WHITESPACE_RE.sub(" ", normalized).strip()


def _normalize_identity_part(value: Any) -> str:
    return _normalize_display_part(value).lower()


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _source_id(parts: list[str | int]) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sensitive_key(key: str) -> bool:
    normalized = unicodedata.normalize("NFKC", key).lower()
    return normalized in _SENSITIVE_KEYS or normalized.endswith((
        "_token",
        "_jwt",
        "_cookie",
        "_storage_key",
        "_download_url",
    ))


def _is_remote_document_string(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        normalized.startswith(("http://", "https://", "javascript:"))
        or normalized.startswith("/documento")
        or normalized.startswith("/document/")
    )


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, list):
        return [
            sanitized
            for item in value
            if (sanitized := _sanitize_json(item)) is not _REMOVED
        ]
    if isinstance(value, dict):
        return {
            key: sanitized
            for key, child in value.items()
            if not _is_sensitive_key(key)
            if (sanitized := _sanitize_json(child)) is not _REMOVED
        }
    if isinstance(value, str) and _is_remote_document_string(value):
        return _REMOVED
    return value


def extract_pjud_document_sources(
    payload: dict[str, Any], movement_identity: str
) -> list[dict[str, Any]]:
    identity = _normalize_display_part(movement_identity)
    if not identity:
        return []

    sources: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()

    if _is_non_empty_string(payload.get("documento_url")) and _is_non_empty_string(
        payload.get("documento_token")
    ):
        principal = {
            "document_kind": "principal",
            "source_id": _source_id([identity, "principal"]),
            "ordinal": 0,
            "label": "Documento principal",
            "available": True,
        }
        sources.append(principal)
        seen_source_ids.add(principal["source_id"])

    additional = payload.get("documentos_adicionales")
    if isinstance(additional, list):
        for ordinal, candidate in enumerate(additional):
            if not isinstance(candidate, dict):
                continue
            if not _is_non_empty_string(candidate.get("url")) or not _is_non_empty_string(
                candidate.get("token")
            ):
                continue

            raw_label = _normalize_display_part(candidate.get("label") or candidate.get("tipo"))
            label = raw_label or f"Certificado {ordinal + 1}"
            stable_code = _normalize_identity_part(candidate.get("codigo") or candidate.get("code"))
            identity_parts: list[str | int]
            if stable_code:
                identity_parts = [identity, "certificate", "code", stable_code]
            else:
                identity_parts = [
                    identity,
                    "certificate",
                    "label",
                    _normalize_identity_part(label),
                    ordinal,
                ]
            certificate = {
                "document_kind": "certificate",
                "source_id": _source_id(identity_parts),
                "ordinal": ordinal,
                "label": label,
                "available": True,
            }
            if certificate["source_id"] in seen_source_ids:
                continue
            sources.append(certificate)
            seen_source_ids.add(certificate["source_id"])

    if _is_non_empty_string(payload.get("anexo_func")) and _is_non_empty_string(
        payload.get("anexo_token")
    ):
        discovery = {
            "document_kind": "anexo_discovery",
            "source_id": _source_id([identity, "anexo_discovery"]),
            "ordinal": 0,
            "label": "Anexos",
            "available": True,
        }
        if discovery["source_id"] not in seen_source_ids:
            sources.append(discovery)

    return sources


def sanitize_pjud_movement_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        field: payload[field]
        for field in _MOVEMENT_PAYLOAD_FIELDS
        if field in payload
    }


def sanitize_pjud_case_external_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _sanitize_json(payload)


def contains_pjud_document_secret(value: Any) -> bool:
    if isinstance(value, list):
        return any(contains_pjud_document_secret(item) for item in value)
    if isinstance(value, dict):
        return any(
            _is_sensitive_key(key) or contains_pjud_document_secret(child)
            for key, child in value.items()
        )
    return isinstance(value, str) and _is_remote_document_string(value)
