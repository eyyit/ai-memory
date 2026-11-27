import time
from unittest.mock import AsyncMock, patch

import anyio
import pytest
from cryptography.fernet import Fernet
from fastmcp.server.auth.auth import AccessToken, OAuthClientInformationFull
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import RefreshToken, TokenError

from fastmcp.server.auth.oauth_proxy.proxy import (
    JTIMapping,
    RefreshTokenMetadata,
    UpstreamTokenSet,
    _hash_token,
)
from gateway.main import REDIRECTS, SCOPES, SingleUserGoogle, make_oauth_storage, make_server
import httpx2

CLIENT_ID = "synthetic-mcp-client"
CLAIMS = {"sub": "synthetic-google-user", "email": "owner@example.com", "email_verified": True}


def provider(state_dir, allowed_file, key):
    storage = FernetEncryptionWrapper(
        key_value=DiskStore(directory=state_dir), fernet=Fernet(key)
    )
    instance = SingleUserGoogle(
        allowed_email_file=allowed_file,
        client_id="synthetic-google-client",
        client_secret="synthetic-google-secret",
        base_url="https://memory.example.com",
        redirect_path="/oauth2/callback",
        required_scopes=SCOPES,
        valid_scopes=SCOPES,
        client_storage=storage,
        jwt_signing_key=b"01234567890123456789012345678901",
        allowed_client_redirect_uris=REDIRECTS,
        fallback_refresh_token_expiry_seconds=3600,
        fastmcp_access_token_expiry_seconds=900,
        forward_resource=False,
        enable_cimd=False,
    )
    # FastMCP 4.1.0 initializes its JWT issuer while constructing OAuth routes.
    instance.get_routes()
    return instance


async def seed_grant(instance, grant_id, *, client_id=CLIENT_ID):
    now = time.time()
    access_jti = f"access-{grant_id}"
    refresh_jti = f"refresh-{grant_id}"
    upstream_id = f"upstream-{grant_id}"
    access_jwt = instance.jwt_issuer.issue_access_token(
        client_id=client_id,
        scopes=SCOPES,
        jti=access_jti,
        expires_in=900,
        upstream_claims=CLAIMS,
    )
    refresh_jwt = instance.jwt_issuer.issue_refresh_token(
        client_id=client_id,
        scopes=SCOPES,
        jti=refresh_jti,
        expires_in=3600,
        upstream_claims=CLAIMS,
    )
    token_set = UpstreamTokenSet(
        upstream_token_id=upstream_id,
        access_token=f"synthetic-upstream-access-{grant_id}",
        refresh_token=f"synthetic-upstream-refresh-{grant_id}",
        refresh_token_expires_at=now + 3600,
        expires_at=now + 900,
        token_type="Bearer",
        scope=" ".join(SCOPES),
        client_id=client_id,
        created_at=now,
        raw_token_data={"access_token": f"synthetic-upstream-access-{grant_id}"},
    )
    await instance._upstream_token_store.put(
        key=upstream_id, value=token_set, ttl=3600
    )
    for jti in (access_jti, refresh_jti):
        await instance._jti_mapping_store.put(
            key=jti,
            value=JTIMapping(
                jti=jti, upstream_token_id=upstream_id, created_at=now
            ),
            ttl=3600,
        )
    await instance._refresh_token_store.put(
        key=_hash_token(refresh_jwt),
        value=RefreshTokenMetadata(
            client_id=client_id,
            scopes=SCOPES,
            expires_at=int(now + 3600),
            created_at=now,
        ),
        ttl=3600,
    )
    return {
        "access": access_jwt,
        "access_jti": access_jti,
        "refresh": refresh_jwt,
        "refresh_jti": refresh_jti,
        "upstream_id": upstream_id,
        "token_set": token_set,
    }


def client(client_id=CLIENT_ID):
    return OAuthClientInformationFull(client_id=client_id)


def verified_upstream_token():
    return AccessToken(
        token="synthetic-upstream-access",
        client_id="synthetic-google-client",
        scopes=SCOPES,
        expires_at=int(time.time()) + 600,
        claims=dict(CLAIMS),
    )


