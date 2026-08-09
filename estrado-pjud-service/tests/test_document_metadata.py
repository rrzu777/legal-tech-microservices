import json
from pathlib import Path

import pytest

from app.document_metadata import (
    contains_pjud_document_secret,
    extract_pjud_document_sources,
    sanitize_pjud_case_external_payload,
    sanitize_pjud_movement_payload,
)


CONTRACT_CASES = json.loads(
    Path(__file__).with_name("fixtures").joinpath("pjud_document_metadata.json").read_text()
)


@pytest.mark.parametrize("contract_case", CONTRACT_CASES, ids=lambda case: case["name"])
def test_derives_only_stable_non_secret_sources(contract_case):
    assert extract_pjud_document_sources(
        contract_case["movement_input"],
        contract_case["movement_identity"],
    ) == contract_case["expected_sources"]


@pytest.mark.parametrize("contract_case", CONTRACT_CASES, ids=lambda case: case["name"])
def test_sanitizes_movement_payloads(contract_case):
    sanitized = sanitize_pjud_movement_payload(contract_case["movement_input"])
    assert sanitized == contract_case["expected_sanitized_movement"]
    assert contains_pjud_document_secret(sanitized) is False


@pytest.mark.parametrize("contract_case", CONTRACT_CASES, ids=lambda case: case["name"])
def test_recursively_sanitizes_case_payloads(contract_case):
    sanitized = sanitize_pjud_case_external_payload(contract_case["case_input"])
    assert sanitized == contract_case["expected_sanitized_case"]
    assert contains_pjud_document_secret(sanitized) is False


def test_detects_nested_credentials_before_sanitization():
    assert contains_pjud_document_secret(CONTRACT_CASES[0]["movement_input"])
    assert contains_pjud_document_secret(CONTRACT_CASES[0]["case_input"])


def test_does_not_classify_safe_availability_flags_as_credentials():
    assert contains_pjud_document_secret({
        "has_remote_document": True,
        "certificado_disponible": True,
        "document_count": 2,
    }) is False


def test_preserves_distinct_ordinals_when_pjud_repeats_certificate_code():
    movement = {
        "documentos_adicionales": [
            {"codigo": "CERT-1", "label": "Certificado", "url": "/documento/1", "token": "first"},
            {"codigo": " CERT-1 ", "label": "Certificado repetido", "url": "/documento/2", "token": "second"},
        ],
    }
    sources = extract_pjud_document_sources(movement, "movement-1")
    assert len(sources) == 2
    assert sources[0]["source_id"] != sources[1]["source_id"]


def test_recursive_sanitizer_preserves_safe_nulls_but_removes_remote_strings():
    payload = {
        "safe_null": None,
        "items": [None, "visible", "https://consulta.pjud.cl/documento/secret"],
    }

    assert sanitize_pjud_case_external_payload(payload) == {
        "safe_null": None,
        "items": [None, "visible"],
    }
