"""CityPulse federated: people and services authenticate with the ecosystem identity provider, and roles decide what they may do."""
import os
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("set TEST_DATABASE_URL to run DB tests", allow_module_level=True)

from apgkit.auth import Federation, RelyingParty, TokenVerifier  # noqa: E402
from joserfc import jwt  # noqa: E402
from joserfc.jwk import RSAKey  # noqa: E402

from citypulse import api, core, db, gateway, migrate  # noqa: E402
from citypulse.db import connect  # noqa: E402

ISS, AUD = "https://id.example.org", "citypulse"
KEY = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
CSRF = {"X-CityPulse-CSRF": "1"}
READING = {"readings": [{"sensor": "temperature-d01-0001", "type": "temperature", "district": "d01", "value": 20.0}]}


class Provider:
    def __init__(self):
        self.nonce = None
        self.id_claims = {"sub": "usr_1", "name": "Ada", "email": "ada@example.org", "roles": ["citypulse:operator"]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISS, "jwks_uri": f"{ISS}/jwks", "authorization_endpoint": f"{ISS}/oauth/authorize",
                                             "token_endpoint": f"{ISS}/oauth/token", "end_session_endpoint": f"{ISS}/oauth/logout"})
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if path == "/oauth/token":
            claims = {"iss": ISS, "aud": AUD, "exp": int(time.time()) + 300, "nonce": self.nonce, **self.id_claims}
            return httpx.Response(200, json={"id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY), "access_token": "x"})
        return httpx.Response(404)

    def http(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False)


def access(**claims) -> dict:
    base = {"iss": ISS, "aud": AUD, "sub": "usr_1", "exp": int(time.time()) + 600, "sub_type": "user"}
    base.update(claims)
    return {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1", "typ": "at+jwt"}, base, KEY)}


def service(scope: str) -> dict:
    return access(sub="svc-1", sub_type="service", scope=scope, client_id="svc-1")


@pytest.fixture(autouse=True)
def _federated(monkeypatch, client):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    for v in ("CITYPULSE_API_KEY", "CITYPULSE_ALLOW_ANONYMOUS", "PAGERLENS_URL", "PAGERLENS_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CITYPULSE_API_KEY_FILE", "/nonexistent/key")
    db.reset_pool()
    core.auth.limiter.clear()
    core.local._setup_code = None
    core.clear_token_cache()
    gateway._inflight = 0
    gateway._known.update(count=0, at=0.0)
    gateway._bursts._at = 0.0
    client.cookies.clear()
    with connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.commit()
    migrate.run()
    provider = Provider()
    verifier = TokenVerifier(ISS, AUD, http=provider.http())
    monkeypatch.setattr(core.auth, "verifier", verifier)
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, verifier, RelyingParty(ISS, AUD, None, http=provider.http()), True))
    client.provider = provider
    yield
    db.reset_pool()


def test_roles_decide_what_a_person_may_do(client):
    admin, operator = access(sub="usr_3", roles=["citypulse:admin"]), access(roles=["citypulse:operator"])
    viewer, stranger = access(sub="usr_2", roles=["citypulse:viewer"]), access(sub="usr_4", roles=["otherapp:admin"])
    for who in (admin, operator, viewer):
        assert client.get("/api/status", headers=who).status_code == 200
        assert client.get("/api/sensors", headers=who).status_code == 200
    assert client.get("/api/status", headers=stranger).status_code == 403                    # signed in, but no role in this app
    assert client.post("/api/chaos/stop", headers=admin).status_code == 200
    assert client.post("/api/chaos/stop", headers=operator).status_code == 200               # operators run the simulated city
    assert client.post("/api/chaos/stop", headers=viewer).status_code == 403
    assert client.get("/api/tokens", headers=operator).status_code == 403                    # but do not manage tokens or settings
    assert client.post("/api/tokens", headers=operator, json={"name": "x", "scope": "full"}).status_code == 403
    assert client.post("/api/admin-password", headers=operator, json={"new": "x" * 14}).status_code == 403
    assert client.get("/api/tokens", headers=admin).status_code == 200
    assert client.get("/metrics", headers=operator).status_code == 403                       # scraping is a service permission
    assert client.post("/api/readings", headers=operator, json=READING).status_code == 403   # nor do people push readings
    assert client.get("/metrics", headers=admin).status_code == 200


def test_service_scopes_are_each_narrow(client):
    ing, rd, met = service("citypulse.ingest"), service("citypulse.read"), service("citypulse.metrics")
    assert client.post("/api/readings", headers=ing, json=READING).json()["accepted"] == 1
    assert client.post("/api/readings", headers=rd, json=READING).status_code == 403
    assert client.post("/api/readings", headers=met, json=READING).status_code == 403
    assert client.get("/api/status", headers=ing).status_code == 403                         # ingest cannot read
    assert client.get("/api/status", headers=rd).status_code == 200
    assert client.get("/metrics", headers=met).status_code == 200
    assert client.get("/metrics", headers=ing).status_code == 403
    assert client.get("/api/status", headers=met).status_code == 403
    assert client.post("/api/chaos/stop", headers=rd).status_code == 403
    assert client.get("/api/tokens", headers=rd).status_code == 403
    assert client.post("/api/readings", headers=service("trustdesk.requests.write"), json=READING).status_code == 403   # another app's scope


def test_tokens_for_another_app_or_with_a_bad_signature_are_refused(client):
    assert client.get("/api/status", headers=access(aud="some-other-app", roles=["citypulse:admin"])).status_code == 401
    assert client.get("/api/status", headers=access(exp=int(time.time()) - 3600, roles=["citypulse:admin"])).status_code == 401
    assert client.get("/api/status", headers=access(iss="https://evil.example.org", roles=["citypulse:admin"])).status_code == 401
    other = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
    forged = {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1"}, {"iss": ISS, "aud": AUD, "sub": "x", "exp": int(time.time()) + 60,
                                                                                      "roles": ["citypulse:admin"]}, other)}
    assert client.get("/api/status", headers=forged).status_code == 401