class SyntheticOAuthClient:
    async def refresh_token(self, **kwargs):
        assert kwargs["refresh_token"].startswith("synthetic-upstream-refresh-")
        return {
            "access_token": "synthetic-upstream-access-rotated",
            "refresh_token": "synthetic-upstream-refresh-rotated",
            "expires_in": 1800,
            "refresh_expires_in": 3600,
            "scope": " ".join(SCOPES),
            "token_type": "Bearer",
        }


class SyntheticOAuthClientContext:
    async def __aenter__(self):
        return SyntheticOAuthClient()

    async def __aexit__(self, *exc):
        return False


def test_refresh_rotates_signed_tokens_and_rejects_the_previous_refresh(tmp_path):
    async def run():
        allowed_file = tmp_path / "allowed-email"
        allowed_file.write_text("owner@example.com")
        key = Fernet.generate_key()
        p = provider(tmp_path / "oauth", allowed_file, key)
        grant = await seed_grant(p, "rotation")

        with patch.object(
            p,
            "_upstream_oauth_client",
            return_value=SyntheticOAuthClientContext(),
        ) as refresh_call:
            result = await p.exchange_refresh_token(
                client(),
                RefreshToken(
                    token=grant["refresh"], client_id=CLIENT_ID, scopes=SCOPES
                ),
                SCOPES,
            )

        assert result.access_token != grant["access"]
        assert result.refresh_token and result.refresh_token != grant["refresh"]
        p.jwt_issuer.verify_token(result.access_token, expected_token_use="access")
        p.jwt_issuer.verify_token(result.refresh_token, expected_token_use="refresh")
        assert refresh_call.call_count == 1
        assert await p.load_refresh_token(client(), grant["refresh"]) is None
        with pytest.raises(TokenError) as err:
            await p.exchange_refresh_token(
                client(),
                RefreshToken(
                    token=grant["refresh"], client_id=CLIENT_ID, scopes=SCOPES
                ),
                SCOPES,
            )
        assert err.value.error == "invalid_grant"
        assert await p.load_refresh_token(client(), result.refresh_token) is not None

    anyio.run(run)


@pytest.mark.parametrize("revoked_kind", ["access", "refresh"])
def test_revoking_either_token_denies_the_whole_grant_and_preserves_other_grants(
    tmp_path, revoked_kind
):
    async def run():
        allowed_file = tmp_path / "allowed-email"
        allowed_file.write_text("owner@example.com")
        key = Fernet.generate_key()
        p = provider(tmp_path / "oauth", allowed_file, key)
        revoked = await seed_grant(p, f"revoke-{revoked_kind}")
        other = await seed_grant(p, f"other-{revoked_kind}")

        with patch.object(
            p._token_validator, "verify_token", new=AsyncMock(return_value=verified_upstream_token())
        ):
            assert await p.load_access_token(revoked["access"]) is not None
            assert await p.load_access_token(other["access"]) is not None

            token = (
                RefreshToken(
                    token=revoked["refresh"], client_id=CLIENT_ID, scopes=SCOPES
                )
                if revoked_kind == "refresh"
                else AccessToken(
                    token=revoked["access"], client_id=CLIENT_ID, scopes=SCOPES
                )
            )
            await p.revoke_token(token)

            assert await p.load_access_token(revoked["access"]) is None
            assert await p.load_refresh_token(client(), revoked["refresh"]) is None
            with pytest.raises(TokenError) as err:
                await p.exchange_refresh_token(
                    client(),
                    RefreshToken(
                        token=revoked["refresh"], client_id=CLIENT_ID, scopes=SCOPES
                    ),
                    SCOPES,
                )
            assert err.value.error == "invalid_grant"
            assert await p.load_access_token(other["access"]) is not None
            assert await p.load_refresh_token(client(), other["refresh"]) is not None

    anyio.run(run)


