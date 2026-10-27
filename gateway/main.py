"""Single-user Google OAuth MCP gateway; backend stays private."""
import logging
import os
from pathlib import Path
import uuid

from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import RefreshToken, TokenError
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from cryptography.fernet import Fernet
import httpx2

SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email']
REDIRECTS = ['http://127.0.0.1/callback', 'http://127.0.0.1:*/callback',
    'http://localhost:*/callback', 'https://chatgpt.com/connector_platform_oauth_redirect',
    'https://chatgpt.com/connector/oauth/*']

def secret(name):
    value = Path(os.environ[name + '_FILE']).read_text().strip()
    if not value: raise ValueError('Empty credential file: ' + name)
    return value

def permitted_user(claims, allowed_email):
    verified = claims.get('email_verified')
    return (bool(claims.get('sub')) and isinstance(claims.get('email'), str)
        and claims['email'].casefold() == allowed_email.casefold()
        and (verified is True or isinstance(verified, str) and verified.casefold() == 'true'))

class SingleUserGoogle(GoogleProvider):
    def __init__(self, *, allowed_email_file, **kwargs):
        self.allowed_email_file = Path(allowed_email_file)
        super().__init__(**kwargs)

    async def active_mapping(self, token, token_use='access'):
        # These private stores are covered by lifecycle tests against pinned FastMCP.
        try:
            payload = self.jwt_issuer.verify_token(token, expected_token_use=token_use)
        except Exception:
            return None
        mapping = await self._jti_mapping_store.get(key=payload['jti'])
        if mapping is None:
            return None
        revoked = await self._client_storage.get(key=mapping.upstream_token_id,
            collection='home-memory-revoked-grants')
        if revoked is not None or await self._upstream_token_store.get(key=mapping.upstream_token_id) is None:
            return None
        return mapping

    async def load_refresh_token(self, client, refresh_token):
        result = await super().load_refresh_token(client, refresh_token)
        if result is None or await self.active_mapping(refresh_token, 'refresh') is None:
            return None
        return result

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if await self.active_mapping(refresh_token.token, 'refresh') is None:
            raise TokenError('invalid_grant', 'Session revoked or expired')
        result = await super().exchange_refresh_token(client, refresh_token, scopes)
        if await self.active_mapping(result.access_token) is None:
            raise TokenError('invalid_grant', 'Session revoked during refresh')
        return result

    async def revoke_token(self, token):
        token_use = 'refresh' if isinstance(token, RefreshToken) else 'access'
        mapping = await self.active_mapping(token.token, token_use)
        if mapping is not None:
            # Persistent tombstone also rejects a concurrent refresh that rewrites
            # the upstream record. Other independently authorized grants remain valid.
            await self._client_storage.put(key=mapping.upstream_token_id,
                collection='home-memory-revoked-grants', value={'revoked': True})
        await super().revoke_token(token)
        if mapping is not None:
            await self._upstream_token_store.delete(key=mapping.upstream_token_id)
            await self._jti_mapping_store.delete(key=mapping.jti)

    async def load_access_token(self, token):
        if await self.active_mapping(token) is None:
            return None
        result = await super().load_access_token(token)
        allowed = self.allowed_email_file.read_text().strip()
        if result is None or not allowed or not permitted_user(result.claims or {}, allowed) or await self.active_mapping(token) is None:
            return None
        return result

