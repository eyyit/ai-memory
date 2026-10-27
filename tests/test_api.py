import hashlib
import json
from fastapi.testclient import TestClient
from ai_memory.api import create_app
from ai_memory.config import Settings

def auth_settings(tmp_path):
    path = tmp_path / 'auth.json'
    rows = [{'id': role, 'token_sha256': hashlib.sha256(('key-' + role).encode()).hexdigest(),
        'scopes': scopes, 'instance_id': 'home'}
        for role, scopes in [('reader', ['read']), ('writer', ['read', 'write']), ('admin', ['admin'])]]
    path.write_text(json.dumps({'clients': rows}))
    return Settings(auth_file=str(path))

def headers(role): return {'Authorization': 'Bearer key-' + role}

def test_server_enforces_read_write_admin_and_revocation(rig, tmp_path):
    svc, _, _, _, _ = rig
    settings = auth_settings(tmp_path)
    with TestClient(create_app(svc, settings)) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/v1/memories').status_code == 401
        assert client.get('/v1/memories', headers={'Authorization': 'key-reader'}).status_code == 401
        assert client.get('/v1/memories', headers=headers('reader')).status_code == 200
        payload = {'source': 'test', 'source_record_id': '1', 'text': 'I like tea'}
        assert client.post('/v1/ingest', headers=headers('reader'), json=payload).status_code == 403
        assert client.post('/v1/ingest', headers=headers('writer'), json=payload).status_code == 202
        mid = client.post('/v1/memories', headers=headers('writer'),
            json={'content': 'Likes tea', 'source_record_id': 'explicit-1'}).json()['id']
        assert client.patch('/v1/memories/' + mid, headers=headers('reader'), json={'content': 'Changed'}).status_code == 403
        assert client.delete('/v1/memories/' + mid, headers=headers('reader')).status_code == 403
        assert client.post('/v1/index/retry', headers=headers('writer')).status_code == 403
        assert client.get('/v1/queue', headers=headers('admin')).status_code == 200
        assert client.get('/v1/memories/' + mid, headers=headers('reader')).json()['sources']
        assert client.delete('/v1/memories/' + mid, headers=headers('writer')).status_code == 200
        path = tmp_path / 'auth.json'
        values = json.loads(path.read_text())
        values['clients'][0]['disabled'] = True
        path.write_text(json.dumps(values))
        assert client.get('/v1/memories', headers=headers('reader')).status_code == 401

def test_unknown_instance_key_and_legacy_token_fail_closed(rig, tmp_path):
    svc = rig[0]
    settings = auth_settings(tmp_path)
    path = tmp_path / 'auth.json'
    data = json.loads(path.read_text())
    data['clients'][0]['instance_id'] = 'other'
    path.write_text(json.dumps(data))
    with TestClient(create_app(svc, settings)) as client:
        assert client.get('/v1/memories', headers=headers('reader')).status_code == 401
    with TestClient(create_app(svc, Settings(api_token='legacy'))) as client:
        assert client.get('/v1/memories', headers={'Authorization': 'Bearer legacy'}).status_code == 200
        assert client.post('/v1/ingest', headers={'Authorization': 'Bearer legacy'}, json={}).status_code == 403
