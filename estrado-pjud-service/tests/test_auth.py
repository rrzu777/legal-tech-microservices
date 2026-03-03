import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import verify_api_key


app = FastAPI()


@app.get("/protected")
async def protected(api_key: str = verify_api_key):
    return {"ok": True}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-secret")
    return TestClient(app)


def test_valid_api_key(client):
    resp = client.get("/protected", headers={"Authorization": "Bearer test-secret"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_missing_auth_header(client):
    resp = client.get("/protected")
    assert resp.status_code == 401


def test_wrong_api_key(client):
    resp = client.get("/protected", headers={"Authorization": "Bearer wrong-key"})
    assert resp.status_code == 401


def test_malformed_auth_header(client):
    resp = client.get("/protected", headers={"Authorization": "Basic abc123"})
    assert resp.status_code == 401
