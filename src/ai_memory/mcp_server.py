import os
import logging
import uuid
import anyio
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from starlette.middleware.authentication import AuthenticationMiddleware
from .config import Settings
from .usage import UsageService, report

def create_http_mcp(service_getter, authenticator, settings):
    class Verifier:
        async def verify_token(self, token):
            principal = await anyio.to_thread.run_sync(authenticator.authenticate, token)
            if principal is None: return None
            scopes = set(principal.scopes)
            if 'admin' in scopes: scopes.update(['read', 'write'])
            return AccessToken(token=token, client_id=principal.client_id, scopes=sorted(scopes),
                resource=settings.oauth_resource or None, claims={'instance_id': principal.instance_id})

    server = FastMCP('AI Memory', stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=list(settings.mcp_hosts), allowed_origins=list(settings.mcp_origins)))

    def authorize(scope):
        token = get_access_token()
        if token is None or (scope not in token.scopes and 'admin' not in token.scopes):
            raise PermissionError('Insufficient permission')
        return token.claims['instance_id']

    def observed_service():
        token = get_access_token()
        return UsageService(service_getter(), token.client_id, 'mcp')

    @server.tool()
    def get_usage_stats(hours: int = 24):
        iid = authorize('read')
        return report(service_getter(), iid, hours)

    @server.tool()
    def search_memories(query: str, limit: int = 10):
        iid = authorize('read')
        if not query or len(query) > 2000: raise ValueError('Query length out of bounds')
        return observed_service().search(iid, query, max(1, min(limit, 100)))

    @server.tool()
    def list_memories(limit: int = 50, offset: int = 0):
        return observed_service().list(authorize('read'), limit=max(1, min(limit, 200)), offset=max(0, offset))

    @server.tool()
    def get_memory(memory_id: str):
        iid = authorize('read')
        return observed_service().get(iid, str(uuid.UUID(memory_id)))

    @server.tool()
    def ingest_memory(source: str, source_record_id: str, text: str):
        iid = authorize('write')
        if not 0 < len(text) <= 200000 or not 0 < len(source) <= 64 or not 0 < len(source_record_id) <= 512:
            raise ValueError('Source fields out of bounds')
        return observed_service().ingest(instance_id=iid, source=source, source_record_id=source_record_id, text=text)

    @server.tool()
    def add_memory(content: str, source_record_id: str):
        iid = authorize('write')
        if not 0 < len(content) <= 1000 or not 0 < len(source_record_id) <= 512:
            raise ValueError('Memory fields out of bounds')
        token = get_access_token()
        return observed_service().add_memory(iid, content, source_record_id, source='explicit:' + token.client_id[:50])

    application = server.streamable_http_app()
    metadata_url = None
    if settings.oauth_resource:
        metadata_url = settings.oauth_resource.rsplit('/mcp', 1)[0] + '/.well-known/oauth-protected-resource/mcp'
    application = RequireAuthMiddleware(application, ['read'], metadata_url)
    application = AuthContextMiddleware(application)
    application = AuthenticationMiddleware(application, backend=BearerAuthBackend(Verifier()))
    return server, application

def create_mcp(settings=None, *, http_client=None):
    settings = settings or Settings.from_env()
    if not settings.api_token:
        raise RuntimeError('Provide the client API key through AI_MEMORY_API_TOKEN_FILE or its environment variable')
    client = http_client or httpx.Client(base_url=os.getenv('AI_MEMORY_API_URL', 'http://ai-memory-api:8080'),
        headers={'Authorization': 'Bearer ' + settings.api_token}, timeout=60)
    server = FastMCP('AI Memory')
    def read(url, **params):
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    @server.tool()
    def search_memories(query: str, limit: int = 10):
        return read('/v1/memories/search', q=query, limit=max(1, min(limit, 100)))['items']
    @server.tool()
    def list_memories(limit: int = 50, offset: int = 0):
        return read('/v1/memories', limit=max(1, min(limit, 200)), offset=max(0, offset))['items']
    @server.tool()
    def get_memory(memory_id: str):
        return read('/v1/memories/' + str(uuid.UUID(memory_id)))

    if os.getenv('AI_MEMORY_MCP_ALLOW_WRITES', 'false').lower() == 'true':
        @server.tool()
        def ingest_memory(source: str, source_record_id: str, text: str):
            response = client.post('/v1/ingest', json={'source': source, 'source_record_id': source_record_id, 'text': text})
            response.raise_for_status()
            return response.json()
        @server.tool()
        def add_memory(content: str, source_record_id: str):
            response = client.post('/v1/memories', json={'content': content, 'source_record_id': source_record_id})
            response.raise_for_status()
            return response.json()
    return server

def run_stdio():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    create_mcp().run(transport='stdio')
