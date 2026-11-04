"""Sealed boot: with APGKIT_SEALED set the app serves only /_seal/* and /healthz until the operator unseals it."""
import importlib

import pytest
from apgkit import config
from apgkit import sealed as sealing
from fastapi.testclient import TestClient

from citypulse import api

PASS = "correct-horse-battery-staple-9"


@pytest.fixture
def sealed_app(monkeypatch):
    blob = sealing.seal({"DATABASE_URL": "postgresql://u:p@127.0.0.1:1/none", "CITYPULSE_INGEST_TOKEN": "t" * 40}, PASS)
    monkeypatch.setenv("APGKIT_SEALED", blob)
    from citypulse import migrate
    monkeypatch.setattr(migrate, "run", lambda: [])    # the unseal step migrates; this test has no database (see test_sealed_boot.py)
    app_module = importlib.reload(api)          # the sealed state is read when the module is imported
    yield app_module
    monkeypatch.delenv("APGKIT_SEALED")
    sealing.set_active(None)
    importlib.reload(api)


def test_sealed_until_unsealed(sealed_app):
    with TestClient(sealed_app.app) as c:
        assert c.get("/api/status").status_code == 503
        assert c.get("/healthz").status_code == 200
        assert c.get("/_seal/status").json() == {"state": "sealed"}
        key = c.get("/_seal/key").json()["public_key"]
        bad = c.post("/_seal/unseal", json=sealing.make_envelope(key, "wrong-wrong-wrong-wrong-1"))
        assert bad.status_code == 401
        key = c.get("/_seal/key").json()["public_key"]
        ok = c.post("/_seal/unseal", json=sealing.make_envelope(key, PASS))
        assert ok.json() == {"state": "unsealed"}
        assert c.get("/api/status").status_code != 503
        assert config.read_secret("CITYPULSE_INGEST_TOKEN") == "t" * 40
        assert "127.0.0.1:1" in config.database_url()


def test_no_blob_changes_nothing(client):
    assert not api.sealed.enabled
    assert client.get("/healthz").status_code == 200


def test_oidc_client_secret_is_not_read_at_import(monkeypatch):
    from citypulse import core
    monkeypatch.setenv("CITYPULSE_OIDC_ISSUER", "https://idp.example.test")
    calls = []
    monkeypatch.setattr(core, "read_secret", lambda name, *a: calls.append(name) or "")
    fed = core._federation()
    assert fed.enabled and calls == []
