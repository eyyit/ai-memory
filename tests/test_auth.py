import json
import time
from types import SimpleNamespace
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
from ai_memory.auth import Authenticator
from ai_memory.config import Settings

def test_oauth_checks_issuer_audience_expiry_subject_and_scope():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(oauth_issuer='https://identity.example', oauth_jwks_url='https://identity.example/jwks',
        oauth_resource='https://memory.example/mcp', oauth_subjects=('owner',))
    auth = Authenticator(settings)
    auth.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private.public_key()))
    claims = {'iss':settings.oauth_issuer, 'aud':settings.oauth_resource, 'sub':'owner',
              'exp':int(time.time())+60, 'scope':'read write admin'}
    valid = jwt.encode(claims,private,algorithm='RS256')
    principal = auth.authenticate(valid)
    assert principal.allows('read') and principal.allows('write') and not principal.allows('admin')
    for updates in [{'iss':'https://attacker.example'}, {'aud':'https://elsewhere.example'}, {'sub':'stranger'}, {'exp':0}]:
        assert auth.authenticate(jwt.encode({**claims, **updates},private,algorithm='RS256')) is None
    assert auth.authenticate(jwt.encode(claims,'fake-key' * 8,algorithm='HS256')) is None

def test_oauth_requires_complete_https_configuration_and_allowed_user():
    with pytest.raises(ValueError):
        Authenticator(Settings(oauth_issuer='https://identity.example'))
    with pytest.raises(ValueError):
        Authenticator(Settings(oauth_issuer='http://identity.example',oauth_jwks_url='http://identity.example/jwks',
            oauth_resource='https://memory.example/mcp',oauth_subjects=('owner',)))

def test_secret_file_configuration(tmp_path, monkeypatch):
    key = tmp_path / 'key'
    key.write_text('private-key\n')
    monkeypatch.setenv('AI_MEMORY_API_TOKEN_FILE',str(key))
    monkeypatch.setenv('AI_MEMORY_API_TOKEN','should-not-win')
    assert Settings.from_env().api_token == 'private-key'
