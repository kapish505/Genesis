from pathlib import Path

import httpx
import pytest

from gateway.auth import TokenError, make_token, verify_token
from gateway.main import GatewaySettings, Services, create_app


@pytest.fixture
def gateway_test_env(tmp_path: Path):
    quota_db = str(tmp_path / "quotas.db")
    revocations = str(tmp_path / "revoked.json")
    settings = GatewaySettings()
    settings.secret = "test-secret-123456"
    settings.admin_secret = "test-admin-secret"
    settings.quota_db = quota_db
    settings.revocation_file = revocations
    settings.daily_quota = 5
    services = Services(settings)
    app = create_app(services)
    return app, settings, services


@pytest.mark.asyncio
async def test_auth_token_lifecycle():
    secret = "my-secret-key"
    token = make_token(
        secret, "agent-1", tools=["fetch", "extract"], hosts=["example.com"], ttl_s=60
    )
    scope = verify_token(token, secret=secret)
    assert scope.agent_id == "agent-1"
    assert scope.allows_tool("fetch")
    assert scope.allows_tool("extract")
    assert not scope.allows_tool("search")
    assert scope.allows_host("example.com")
    assert not scope.allows_host("evil.com")

    # Tampered token
    tampered = token[:-4] + "abcd"
    with pytest.raises(TokenError):
        verify_token(tampered, secret=secret)

    # Wrong secret
    with pytest.raises(TokenError):
        verify_token(token, secret="wrong-secret")

    # Revoked token
    with pytest.raises(TokenError, match="revoked"):
        verify_token(token, secret=secret, revoked=frozenset({scope.token_id}))


@pytest.mark.asyncio
async def test_gateway_health_and_admin(gateway_test_env):
    app, settings, _services = gateway_test_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # /health
        res = await client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"

        # Admin check without secret
        res = await client.post("/admin/check", json={"token": "abc"})
        assert res.status_code == 403

        # Admin check with valid token
        token = make_token(
            settings.secret, "agent-admin-test", tools=["fetch"], hosts=["example.com"], ttl_s=300
        )
        res = await client.post(
            "/admin/check",
            json={"token": token},
            headers={"X-Genesis-Admin": settings.admin_secret},
        )
        assert res.status_code == 200
        info = res.json()
        assert info["valid"] is True
        assert info["agent_id"] == "agent-admin-test"
        assert info["tools"] == ["fetch"]
        assert info["remaining"] == 5

        # Admin revoke
        res = await client.post(
            "/admin/revoke",
            json={"token": token},
            headers={"X-Genesis-Admin": settings.admin_secret},
        )
        assert res.status_code == 200

        # Token should now be invalid
        res = await client.post(
            "/admin/check",
            json={"token": token},
            headers={"X-Genesis-Admin": settings.admin_secret},
        )
        assert res.status_code == 200
        assert res.json()["valid"] is False


@pytest.mark.asyncio
async def test_gateway_extract_and_quotas(gateway_test_env):
    app, settings, _services = gateway_test_env
    token = make_token(settings.secret, "agent-worker", tools=["extract"], hosts=[], ttl_s=300)
    auth_header = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Extract from inline content
        payload = {
            "agent_id": "agent-worker",
            "payload": {
                "content": "<html><body><h1>Menu</h1><p>Paneer butter masala served today</p></body></html>",
                "format": "html",
            },
        }
        res = await client.post("/extract", json=payload, headers=auth_header)
        assert res.status_code == 200
        data = res.json()
        assert "Paneer" in data["text"]

        # Wrong agent_id in body
        bad_payload = dict(payload)
        bad_payload["agent_id"] = "impostor"
        res = await client.post("/extract", json=bad_payload, headers=auth_header)
        assert res.status_code == 403

        # Unauthorized tool
        res = await client.post("/search", json=payload, headers=auth_header)
        assert res.status_code == 403