def test_revocation_tombstone_survives_provider_restart_and_blocks_restored_records(
    tmp_path,
):
    async def run():
        allowed_file = tmp_path / "allowed-email"
        allowed_file.write_text("owner@example.com")
        key = Fernet.generate_key()
        state_dir = tmp_path / "oauth"
        first = provider(state_dir, allowed_file, key)
        grant = await seed_grant(first, "restart")
        await first.revoke_token(
            AccessToken(token=grant["access"], client_id=CLIENT_ID, scopes=SCOPES)
        )

        # Recreate the provider on the same encrypted disk store, then restore the
        # upstream record and JTI links to model a refresh that raced with revoke.
        restarted = provider(state_dir, allowed_file, key)
        await restarted._upstream_token_store.put(
            key=grant["upstream_id"], value=grant["token_set"], ttl=3600
        )
        for jti in (grant["access_jti"], grant["refresh_jti"]):
            await restarted._jti_mapping_store.put(
                key=jti,
                value=JTIMapping(
                    jti=jti,
                    upstream_token_id=grant["upstream_id"],
                    created_at=time.time(),
                ),
                ttl=3600,
            )

        assert await restarted._client_storage.get(
            key=grant["upstream_id"], collection="home-memory-revoked-grants"
        ) is not None
        assert await restarted.load_refresh_token(client(), grant["refresh"]) is None
        assert await restarted.load_access_token(grant["access"]) is None

    anyio.run(run)


def test_registration_storage_isolated_from_active_grants(tmp_path):
    async def run():
        allowed_file = tmp_path / "allowed-email"
        allowed_file.write_text("owner@example.com")
        key = Fernet.generate_key()
        storage = make_oauth_storage(
            state_dir=tmp_path / "oauth",
            public_state_dir=tmp_path / "oauth-public",
            fernet_key=key,
            public_max_size=16384,
        )
        p = SingleUserGoogle(
            allowed_email_file=allowed_file,
            client_id="synthetic-google-client",
            client_secret="synthetic-google-secret",
            base_url="https://memory.example.com",
            redirect_path="/oauth2/callback",
            required_scopes=SCOPES,
            valid_scopes=SCOPES,
            client_storage=storage,
            jwt_signing_key=b"01234567890123456789012345678901",
            allowed_client_redirect_uris=REDIRECTS,
            fallback_refresh_token_expiry_seconds=3600,
            fastmcp_access_token_expiry_seconds=900,
            forward_resource=False,
            enable_cimd=False,
            register_quota_max=200,
        )
        p.get_routes()
        try:
            grant = await seed_grant(p, "isolated")
            for i in range(40):
                await p.register_client(
                    OAuthClientInformationFull(
                        client_id=f"dcr-client-{i}",
                        client_name=f"client-{i}-" + ("x" * 180),
                        redirect_uris=["http://127.0.0.1/callback"],
                    )
                )
            # Active grant records live in grant_store and are never evicted by DCR volume.
            assert await storage.grant_store.get(
                key=grant["upstream_id"], collection="mcp-upstream-tokens"
            ) is not None
            assert await storage.public_store.get(
                key=grant["upstream_id"], collection="mcp-upstream-tokens"
            ) is None
            assert await p.load_refresh_token(client(), grant["refresh"]) is not None
            with patch.object(
                p._token_validator,
                "verify_token",
                new=AsyncMock(return_value=verified_upstream_token()),
            ):
                assert await p.load_access_token(grant["access"]) is not None
        finally:
            await storage.close()

    anyio.run(run)


