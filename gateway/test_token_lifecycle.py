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
from gateway.main import REDIRECTS, SCOPES, SingleUserGoogle

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
