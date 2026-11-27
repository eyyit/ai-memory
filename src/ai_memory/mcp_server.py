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

MCP_INSTRUCTIONS = (
    "Personal cross-platform AI memory service. "
    "Call get_context_brief at session start to load pinned preferences, standing rules, and recent project updates. "
    "Call search_memories before acting on project, homelab, or preference topics. "
    "Use add_memory to save concise distilled facts, decisions, or preferences (<=1000 chars) with a unique descriptive source_record_id. "
    "Use revise_memory to update an existing memory instead of creating conflicting duplicates. "
    "Use ingest_memory only for raw source text that requires asynchronous model extraction. "
    "Never store raw secrets, passwords, tokens, or private keys."
)


def _merge_attributes(existing_attrs, tags, pinned):
    if tags is None and pinned is None:
        return None
    merged = dict(existing_attrs or {})
    if tags is not None:
        merged['tags'] = [str(t).strip()[:64] for t in tags if str(t).strip()][:20]
    if pinned is not None:
        if pinned:
            merged['pinned'] = True
        else:
            merged.pop('pinned', None)
    return merged


def create_http_mcp(service_getter, authenticator, settings):
    class Verifier:
        async def verify_token(self, token):
            principal = await anyio.to_thread.run_sync(authenticator.authenticate, token)
            if principal is None: return None
            scopes = set(principal.scopes)
            if 'admin' in scopes: scopes.update(['read', 'write'])
            return AccessToken(token=token, client_id=principal.client_id, scopes=sorted(scopes),
                resource=settings.oauth_resource or None, claims={'instance_id': principal.instance_id})

    server = FastMCP(
        'AI Memory',
        instructions=MCP_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(settings.mcp_hosts),
            allowed_origins=list(settings.mcp_origins),
        ),
    )

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
        """Return aggregate hourly memory usage counters (no query text or content) for the last 1-720 hours."""
        iid = authorize('read')
        return report(service_getter(), iid, hours)

    @server.tool()
    def get_context_brief(pinned_limit: int = 20, recent_limit: int = 15):
        """Load a one-call session bootstrap: pinned memories, standing preferences/rules, and most recently updated memories."""
        iid = authorize('read')
        return observed_service().context_brief(
            iid,
            pinned_limit=max(1, min(pinned_limit, 50)),
            recent_limit=max(1, min(recent_limit, 50)),
        )

    @server.tool()
    def search_memories(query: str, limit: int = 10, memory_type: str | None = None):
        """Hybrid lexical + semantic search over active memories with mild recency weighting. Optionally filter by memory_type (e.g. fact, preference, rule, project, decision)."""
        iid = authorize('read')
        if not query or len(query) > 2000: raise ValueError('Query length out of bounds')
        return observed_service().search(iid, query, max(1, min(limit, 100)), memory_type=memory_type)

    @server.tool()
    def list_memories(limit: int = 50, offset: int = 0):
        """List active memories ordered by updated_at descending."""
        return observed_service().list(authorize('read'), limit=max(1, min(limit, 200)), offset=max(0, offset))

    @server.tool()
    def get_memory(memory_id: str, sources_limit: int = 20, sources_offset: int = 0):
        """Fetch memory details, metadata, and bounded paginated source provenance for a memory UUID."""
        iid = authorize('read')
        return observed_service().get(
            iid,
            str(uuid.UUID(memory_id)),
            sources_limit=max(0, min(sources_limit, 100)),
            sources_offset=max(0, sources_offset),
        )

    @server.tool()
    def ingest_memory(source: str, source_record_id: str, text: str):
        """Submit raw source text for asynchronous background fact extraction. Prefer add_memory for already-distilled facts."""
        iid = authorize('write')
        if not 0 < len(text) <= 200000 or not 0 < len(source) <= 64 or not 0 < len(source_record_id) <= 512:
            raise ValueError('Source fields out of bounds')
        return observed_service().ingest(instance_id=iid, source=source, source_record_id=source_record_id, text=text)

    @server.tool()
    def add_memory(
        content: str,
        source_record_id: str,
        memory_type: str = 'fact',
        entities: list[str] | None = None,
        tags: list[str] | None = None,
        pinned: bool = False,
    ):
        """Store a concise distilled durable fact immediately without model extraction. Use a unique descriptive source_record_id (idempotency key), optional memory_type (fact, preference, rule, project, decision), entities, tags, and pinned=True for core cross-session context."""
        iid = authorize('write')
        if not 0 < len(content) <= 1000 or not 0 < len(source_record_id) <= 512:
            raise ValueError('Memory fields out of bounds')
        token = get_access_token()
        return observed_service().add_memory(
            iid, content, source_record_id,
            source='explicit:' + token.client_id[:50],
            memory_type=memory_type, entities=entities, tags=tags, pinned=pinned,
        )

    @server.tool()
    def revise_memory(
        memory_id: str,
        content: str | None = None,
        memory_type: str | None = None,
        entities: list[str] | None = None,
        tags: list[str] | None = None,
        pinned: bool | None = None,
        reason: str = 'updated',
    ):
        """Update an existing active memory while preserving full revision history. Prefer this over add_memory when a fact or project status changes."""
        iid = authorize('write')
        mid = str(uuid.UUID(memory_id))
        if content is not None and not 0 < len(content) <= 1000:
            raise ValueError('Memory content out of bounds')
        existing = service_getter().get(iid, mid)
        if existing is None:
            raise ValueError('Memory not found')
        attrs = _merge_attributes(existing.get('attributes'), tags, pinned)
        row = observed_service().revise(
            iid, mid,
            content=content,
            memory_type=memory_type,
            entities=entities,
            attributes=attrs,
            reason=reason or 'updated',
        )
        if row is None:
            raise ValueError('Memory not found')
        return service_getter().get(iid, mid)

    @server.tool()
    def delete_memory(memory_id: str):
        """Soft-delete an active memory by UUID and queue vector index removal. Use only when a memory is wrong or obsolete."""
        iid = authorize('write')
        mid = str(uuid.UUID(memory_id))
        return {'deleted': bool(observed_service().delete(iid, mid))}

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
    server = FastMCP('AI Memory', instructions=MCP_INSTRUCTIONS)

    def read(url, **params):
        clean_params = {k: v for k, v in params.items() if v is not None}
        response = client.get(url, params=clean_params)
        response.raise_for_status()
        return response.json()

    @server.tool()
    def get_context_brief(pinned_limit: int = 20, recent_limit: int = 15):
        """Load a one-call session bootstrap: pinned memories, standing preferences/rules, and most recently updated memories."""
        return read('/v1/context', pinned_limit=max(1, min(pinned_limit, 50)), recent_limit=max(1, min(recent_limit, 50)))

    @server.tool()
    def search_memories(query: str, limit: int = 10, memory_type: str | None = None):
        """Hybrid lexical + semantic search over active memories with mild recency weighting. Optionally filter by memory_type (e.g. fact, preference, rule, project, decision)."""
        return read('/v1/memories/search', q=query, limit=max(1, min(limit, 100)), memory_type=memory_type)['items']

    @server.tool()
    def list_memories(limit: int = 50, offset: int = 0):
        """List active memories ordered by updated_at descending."""
        return read('/v1/memories', limit=max(1, min(limit, 200)), offset=max(0, offset))['items']

    @server.tool()
    def get_memory(memory_id: str, sources_limit: int = 20, sources_offset: int = 0):
        """Fetch memory details, metadata, and bounded paginated source provenance for a memory UUID."""
        return read(
            '/v1/memories/' + str(uuid.UUID(memory_id)),
            sources_limit=max(0, min(sources_limit, 100)),
            sources_offset=max(0, sources_offset),
        )

    if os.getenv('AI_MEMORY_MCP_ALLOW_WRITES', 'false').lower() == 'true':
        @server.tool()
        def ingest_memory(source: str, source_record_id: str, text: str):
            """Submit raw source text for asynchronous background fact extraction. Prefer add_memory for already-distilled facts."""
            response = client.post('/v1/ingest', json={'source': source, 'source_record_id': source_record_id, 'text': text})
            response.raise_for_status()
            return response.json()

        @server.tool()
        def add_memory(
            content: str,
            source_record_id: str,
            memory_type: str = 'fact',
            entities: list[str] | None = None,
            tags: list[str] | None = None,
            pinned: bool = False,
        ):
            """Store a concise distilled durable fact immediately without model extraction. Use a unique descriptive source_record_id (idempotency key), optional memory_type (fact, preference, rule, project, decision), entities, tags, and pinned=True for core cross-session context."""
            payload = {
                'content': content,
                'source_record_id': source_record_id,
                'type': memory_type,
                'pinned': pinned,
            }
            if entities is not None:
                payload['entities'] = entities
            if tags is not None:
                payload['tags'] = tags
            response = client.post('/v1/memories', json=payload)
            response.raise_for_status()
            return response.json()

        @server.tool()
        def revise_memory(
            memory_id: str,
            content: str | None = None,
            memory_type: str | None = None,
            entities: list[str] | None = None,
            tags: list[str] | None = None,
            pinned: bool | None = None,
            reason: str = 'updated',
        ):
            """Update an existing active memory while preserving full revision history. Prefer this over add_memory when a fact or project status changes."""
            mid = str(uuid.UUID(memory_id))
            payload = {'reason': reason or 'updated'}
            if content is not None:
                payload['content'] = content
            if memory_type is not None:
                payload['type'] = memory_type
            if entities is not None:
                payload['entities'] = entities
            if tags is not None or pinned is not None:
                existing = read('/v1/memories/' + mid)
                payload['attributes'] = _merge_attributes(existing.get('attributes'), tags, pinned)
            response = client.patch('/v1/memories/' + mid, json=payload)
            response.raise_for_status()
            return response.json()

        @server.tool()
        def delete_memory(memory_id: str):
            """Soft-delete an active memory by UUID and queue vector index removal. Use only when a memory is wrong or obsolete."""
            mid = str(uuid.UUID(memory_id))
            response = client.delete('/v1/memories/' + mid)
            response.raise_for_status()
            return response.json()

    return server


def run_stdio():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    create_mcp().run(transport='stdio')
