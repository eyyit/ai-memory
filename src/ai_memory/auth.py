"""Per-client key authorization and optional external OAuth resource-server validation."""
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlparse

@dataclass(frozen=True)
class Principal:
    client_id: str
    instance_id: str
    scopes: frozenset
    def allows(self, scope):
        return scope in self.scopes or 'admin' in self.scopes

class Authenticator:
    def __init__(self, settings):
        self.settings = settings
        self.jwks = None
        configured = [settings.oauth_issuer, settings.oauth_jwks_url, settings.oauth_resource]
        if any(configured):
            if not all(configured) or not settings.oauth_subjects:
                raise ValueError('OAuth requires issuer, JWKS URL, resource URL and allowed user subjects')
            if any(urlparse(url).scheme != 'https' for url in configured):
                raise ValueError('OAuth endpoints must use HTTPS')
            import jwt
            self.jwks = jwt.PyJWKClient(settings.oauth_jwks_url, timeout=5)

    def authenticate(self, token):
        if not token or len(token) > 16384:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        if self.settings.auth_file:
            try:
                clients = json.loads(Path(self.settings.auth_file).read_text())['clients']
                for row in clients:
                    if row.get('disabled', False):
                        continue
                    if hmac.compare_digest(digest, row.get('token_sha256', '')):
                        scopes = frozenset(row['scopes'])
                        if not scopes <= {'read', 'write', 'admin'} or row['instance_id'] != self.settings.instance_id:
                            return None
                        return Principal(row['id'], row['instance_id'], scopes)
            except (OSError, ValueError, KeyError, TypeError):
                return None
        elif self.settings.api_token and hmac.compare_digest(token.encode(), self.settings.api_token.encode()):
            return Principal('local', self.settings.instance_id, frozenset(self.settings.api_token_scopes))
        if self.jwks:
            import jwt
            try:
                key = self.jwks.get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=['RS256', 'ES256'],
                    issuer=self.settings.oauth_issuer, audience=self.settings.oauth_resource,
                    options={'require': ['exp', 'iss', 'aud', 'sub']})
                if claims['sub'] not in self.settings.oauth_subjects:
                    return None
                scopes = frozenset(str(claims.get('scope', '')).split()) & {'read', 'write'}
                return Principal('oauth:' + claims['sub'], self.settings.instance_id, scopes)
            except Exception:
                return None
        return None

    def metadata(self):
        if not self.settings.oauth_resource:
            return None
        return {'resource': self.settings.oauth_resource,
                'authorization_servers': [self.settings.oauth_issuer],
                'scopes_supported': ['read', 'write'], 'bearer_methods_supported': ['header']}
