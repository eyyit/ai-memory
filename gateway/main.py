"""Single-user Google OAuth MCP gateway; backend stays private."""

import hashlib
import logging
import os
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import anyio
import httpx2
from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import RefreshToken, RegistrationError, TokenError
from mcp.server.auth.settings import RevocationOptions
from starlette.responses import JSONResponse
from starlette.routing import Route

SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email']
REDIRECTS = [
  'http://127.0.0.1/callback',
  'http://127.0.0.1:*/callback',
  'http://localhost:*/callback',
  'https://chatgpt.com/connector_platform_oauth_redirect',
  'https://chatgpt.com/connector/oauth/*',
]
PUBLIC_OAUTH_COLLECTIONS = frozenset({
  'mcp-oauth-proxy-clients',
  'mcp-oauth-transactions',
  'mcp-consent-csrf-tokens',
  'mcp-authorization-codes',
})
MAX_REGISTRATION_BODY_BYTES = 8192
MAX_CLIENT_NAME_CHARS = 256
MAX_REDIRECT_URIS = 10
MAX_URI_CHARS = 512
MAX_SCOPE_CHARS = 512
MAX_GRANT_TYPES = 4
MAX_CONTACTS = 5
MAX_CONTACT_CHARS = 256
MAX_SOFTWARE_FIELD_CHARS = 128
DEFAULT_PUBLIC_STORE_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_REGISTER_RATE_LIMIT_PER_IP = 10
DEFAULT_REGISTER_RATE_LIMIT_GLOBAL = 30
DEFAULT_REGISTER_RATE_WINDOW_SECONDS = 60
DEFAULT_REGISTER_QUOTA_MAX = 20
DEFAULT_REGISTER_QUOTA_WINDOW_SECONDS = 3600


class PartitionedKeyValue:
  """Route unauthenticated/ephemeral OAuth collections away from active grants."""

  def __init__(
    self,
    *,
    grant_store,
    public_store,
    public_collections=PUBLIC_OAUTH_COLLECTIONS,
    closables=(),
  ):
    self.grant_store = grant_store
    self.public_store = public_store
    self.public_collections = frozenset(public_collections)
    self._closables = tuple(closables)

  def _store_for(self, collection):
    if collection in self.public_collections:
      return self.public_store
    return self.grant_store

  async def get(self, key, *, collection=None):
    return await self._store_for(collection).get(key=key, collection=collection)

  async def get_many(self, keys, *, collection=None):
    return await self._store_for(collection).get_many(
      keys=keys, collection=collection
    )

  async def put(self, key, value, *, collection=None, ttl=None):
    return await self._store_for(collection).put(
      key=key, value=value, collection=collection, ttl=ttl
    )

  async def put_many(self, keys, values, *, collection=None, ttl=None):
    return await self._store_for(collection).put_many(
      keys=keys, values=values, collection=collection, ttl=ttl
    )

  async def delete(self, key, *, collection=None):
    return await self._store_for(collection).delete(
      key=key, collection=collection
    )

  async def delete_many(self, keys, *, collection=None):
    return await self._store_for(collection).delete_many(
      keys=keys, collection=collection
    )

  async def ttl(self, key, *, collection=None):
    return await self._store_for(collection).ttl(key=key, collection=collection)

  async def ttl_many(self, keys, *, collection=None):
    return await self._store_for(collection).ttl_many(
      keys=keys, collection=collection
    )

  async def close(self):
    for store in (self.grant_store, self.public_store):
      close = getattr(store, 'close', None)
      if callable(close):
        await close()
    for resource in self._closables:
      close = getattr(resource, 'close', None)
      if callable(close):
        close()


def make_oauth_storage(
  *,
  state_dir,
  fernet_key,
  public_state_dir=None,
  public_max_size=DEFAULT_PUBLIC_STORE_MAX_BYTES,
):
  """Create separate encrypted stores for active grants (no eviction) and public DCR/transactions."""
  state_path = Path(state_dir)
  state_path.mkdir(parents=True, exist_ok=True)
  if public_state_dir is None:
    public_path = state_path.with_name(state_path.name + '-public')
  else:
    public_path = Path(public_state_dir)
  public_path.mkdir(parents=True, exist_ok=True)
  fernet = Fernet(fernet_key)
  grant_disk = DiskStore(directory=str(state_path))
  grant_disk._cache.reset('eviction_policy', 'none')
  grant_store = FernetEncryptionWrapper(
    key_value=grant_disk,
    fernet=fernet,
  )
  public_store = FernetEncryptionWrapper(
    key_value=DiskStore(directory=str(public_path), max_size=public_max_size),
    fernet=fernet,
  )
  return PartitionedKeyValue(
    grant_store=grant_store,
    public_store=public_store,
  )