def test_local_token_scopes_still_hold_beside_federation(client):
    admin = access(sub="usr_3", roles=["citypulse:admin"])

    def mk(scope):
        r = client.post("/api/tokens", headers=admin, json={"name": scope, "scope": scope})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    ing, rd, met, full = mk("ingest"), mk("read"), mk("metrics"), mk("full")
    assert client.post("/api/readings", headers=ing, json=READING).json()["accepted"] == 1
    assert client.get("/api/status", headers=ing).status_code == 403
    assert client.post("/api/readings", headers=rd, json=READING).status_code == 403
    assert client.get("/api/status", headers=rd).status_code == 200
    assert client.post("/api/chaos/stop", headers=rd).status_code == 403
    assert client.get("/metrics", headers=met).status_code == 200
    assert client.get("/api/status", headers=met).status_code == 403
    assert client.post("/api/chaos/stop", headers=full).status_code == 200
    assert client.get("/api/tokens", headers=full).status_code == 200
    tid = client.get("/api/tokens", headers=admin).json()[0]["id"]
    assert client.get("/api/status", headers=full).status_code == 200
    assert client.delete(f"/api/tokens/{tid}", headers=admin).status_code == 200
    assert client.get("/api/status", headers=full).status_code == 401                        # revocation is immediate on this process


def test_single_sign_on_login_creates_a_role_bound_session(client):
    r = client.get("/auth/login", params={"next": "/#burst"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISS}/oauth/authorize?")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://testserver/auth/callback"] and q["client_id"] == [AUD]
    client.provider.nonce = q["nonce"][0]
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/#burst" and "cp_session" in cb.cookies
    s = client.get("/api/session").json()
    assert s["state"] == "logged_in" and s["user"]["name"] == "Ada" and s["user"]["roles"] == ["citypulse:operator"] and s["user"]["source"] == "oidc"
    assert "city:control" in s["user"]["permissions"] and "settings:manage" not in s["user"]["permissions"]
    assert s["sso"]["login_url"] == "/auth/login" and s["sso"]["issuer"] == ISS
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/chaos/stop", headers=CSRF).status_code == 200
    assert client.get("/api/tokens").status_code == 403                                      # an operator is not an administrator
    out = client.post("/api/logout", headers=CSRF).json()
    assert out["logout_url"].startswith(f"{ISS}/oauth/logout?") and "id_token_hint=" in out["logout_url"]
    assert client.get("/api/status").status_code == 401


def test_sso_refuses_people_without_a_role_here_and_replayed_or_foreign_logins(client):
    client.provider.id_claims = {"sub": "usr_9", "name": "Nobody", "roles": ["otherapp:admin"]}
    r = client.get("/auth/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    client.provider.nonce = q["nonce"][0]
    cb = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert cb.headers["location"].startswith("/?sso_error=") and "cp_session" not in cb.cookies
    assert client.get("/api/status").status_code == 401
    again = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert again.headers["location"].startswith("/?sso_error=")                              # the state was single-use
    assert client.get("/auth/callback", params={"error": "access_denied"}, follow_redirects=False).headers["location"].startswith("/?sso_error=")


def test_setup_is_not_offered_when_federated_and_local_login_can_be_turned_off(client, monkeypatch):
    s = client.get("/api/session").json()
    assert s["state"] == "logged_out" and s["sso"]["issuer"] == ISS                          # no setup screen: people sign in with SSO
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, core.auth.verifier, api.fed.rp, False))
    assert client.post("/api/login", json={"password": "whatever-it-is"}, headers=CSRF).status_code == 403


def test_a_federated_session_ends_when_sessions_are_cleared(client):
    r = client.get("/auth/login", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    client.provider.nonce = q["nonce"][0]
    client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert client.get("/api/status").status_code == 200
    with connect() as c:
        c.execute("DELETE FROM web_session")
        c.commit()
    assert client.get("/api/status").status_code == 401


def test_sso_endpoints_are_404_when_federation_is_off(client, monkeypatch):
    monkeypatch.setattr(api, "fed", Federation("", AUD, None, None, True))
    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/auth/callback", follow_redirects=False).status_code == 404
    assert client.get("/api/session").json()["sso"] is None