def test_register_bounds_payload_field_sizes_rate_limit_and_quota(tmp_path):
    async def run():
        allowed_file = tmp_path / "allowed-email"
        allowed_file.write_text("owner@example.com")
        key = Fernet.generate_key()
        storage = make_oauth_storage(
            state_dir=tmp_path / "oauth",
            public_state_dir=tmp_path / "oauth-public",
            fernet_key=key,
        )
        p = SingleUserGoogle(
            allowed_email_file=allowed_file,
            client_id="synthetic-google-client",
            client_secret="synthetic-google-secret",
            base_url="https://memory.example.com",
            redirect_path="/oauth2/callback",
            required_scopes=SCOPES,
            valid_scopes=SCOPES,
            client_storage=storage,
            jwt_signing_key=b"01234567890123456789012345678901",
            allowed_client_redirect_uris=REDIRECTS,
            forward_resource=False,
            enable_cimd=False,
            register_rate_limit_per_ip=4,
            register_rate_limit_global=10,
            register_quota_max=2,
        )
        backend = httpx2.AsyncClient(
            base_url="http://ai-memory-api:8080",
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json={})),
        )
        app = make_server(p, backend).http_app(
            path="/mcp",
            stateless_http=True,
            json_response=True,
            allowed_hosts=["memory.example.com"],
        )
        try:
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="https://memory.example.com",
                ) as http:
                    # 900KB client_name (F1 PoC) is rejected with 413 before reaching DCR handler.
                    huge = await http.post(
                        "/register",
                        json={
                            "client_name": "A" * 900_000,
                            "redirect_uris": ["http://127.0.0.1/callback"],
                        },
                        headers={"X-Real-IP": "198.51.100.1"},
                    )
                    assert huge.status_code == 413

                    # Oversized client_name within body limit is rejected with 400.
                    long_name = await http.post(
                        "/register",
                        json={
                            "client_name": "B" * 300,
                            "redirect_uris": ["http://127.0.0.1/callback"],
                        },
                        headers={"X-Real-IP": "198.51.100.2"},
                    )
                    assert long_name.status_code == 400

                    # Valid registrations succeed up to quota, then quota rejects with 400.
                    valid_body = {
                        "client_name": "legit-client",
                        "redirect_uris": ["http://127.0.0.1/callback"],
                    }
                    r1 = await http.post(
                        "/register", json=valid_body, headers={"X-Real-IP": "198.51.100.3"}
                    )
                    r2 = await http.post(
                        "/register", json=valid_body, headers={"X-Real-IP": "198.51.100.3"}
                    )
                    r3 = await http.post(
                        "/register", json=valid_body, headers={"X-Real-IP": "198.51.100.3"}
                    )
                    assert r1.status_code == 201
                    assert r2.status_code == 201
                    assert r3.status_code == 400

                    # Per-IP rate limit triggers 429 with Retry-After.
                    r4 = await http.post(
                        "/register", json=valid_body, headers={"X-Real-IP": "198.51.100.3"}
                    )
                    r5 = await http.post(
                        "/register", json=valid_body, headers={"X-Real-IP": "198.51.100.3"}
                    )
                    assert r4.status_code == 400
                    assert r5.status_code == 429
                    assert "retry-after" in {k.lower() for k in r5.headers}
        finally:
            await backend.aclose()
            await storage.close()

    anyio.run(run)