def make_server(provider, backend):
    server = FastMCP('Home AI Memory', auth=provider)
    async def call(method, path, *, body=None, params=None):
        if get_access_token() is None: raise PermissionError('Authentication required')
        response = await backend.request(method, path, json=body, params=params)
        response.raise_for_status()
        return response.json()

    @server.tool(annotations={'readOnlyHint': True, 'openWorldHint': False},
                 meta={'securitySchemes':[{'type':'oauth2','scopes':SCOPES}]})
    async def search_memories(query: str, limit: int = 10):
        if not 0 < len(query) <= 2000: raise ValueError('Query length out of bounds')
        return (await call('GET', '/v1/memories/search', params={'q':query,'limit':max(1,min(limit,100))}))['items']

    @server.tool(annotations={'readOnlyHint': True, 'openWorldHint': False},
                 meta={'securitySchemes':[{'type':'oauth2','scopes':SCOPES}]})
    async def get_usage_stats(hours: int = 24):
        return await call('GET', '/v1/usage',
                          params={'hours': max(1, min(hours, 720))})

    @server.tool(annotations={'readOnlyHint': True, 'openWorldHint': False},
                 meta={'securitySchemes':[{'type':'oauth2','scopes':SCOPES}]})
    async def list_memories(limit: int = 50, offset: int = 0):
        return (await call('GET','/v1/memories',params={'limit':max(1,min(limit,200)), 'offset':max(0,offset)}))['items']

    @server.tool(annotations={'readOnlyHint': True, 'openWorldHint': False},
                 meta={'securitySchemes':[{'type':'oauth2','scopes':SCOPES}]})
    async def get_memory(memory_id: str):
        return await call('GET','/v1/memories/'+str(uuid.UUID(memory_id)))

    @server.tool(annotations={'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False},
                 meta={'securitySchemes':[{'type':'oauth2','scopes':SCOPES}]})
    async def ingest_memory(source: str, source_record_id: str, text: str):
        if not 0 < len(text) <= 200000 or not 0 < len(source) <= 64 or not 0 < len(source_record_id) <= 512:
            raise ValueError('Source fields out of bounds')
        return await call('POST','/v1/ingest',body={'source':source,'source_record_id':source_record_id,'text':text})

    @server.tool(annotations={'readOnlyHint': False, 'destructiveHint': False, 'openWorldHint': False},
                 meta={'securitySchemes':[{'type':'oauth2','scopes':SCOPES}]})
    async def add_memory(content: str, source_record_id: str):
        if not 0 < len(content) <= 1000 or not 0 < len(source_record_id) <= 512:
            raise ValueError('Memory fields out of bounds')
        return await call('POST','/v1/memories',body={'content':content,'source_record_id':source_record_id})

    return server

def create_app():
    base = os.getenv('PUBLIC_BASE_URL','https://memory.example.com')
    if base != 'https://memory.example.com': raise ValueError('Review the configured public hostname')
    storage = FernetEncryptionWrapper(key_value=DiskStore(directory=os.getenv('OAUTH_STATE_DIR','/state/oauth'), max_size=134217728),
        fernet=Fernet(secret('STORAGE_ENCRYPTION_KEY').encode()))
    provider = SingleUserGoogle(allowed_email_file=os.environ['ALLOWED_EMAIL_FILE'],
        client_id=secret('GOOGLE_CLIENT_ID'), client_secret=secret('GOOGLE_CLIENT_SECRET'),
        base_url=base, redirect_path='/oauth2/callback', required_scopes=SCOPES,
        valid_scopes=SCOPES, jwt_signing_key=secret('JWT_SIGNING_KEY').encode(), client_storage=storage,
        allowed_client_redirect_uris=REDIRECTS, require_authorization_consent=True,
        cache_ttl_seconds=60, fastmcp_access_token_expiry_seconds=900,
        fallback_refresh_token_expiry_seconds=2592000, forward_resource=False, enable_cimd=False)
    backend = httpx2.AsyncClient(base_url='http://ai-memory-api:8080',
        headers={'Authorization':'Bearer '+secret('BACKEND_KEY')},timeout=90)
    server = make_server(provider, backend)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpx2').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('fastmcp.server.auth').setLevel(logging.WARNING)
    app = server.http_app(path='/mcp', stateless_http=True, json_response=True,
        allowed_hosts=['memory.example.com','ai-memory-oauth:*','localhost:*','127.0.0.1:*'],
        allowed_origins=['https://memory.example.com'])
    original = app.router.lifespan_context
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original(app): yield
        finally: await backend.aclose()
    app.router.lifespan_context = lifespan
    return app
