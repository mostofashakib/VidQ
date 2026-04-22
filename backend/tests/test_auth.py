"""Auth endpoint integration tests (AUTH_ENABLED=false, set in conftest)."""


def test_auth_status_disabled(client):
    r = client.get("/auth/status")
    assert r.status_code == 200
    assert r.json() == {"auth_enabled": False}


def test_login_when_auth_disabled_returns_bypass_token(client):
    r = client.post("/auth/login", json={"password": "anything"})
    assert r.status_code == 200
    assert r.json()["token"] == "temp-bypass-token"


def test_protected_endpoint_accessible_without_real_token(client):
    """With auth disabled every token (including empty string) should be accepted."""
    r = client.get("/videos", headers={"Authorization": "Bearer whatever"})
    assert r.status_code == 200