class _SlidingWindowLimiter:
  def __init__(self, *, max_requests, window_seconds):
    self.max_requests = max_requests
    self.window_seconds = window_seconds
    self._events = deque()

  def allow(self, now=None):
    now = time.monotonic() if now is None else now
    cutoff = now - self.window_seconds
    while self._events and self._events[0] <= cutoff:
      self._events.popleft()
    if len(self._events) >= self.max_requests:
      retry_after = max(1, int(self._events[0] + self.window_seconds - now) + 1)
      return False, retry_after
    self._events.append(now)
    return True, 0


def _validate_registration_metadata(client_info):
  client_name = getattr(client_info, 'client_name', None)
  if client_name is not None and len(str(client_name)) > MAX_CLIENT_NAME_CHARS:
    raise RegistrationError(
      'invalid_client_metadata',
      f'client_name exceeds maximum length of {MAX_CLIENT_NAME_CHARS}',
    )
  redirect_uris = getattr(client_info, 'redirect_uris', None) or []
  if len(redirect_uris) > MAX_REDIRECT_URIS:
    raise RegistrationError(
      'invalid_redirect_uri',
      f'At most {MAX_REDIRECT_URIS} redirect_uris are permitted',
    )
  for uri in redirect_uris:
    if len(str(uri)) > MAX_URI_CHARS:
      raise RegistrationError(
        'invalid_redirect_uri',
        f'redirect_uri exceeds maximum length of {MAX_URI_CHARS}',
      )
  scope = getattr(client_info, 'scope', None)
  if scope is not None and len(str(scope)) > MAX_SCOPE_CHARS:
    raise RegistrationError(
      'invalid_client_metadata',
      f'scope exceeds maximum length of {MAX_SCOPE_CHARS}',
    )
  grant_types = getattr(client_info, 'grant_types', None) or []
  if len(grant_types) > MAX_GRANT_TYPES:
    raise RegistrationError(
      'invalid_client_metadata',
      f'At most {MAX_GRANT_TYPES} grant_types are permitted',
    )
  contacts = getattr(client_info, 'contacts', None) or []
  if len(contacts) > MAX_CONTACTS or any(
    len(str(contact)) > MAX_CONTACT_CHARS for contact in contacts
  ):
    raise RegistrationError(
      'invalid_client_metadata',
      'contacts exceed allowed count or length',
    )
  for field in ('client_uri', 'logo_uri', 'tos_uri', 'policy_uri', 'jwks_uri'):
    value = getattr(client_info, field, None)
    if value is not None and len(str(value)) > MAX_URI_CHARS:
      raise RegistrationError(
        'invalid_client_metadata',
        f'{field} exceeds maximum length of {MAX_URI_CHARS}',
      )
  for field in ('software_id', 'software_version'):
    value = getattr(client_info, field, None)
    if value is not None and len(str(value)) > MAX_SOFTWARE_FIELD_CHARS:
      raise RegistrationError(
        'invalid_client_metadata',
        f'{field} exceeds maximum length of {MAX_SOFTWARE_FIELD_CHARS}',
      )


def secret(name):
  value = Path(os.environ[name + '_FILE']).read_text().strip()
  if not value:
    raise ValueError('Empty credential file: ' + name)
  return value


def permitted_user(claims, allowed_email):
  verified = claims.get('email_verified')
  return (
    bool(claims.get('sub'))
    and isinstance(claims.get('email'), str)
    and claims['email'].casefold() == allowed_email.casefold()
    and (
      verified is True
      or isinstance(verified, str)
      and verified.casefold() == 'true'
    )
  )