@pytest.mark.parametrize("revoke_kind", ["refresh", "access"])
def test_http_revocation_advertised_and_revokes_grant(tmp_path, revoke_kind):
    async def run():
        allowed_file = tmp_path / "allowed-email"
        allowed_file.write_text("owner@example.com")
        key = Fernet.generate_key()
        storage = make_oauth_storage(
            state_dir=tmp_path / "oauth",
            public_state_dir=tmp_path / "oauth-public",
            fernet_key=key,
        )
        p = SingleUserGoogle(
            allowed_email_file=allowed_file,
            client_id="synthetic-google-client",
            client_secret="synthetic-google-secret",
            base_url="https://memory.example.com",
            redirect_path="/oauth2/callback",
            required_scopes=SCOPES,
            valid_scopes=SCOPES,
            client_storage=storage,
            jwt_signing_key=b"01234567890123456789012345678901",
            allowed_client_redirect_uris=REDIRECTS,
            fallback_refresh_token_expiry_seconds=3600,
            fastmcp_access_token_expiry_seconds=900,
            forward_resource=False,
            enable_cimd=False,
        )
        backend = httpx2.AsyncClient(
            base_url="http://ai-memory-api:8080",
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json={"items": []})),
        )
        app = make_server(p, backend).http_app(
            path="/mcp",
            stateless_http=True,
            json_response=True,
            allowed_hosts=["memory.example.com"],
        )
        try:
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="https://memory.example.com",
                ) as http:
                    metadata = (
                        await http.get("/.well-known/oauth-authorization-server")
                    ).json()
                    assert (
                        metadata.get("revocation_endpoint")
                        == "https://memory.example.com/revoke"
                    )

                    reg = await http.post(
                        "/register",
                        json={
                            "client_name": "revoke-test-client",
                            "redirect_uris": ["http://127.0.0.1/callback"],
                        },
                    )
                    assert reg.status_code == 201
                    registered_id = reg.json()["client_id"]
                    grant = await seed_grant(p, f"http-revoke-{revoke_kind}", client_id=registered_id)

                    with patch.object(
                        p._token_validator,
                        "verify_token",
                        new=AsyncMock(return_value=verified_upstream_token()),
                    ):
                        headers = {
                            "Accept": "application/json, text/event-stream",
                            "Authorization": f"Bearer {grant['access']}",
                        }
                        mcp_body = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/list",
                            "params": {},
                        }
                        before = await http.post("/mcp", json=mcp_body, headers=headers)
                        assert before.status_code == 200

                        revoke_resp = await http.post(
                            "/revoke",
                            data={
                                "token": grant[revoke_kind],
                                "token_type_hint": (
                                    "refresh_token"
                                    if revoke_kind == "refresh"
                                    else "access_token"
                                ),
                                "client_id": registered_id,
                            },
                        )
                        assert revoke_resp.status_code == 200

                        after = await http.post("/mcp", json=mcp_body, headers=headers)
                        assert after.status_code == 401
                        assert await p.load_access_token(grant["access"]) is None
                        assert (
                            await p.load_refresh_token(
                                client(registered_id), grant["refresh"]
                            )
                            is None
                        )
                        refresh_resp = await http.post(
                            "/token",
                            data={
                                "grant_type": "refresh_token",
                                "refresh_token": grant["refresh"],
                                "client_id": registered_id,
                            },
                        )
                        assert refresh_resp.status_code in {400, 401}
                        assert refresh_resp.json()["error"] == "invalid_grant"
        finally:
            await backend.aclose()
            await storage.close()

    anyio.run(run)


def test_concurrent_refresh_only_succeeds_once(tmp_path):
    async def run():
        allowed_file = tmp_path / "allowed-email"
        allowed_file.write_text("owner@example.com")
        key = Fernet.generate_key()
        p = provider(tmp_path / "oauth", allowed_file, key)
        grant = await seed_grant(p, "concurrent")

        first_entered = anyio.Event()
        release_upstream = anyio.Event()
        upstream_calls = 0

        class SlowOAuthClient:
            async def refresh_token(self, **kwargs):
                nonlocal upstream_calls
                upstream_calls += 1
                first_entered.set()
                await release_upstream.wait()
                return {
                    "access_token": f"synthetic-upstream-access-rotated-{upstream_calls}",
                    "refresh_token": f"synthetic-upstream-refresh-rotated-{upstream_calls}",
                    "expires_in": 1800,
                    "refresh_expires_in": 3600,
                    "scope": " ".join(SCOPES),
                    "token_type": "Bearer",
                }

        class SlowOAuthClientContext:
            async def __aenter__(self):
                return SlowOAuthClient()

            async def __aexit__(self, *exc):
                return False

        successes = []
        errors = []

        async def do_refresh():
            try:
                res = await p.exchange_refresh_token(
                    client(),
                    RefreshToken(
                        token=grant["refresh"], client_id=CLIENT_ID, scopes=SCOPES
                    ),
                    SCOPES,
                )
                successes.append(res)
            except TokenError as exc:
                errors.append(exc)

        with patch.object(
            p, "_upstream_oauth_client", return_value=SlowOAuthClientContext()
        ):
            async with anyio.create_task_group() as tg:
                tg.start_soon(do_refresh)
                await first_entered.wait()
                tg.start_soon(do_refresh)
                await anyio.sleep(0.05)
                release_upstream.set()

        assert len(successes) == 1
        assert len(errors) == 1
        assert errors[0].error == "invalid_grant"
        assert upstream_calls == 1
        assert await p.load_refresh_token(client(), grant["refresh"]) is None
        assert await p.load_refresh_token(client(), successes[0].refresh_token) is not None

    anyio.run(run)
