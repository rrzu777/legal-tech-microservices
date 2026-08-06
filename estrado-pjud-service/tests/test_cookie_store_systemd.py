from pathlib import Path

from app.cookie_store import DEFAULT_COOKIE_STORE_PATH


ROOT = Path(__file__).resolve().parents[2]


def test_cookie_store_default_lives_outside_git_checkout():
    assert DEFAULT_COOKIE_STORE_PATH == "/var/lib/estrado-pjud/cookies.json"


def test_worker_owns_private_state_directory_and_api_can_read_its_group():
    worker = (ROOT / "ops/systemd/estrado-pjud-worker.service").read_text()
    api = (ROOT / "ops/systemd/estrado-pjud.service").read_text()
    assert "StateDirectory=estrado-pjud" in worker
    assert "StateDirectoryMode=0750" in worker
    assert "SupplementaryGroups=estrado" in api