class SingleUserGoogle(GoogleProvider):
  def __init__(
    self,
    *,
    allowed_email_file,
    max_registration_body_bytes=MAX_REGISTRATION_BODY_BYTES,
    register_rate_limit_per_ip=DEFAULT_REGISTER_RATE_LIMIT_PER_IP,
    register_rate_limit_global=DEFAULT_REGISTER_RATE_LIMIT_GLOBAL,
    register_rate_window_seconds=DEFAULT_REGISTER_RATE_WINDOW_SECONDS,
    register_quota_max=DEFAULT_REGISTER_QUOTA_MAX,
    register_quota_window_seconds=DEFAULT_REGISTER_QUOTA_WINDOW_SECONDS,
    **kwargs,
  ):
    self.allowed_email_file = Path(allowed_email_file)
    self._max_registration_body_bytes = max_registration_body_bytes
    self._register_rate_limit_per_ip = register_rate_limit_per_ip
    self._register_rate_window_seconds = register_rate_window_seconds
    self._register_global_limiter = _SlidingWindowLimiter(
      max_requests=register_rate_limit_global,
      window_seconds=register_rate_window_seconds,
    )
    self._register_ip_limiters = {}
    self._register_quota_limiter = _SlidingWindowLimiter(
      max_requests=register_quota_max,
      window_seconds=register_quota_window_seconds,
    )
    self._refresh_locks = {}
    super().__init__(**kwargs)
    self.revocation_options = RevocationOptions(enabled=True)

  def _allow_register_request(self, client_ip):
    now = time.monotonic()
    ok, retry_after = self._register_global_limiter.allow(now)
    if not ok:
      return False, retry_after
    limiter = self._register_ip_limiters.get(client_ip)
    if limiter is None:
      if len(self._register_ip_limiters) > 1024:
        self._register_ip_limiters.clear()
      limiter = _SlidingWindowLimiter(
        max_requests=self._register_rate_limit_per_ip,
        window_seconds=self._register_rate_window_seconds,
      )
      self._register_ip_limiters[client_ip] = limiter
    return limiter.allow(now)

  async def register_client(self, client_info):
    _validate_registration_metadata(client_info)
    allowed, _ = self._register_quota_limiter.allow()
    if not allowed:
      raise RegistrationError(
        'invalid_client_metadata',
        'Client registration quota exceeded',
      )
    return await super().register_client(client_info)

  def get_routes(self, mcp_path=None):
    routes = super().get_routes(mcp_path)
    guarded = []
    for route in routes:
      if (
        isinstance(route, Route)
        and route.path == '/register'
        and route.methods is not None
        and 'POST' in route.methods
      ):
        class _GuardedRegisterASGI:
          def __init__(self, app, provider, max_bytes):
            self._app = app
            self._provider = provider
            self._max_bytes = max_bytes

          async def __call__(self, scope, receive, send):
            if (
              scope.get('type') == 'http'
              and scope.get('method', '').upper() == 'POST'
            ):
              headers = {
                k.lower(): v for k, v in scope.get('headers', [])
              }
              raw_len = headers.get(b'content-length')
              if raw_len is not None:
                try:
                  content_length = int(raw_len.decode('ascii'))
                except ValueError:
                  content_length = self._max_bytes + 1
                if content_length > self._max_bytes:
                  response = JSONResponse(
                    {
                      'error': 'invalid_client_metadata',
                      'error_description': 'Registration payload too large',
                    },
                    status_code=413,
                  )
                  await response(scope, receive, send)
                  return

              client_ip = None
              real_ip = headers.get(b'x-real-ip')
              if real_ip:
                client_ip = real_ip.decode('latin-1').strip()
              if not client_ip:
                client = scope.get('client')
                client_ip = client[0] if client else 'unknown'
              allowed, retry_after = self._provider._allow_register_request(
                client_ip
              )
              if not allowed:
                response = JSONResponse(
                  {
                    'error': 'temporarily_unavailable',
                    'error_description': 'Registration rate limit exceeded',
                  },
                  status_code=429,
                  headers={'Retry-After': str(retry_after)},
                )
                await response(scope, receive, send)
                return

              chunks = []
              total = 0
              while True:
                message = await receive()
                if message['type'] == 'http.request':
                  body = message.get('body', b'')
                  total += len(body)
                  if total > self._max_bytes:
                    response = JSONResponse(
                      {
                        'error': 'invalid_client_metadata',
                        'error_description': 'Registration payload too large',
                      },
                      status_code=413,
                    )
                    await response(scope, receive, send)
                    return
                  chunks.append(body)
                  if not message.get('more_body', False):
                    break
                elif message['type'] == 'http.disconnect':
                  break

              payload = b''.join(chunks)
              sent = False

              async def replay_receive():
                nonlocal sent
                if not sent:
                  sent = True
                  return {
                    'type': 'http.request',
                    'body': payload,
                    'more_body': False,
                  }
                return {'type': 'http.disconnect'}

              await self._app(scope, replay_receive, send)
              return

            await self._app(scope, receive, send)

        guarded.append(
          Route(
            path=route.path,
            endpoint=_GuardedRegisterASGI(
              route.app, self, self._max_registration_body_bytes
            ),
            methods=list(route.methods),
            name=route.name,
            include_in_schema=route.include_in_schema,
          )
        )
      elif (
        isinstance(route, Route)
        and route.path == '/revoke'
        and route.methods is not None
        and 'POST' in route.methods
      ):
        class _CompatRevokeASGI:
          """Allow RFC 7009 public-client revocation without client_secret."""

          def __init__(self, app, max_bytes):
            self._app = app
            self._max_bytes = max_bytes

          async def __call__(self, scope, receive, send):
            if (
              scope.get('type') == 'http'
              and scope.get('method', '').upper() == 'POST'
            ):
              headers = {
                k.lower(): v for k, v in scope.get('headers', [])
              }
              ctype = headers.get(b'content-type', b'').decode('latin-1').lower()
              if ctype.startswith('application/x-www-form-urlencoded'):
                chunks = []
                total = 0
                while True:
                  message = await receive()
                  if message['type'] == 'http.request':
                    body = message.get('body', b'')
                    total += len(body)
                    if total > self._max_bytes:
                      response = JSONResponse(
                        {
                          'error': 'invalid_request',
                          'error_description': 'Revocation payload too large',
                        },
                        status_code=413,
                      )
                      await response(scope, receive, send)
                      return
                    chunks.append(body)
                    if not message.get('more_body', False):
                      break
                  elif message['type'] == 'http.disconnect':
                    break
                payload = b''.join(chunks)
                pairs = parse_qsl(payload.decode('latin-1'), keep_blank_values=True)
                if not any(k == 'client_secret' for k, _ in pairs):
                  payload = (
                    payload + (b'&client_secret=' if payload else b'client_secret=')
                  )
                new_headers = [
                  (k, v)
                  for k, v in scope.get('headers', [])
                  if k.lower() != b'content-length'
                ]
                new_headers.append(
                  (b'content-length', str(len(payload)).encode('ascii'))
                )
                new_scope = dict(scope, headers=new_headers)
                sent = False

                async def replay_receive():
                  nonlocal sent
                  if not sent:
                    sent = True
                    return {
                      'type': 'http.request',
                      'body': payload,
                      'more_body': False,
                    }
                  return {'type': 'http.disconnect'}

                await self._app(new_scope, replay_receive, send)
                return

            await self._app(scope, receive, send)

        guarded.append(
          Route(
            path=route.path,
            endpoint=_CompatRevokeASGI(
              route.app, self._max_registration_body_bytes
            ),
            methods=list(route.methods),
            name=route.name,
            include_in_schema=route.include_in_schema,
          )
        )
      else:
        guarded.append(route)
    return guarded

  async def active_mapping(self, token, token_use='access'):
    # These private stores are covered by lifecycle tests against pinned
    # FastMCP.
    try:
      payload = self.jwt_issuer.verify_token(
        token, expected_token_use=token_use
      )
    except Exception:
      return None
    mapping = await self._jti_mapping_store.get(key=payload['jti'])
    if mapping is None:
      return None
    revoked = await self._client_storage.get(
      key=mapping.upstream_token_id, collection='home-memory-revoked-grants'
    )
    if (
      revoked is not None
      or await self._upstream_token_store.get(key=mapping.upstream_token_id)
      is None
    ):
      return None
    return mapping

  async def load_refresh_token(self, client, refresh_token):
    result = await super().load_refresh_token(client, refresh_token)
    if (
      result is None
      or await self.active_mapping(refresh_token, 'refresh') is None
    ):
      return None
    return result

  async def exchange_refresh_token(self, client, refresh_token, scopes):
    token_key = hashlib.sha256(refresh_token.token.encode()).hexdigest()
    entry = self._refresh_locks.get(token_key)
    if entry is None:
      entry = [anyio.Lock(), 0]
      self._refresh_locks[token_key] = entry
    entry[1] += 1
    try:
      async with entry[0]:
        if (
          await self.active_mapping(refresh_token.token, 'refresh') is None
          or await super().load_refresh_token(client, refresh_token.token)
          is None
        ):
          raise TokenError('invalid_grant', 'Session revoked or expired')
        result = await super().exchange_refresh_token(
          client, refresh_token, scopes
        )
        if await self.active_mapping(result.access_token) is None:
          raise TokenError('invalid_grant', 'Session revoked during refresh')
        return result
    finally:
      entry[1] -= 1
      if entry[1] == 0:
        self._refresh_locks.pop(token_key, None)

  async def revoke_token(self, token):
    token_use = 'refresh' if isinstance(token, RefreshToken) else 'access'
    mapping = await self.active_mapping(token.token, token_use)
    if mapping is not None:
      # Persistent tombstone also rejects a concurrent refresh that rewrites
      # the upstream record. Other independently authorized grants remain valid.
      await self._client_storage.put(
        key=mapping.upstream_token_id,
        collection='home-memory-revoked-grants',
        value={'revoked': True},
      )
    await super().revoke_token(token)
    if mapping is not None:
      await self._upstream_token_store.delete(key=mapping.upstream_token_id)
      await self._jti_mapping_store.delete(key=mapping.jti)

  async def load_access_token(self, token):
    if await self.active_mapping(token) is None:
      return None
    result = await super().load_access_token(token)
    allowed = self.allowed_email_file.read_text().strip()
    if (
      result is None
      or not allowed
      or not permitted_user(result.claims or {}, allowed)
      or await self.active_mapping(token) is None
    ):
      return None
    try:
      payload = self.jwt_issuer.verify_token(token, expected_token_use='access')
    except Exception:
      payload = None
    if payload is not None and payload.get('client_id'):
      result = result.model_copy(
        update={'token': token, 'client_id': str(payload['client_id'])}
      )
    return result


