import hmac
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from .config import Settings
from .runtime import build_service
from .auth import Authenticator
from .usage import UsageService, report

class IngestRequest(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    source_record_id: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=200000)
    conversation_id: str | None = Field(default=None, max_length=512)
    timestamp: datetime | None = None
    metadata: dict = Field(default_factory=dict)

class AddRequest(BaseModel):
    content: str = Field(min_length=1, max_length=1000)
    source_record_id: str = Field(min_length=1, max_length=512)

class ReviseRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    type: str | None = Field(default=None, max_length=64)
    entities: list | None = None
    attributes: dict | None = None
    reason: str = Field(default='updated', max_length=128)

def create_app(service=None, settings=None):
    settings = settings or Settings.from_env()
    authenticator = Authenticator(settings)
    def get_service(principal=None):
        if app.state.memory_service is None: raise HTTPException(503, 'Service starting')
        service = app.state.memory_service
        if principal is None:
            return service
        return UsageService(service, principal.client_id, "rest")

    from .mcp_server import create_http_mcp
    mcp_server, mcp_app = create_http_mcp(get_service, authenticator, settings)

    @asynccontextmanager
    async def lifespan(app):
        engine = None
        if not settings.auth_file and not settings.api_token and not settings.oauth_issuer:
            raise RuntimeError('Configure a private authentication file before starting')
        if service is None:
            app.state.memory_service, engine = build_service(settings)
        # Processing runs in the dedicated worker; API remains available during inference outages.
        async with mcp_server.session_manager.run():
            yield
        if engine: engine.dispose()

    app = FastAPI(title='AI Memory', version='0.2.0', lifespan=lifespan)
    app.state.memory_service = service
    app.state.authenticator = authenticator

    def require(scope):
        def authorize(authorization: str | None = Header(default=None)):
            token = authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
            principal = authenticator.authenticate(token)
            if principal is None:
                challenge = 'Bearer'
                if settings.oauth_resource:
                    origin = settings.oauth_resource.rsplit('/mcp', 1)[0]
                    challenge += f' resource_metadata="{origin}/.well-known/oauth-protected-resource/mcp"'
                raise HTTPException(401, 'Invalid credential', headers={'WWW-Authenticate': challenge})
            if not principal.allows(scope): raise HTTPException(403, 'Insufficient permission')
            return principal
        return authorize

    @app.get('/health')
    def health():
        with get_service().sessions() as db: db.execute(text('SELECT 1'))
        return {'status': 'ok'}

    @app.get('/v1/me')
    def me(principal=Depends(require('read'))):
        return {'client_id': principal.client_id, 'instance_id': principal.instance_id, 'scopes': sorted(principal.scopes)}

    @app.get('/v1/memories')
    def list_memories(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), principal=Depends(require('read'))):
        return {'items': get_service(principal).list(principal.instance_id, limit=limit, offset=offset)}

    @app.get('/v1/memories/search')
    def search(q: str = Query(min_length=1, max_length=2000), limit: int = Query(10, ge=1, le=100), principal=Depends(require('read'))):
        return {'items': get_service(principal).search(principal.instance_id, q, limit)}

    @app.get('/v1/memories/{memory_id}')
    def get(memory_id: str, principal=Depends(require('read'))):
        row = get_service(principal).get(principal.instance_id, memory_id)
        if row is None: raise HTTPException(404, 'Memory not found')
        return row

    @app.post('/v1/ingest', status_code=202)
    def ingest(body: IngestRequest, principal=Depends(require('write'))):
        return get_service(principal).ingest(instance_id=principal.instance_id, **body.model_dump())

    @app.post('/v1/memories')
    def add(body: AddRequest, principal=Depends(require('write'))):
        try:
            return get_service(principal).add_memory(principal.instance_id, body.content, body.source_record_id,
                source='explicit:' + principal.client_id[:50])
        except ValueError: raise HTTPException(409, 'Idempotency key conflicts with existing content')

    @app.patch('/v1/memories/{memory_id}')
    def revise(memory_id: str, body: ReviseRequest, principal=Depends(require('write'))):
        values = body.model_dump(exclude_unset=True)
        try:
            row = get_service(principal).revise(principal.instance_id, memory_id, content=values.get('content'),
                memory_type=values.get('type'), entities=values.get('entities'), attributes=values.get('attributes'),
                reason=values.get('reason', 'updated'))
        except IntegrityError: raise HTTPException(409, 'Content already belongs to another memory')
        if row is None: raise HTTPException(404, 'Memory not found')
        return get_service(principal).get(principal.instance_id, memory_id)

    @app.delete('/v1/memories/{memory_id}')
    def delete(memory_id: str, principal=Depends(require('write'))):
        if not get_service(principal).delete(principal.instance_id, memory_id): raise HTTPException(404, 'Memory not found')
        return {'deleted': True}

    @app.get('/v1/usage')
    def usage(hours: int = Query(24, ge=1, le=720),
              principal=Depends(require('read'))):
        return report(get_service(), principal.instance_id, hours)

    @app.get('/v1/queue')
    def queue(principal=Depends(require('admin'))):
        return get_service().queue_status(principal.instance_id)

    @app.post('/v1/index/retry')
    def retry_index(principal=Depends(require('admin'))):
        return get_service().process_outbox(instance_id=principal.instance_id)

    @app.get('/.well-known/oauth-protected-resource/mcp')
    def metadata():
        value = authenticator.metadata()
        if value is None: raise HTTPException(404, 'OAuth not configured')
        return value

    app.mount('/', mcp_app)
    return app

app = create_app()
