from pydantic import SecretStr, ValidationError
import pytest


CAPABILITY = "c" * 64
GENERATION = "11111111-1111-4111-8111-111111111111"
GRANT_ID = "98200000-0000-4000-8000-000000000031"
JOB_ID = "98200000-0000-4000-8000-000000000041"
CLAIM_TOKEN = "98200000-0000-4000-8000-000000000099"
LAW_FIRM_ID = "98200000-0000-4000-8000-000000000001"
CREDENTIAL_ID = "98200000-0000-4000-8000-000000000021"
EXPECTED_CREDENTIALS_UPDATED_AT = "2026-08-23T12:00:00.000Z"


def _scope(**overrides):
    from worker.trial_scope import TrialScope

    values = {
        "capability": SecretStr(CAPABILITY),
        "runtime_generation": GENERATION,
        "trial_grant_id": GRANT_ID,
        "job_id": JOB_ID,
        "claim_token": CLAIM_TOKEN,
        "worker_id": "import-worker",
        "law_firm_id": LAW_FIRM_ID,
        "credential_id": CREDENTIAL_ID,
        "expected_credentials_updated_at": EXPECTED_CREDENTIALS_UPDATED_AT,
    }
    values.update(overrides)
    return TrialScope.model_validate(values)


def test_trial_scope_is_complete_immutable_and_secret_safe():
    """Catch tuple mutation or secret disclosure after the exact claim."""
    scope = _scope()

    assert str(scope.trial_grant_id) == GRANT_ID
    assert str(scope.credential_id) == CREDENTIAL_ID
    assert scope.expected_credentials_updated_at.isoformat() == (
        "2026-08-23T12:00:00+00:00"
    )
    assert scope.capability.get_secret_value() == CAPABILITY
    assert CAPABILITY not in repr(scope)
    with pytest.raises(ValidationError, match="frozen"):
        scope.worker_id = "other-worker"


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"capability": SecretStr("C" * 64)}, "invalid_trial_capability"),
        ({"capability": SecretStr("c" * 63)}, "invalid_trial_capability"),
        ({"worker_id": ""}, "invalid_trial_worker"),
        ({"worker_id": "worker\nforged"}, "invalid_trial_worker"),
        ({"job_id": None}, "UUID"),
        (
            {"expected_credentials_updated_at": "2026-08-23T12:00:00"},
            "invalid_trial_credential_revision",
        ),
    ],
)
def test_trial_scope_rejects_missing_or_noncanonical_tuple_without_secret_leak(
    overrides, error_code,
):
    """Catch an incomplete or header-injectable tuple reaching an effect boundary."""
    with pytest.raises(ValidationError, match=error_code) as exc_info:
        _scope(**overrides)

    assert CAPABILITY not in str(exc_info.value)