def make_server(provider, backend):
  server = FastMCP('Home AI Memory', auth=provider)

  async def call(method, path, *, body=None, params=None):
    if get_access_token() is None:
      raise PermissionError('Authentication required')
    response = await backend.request(method, path, json=body, params=params)
    response.raise_for_status()
    return response.json()

  @server.tool(
    annotations={'readOnlyHint': True, 'openWorldHint': False},
    meta={'securitySchemes': [{'type': 'oauth2', 'scopes': SCOPES}]},
  )
  async def search_memories(query: str, limit: int = 10):
    if not 0 < len(query) <= 2000:
      raise ValueError('Query length out of bounds')
    return (
      await call(
        'GET',
        '/v1/memories/search',
        params={'q': query, 'limit': max(1, min(limit, 100))},
      )
    )['items']

  @server.tool(
    annotations={'readOnlyHint': True, 'openWorldHint': False},
    meta={'securitySchemes': [{'type': 'oauth2', 'scopes': SCOPES}]},
  )
  async def get_usage_stats(hours: int = 24):
    return await call(
      'GET', '/v1/usage', params={'hours': max(1, min(hours, 720))}
    )

  @server.tool(
    annotations={'readOnlyHint': True, 'openWorldHint': False},
    meta={'securitySchemes': [{'type': 'oauth2', 'scopes': SCOPES}]},
  )
  async def list_memories(limit: int = 50, offset: int = 0):
    return (
      await call(
        'GET',
        '/v1/memories',
        params={'limit': max(1, min(limit, 200)), 'offset': max(0, offset)},
      )
    )['items']

  @server.tool(
    annotations={'readOnlyHint': True, 'openWorldHint': False},
    meta={'securitySchemes': [{'type': 'oauth2', 'scopes': SCOPES}]},
  )
  async def get_memory(
    memory_id: str, sources_limit: int = 20, sources_offset: int = 0
  ):
    return await call(
      'GET',
      '/v1/memories/' + str(uuid.UUID(memory_id)),
      params={
        'sources_limit': max(0, min(sources_limit, 100)),
        'sources_offset': max(0, sources_offset),
      },
    )

  @server.tool(
    annotations={
      'readOnlyHint': False,
      'destructiveHint': False,
      'openWorldHint': False,
    },
    meta={'securitySchemes': [{'type': 'oauth2', 'scopes': SCOPES}]},
  )
  async def ingest_memory(source: str, source_record_id: str, text: str):
    if (
      not 0 < len(text) <= 200000
      or not 0 < len(source) <= 64
      or not 0 < len(source_record_id) <= 512
    ):
      raise ValueError('Source fields out of bounds')
    return await call(
      'POST',
      '/v1/ingest',
      body={
        'source': source,
        'source_record_id': source_record_id,
        'text': text,
      },
    )

  @server.tool(
    annotations={
      'readOnlyHint': False,
      'destructiveHint': False,
      'openWorldHint': False,
    },
    meta={'securitySchemes': [{'type': 'oauth2', 'scopes': SCOPES}]},
  )
  async def add_memory(content: str, source_record_id: str):
    if not 0 < len(content) <= 1000 or not 0 < len(source_record_id) <= 512:
      raise ValueError('Memory fields out of bounds')
    return await call(
      'POST',
      '/v1/memories',
      body={'content': content, 'source_record_id': source_record_id},
    )

  return server


