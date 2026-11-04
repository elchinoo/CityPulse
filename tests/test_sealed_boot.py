"""Sealed boot end to end: a container started with only APGKIT_SEALED has no database URL until the operator unseals it, so the schema
must be created by the unseal step (`citypulse start` cannot migrate before that). Runs in a fresh interpreter because the sealed
state is read when citypulse.api is imported."""
import json
import os
import subprocess
import sys
import textwrap

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

import psycopg
from apgkit import sealed as sealing

PASSPHRASE = "correct-horse-battery-staple-9"  # pragma: allowlist secret

SCRIPT = textwrap.dedent("""
    import json, os
    from apgkit import sealed as sealing
    from fastapi.testclient import TestClient
    from citypulse import api
    out = {}
    with TestClient(api.app) as c:
        out["sealed_api"] = c.get("/api/status").status_code
        out["healthz"] = c.get("/healthz").status_code
        key = c.get("/_seal/key").json()["public_key"]
        out["unseal"] = c.post("/_seal/unseal", json=sealing.make_envelope(key, os.environ["TEST_PASSPHRASE"])).status_code
        out["after"] = c.get("/api/status").status_code
    print("RESULT" + json.dumps(out))
""")


def test_sealed_start_creates_the_schema_after_unseal():
    url = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    blob = sealing.seal({"DATABASE_URL": url}, PASSPHRASE)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CITYPULSE_", "DATABASE_", "DB_EXTERNAL", "PSQL_", "APGKIT_", "TEST_DATABASE"))}
    env.update(APGKIT_SEALED=blob, TEST_PASSPHRASE=PASSPHRASE, CITYPULSE_ROLE="api", CITYPULSE_API_KEY_FILE="/nonexistent/key")
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=180, check=False)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")), None)
    assert line, proc.stderr[-2000:]
    out = json.loads(line[len("RESULT"):])
    assert out["sealed_api"] == 503 and out["healthz"] == 200
    assert out["unseal"] == 200 and out["after"] != 503
    with psycopg.connect(url) as conn:
        tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"sensor", "api_token", "reading"} <= tables, tables       # created by the migrations that ran after unseal


def test_start_skips_migrate_only_when_sealed(monkeypatch):
    from citypulse import cli
    calls = []
    monkeypatch.setattr(cli.migrate, "run", lambda: calls.append("migrate") or [])
    monkeypatch.setattr(cli, "_serve", lambda: calls.append("serve"))
    monkeypatch.setattr(cli, "_check_sim", lambda: None)
    monkeypatch.delenv("APGKIT_SEALED", raising=False)
    monkeypatch.delenv("APGKIT_SEALED_FILE", raising=False)
    cli.main(["start"])
    assert calls == ["migrate", "serve"]
    calls.clear()
    monkeypatch.setenv("APGKIT_SEALED", sealing.seal({"DATABASE_URL": "postgresql://u:p@h/d"}, PASSPHRASE))
    cli.main(["start"])
    assert calls == ["serve"]