def validate_public_base(value):
  """Use one explicitly configured HTTPS origin, never a request header."""
  parts = urlsplit(value)
  if (
    parts.scheme != 'https'
    or not parts.hostname
    or parts.username
    or parts.password
    or parts.query
    or parts.fragment
    or parts.path not in {'', '/'}
  ):
    raise ValueError('PUBLIC_BASE_URL must be an HTTPS origin')
  parts.port  # Reject invalid port syntax before building an allowlist.
  return 'https://' + parts.netloc.lower()


def create_app():
  base = os.getenv('PUBLIC_BASE_URL', 'https://memory.example.com')
  base = validate_public_base(base)
  storage = make_oauth_storage(
    state_dir=os.getenv('OAUTH_STATE_DIR', '/state/oauth'),
    public_state_dir=os.getenv('OAUTH_PUBLIC_STATE_DIR'),
    fernet_key=secret('STORAGE_ENCRYPTION_KEY').encode(),
  )
  provider = SingleUserGoogle(
    allowed_email_file=os.environ['ALLOWED_EMAIL_FILE'],
    client_id=secret('GOOGLE_CLIENT_ID'),
    client_secret=secret('GOOGLE_CLIENT_SECRET'),
    base_url=base,
    redirect_path='/oauth2/callback',
    required_scopes=SCOPES,
    valid_scopes=SCOPES,
    jwt_signing_key=secret('JWT_SIGNING_KEY').encode(),
    client_storage=storage,
    allowed_client_redirect_uris=REDIRECTS,
    require_authorization_consent=True,
    cache_ttl_seconds=60,
    fastmcp_access_token_expiry_seconds=900,
    fallback_refresh_token_expiry_seconds=2592000,
    forward_resource=False,
    enable_cimd=False,
  )
  backend = httpx2.AsyncClient(
    base_url='http://ai-memory-api:8080',
    headers={'Authorization': 'Bearer ' + secret('BACKEND_KEY')},
    timeout=90,
  )
  server = make_server(provider, backend)
  logging.getLogger('httpx').setLevel(logging.WARNING)
  logging.getLogger('httpx2').setLevel(logging.WARNING)
  logging.getLogger('httpcore').setLevel(logging.WARNING)
  logging.getLogger('fastmcp.server.auth').setLevel(logging.WARNING)
  app = server.http_app(
    path='/mcp',
    stateless_http=True,
    json_response=True,
    allowed_hosts=[
      urlsplit(base).netloc,
      'ai-memory-oauth:*',
      'localhost:*',
      '127.0.0.1:*',
    ],
    allowed_origins=[base],
  )
  original = app.router.lifespan_context
  from contextlib import asynccontextmanager

  @asynccontextmanager
  async def lifespan(app):
    try:
      async with original(app):
        yield
    finally:
      await backend.aclose()
      await storage.close()

  app.router.lifespan_context = lifespan
  return app
