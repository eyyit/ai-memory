import anyio
import time
from unittest.mock import patch
from pathlib import Path
import pytest
import httpx2
from fastmcp.server.auth.providers.google import GoogleProvider, GoogleTokenVerifier
from fastmcp.server.auth.auth import AccessToken
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from cryptography.fernet import Fernet
from gateway.main import SingleUserGoogle, permitted_user, make_server, SCOPES, REDIRECTS

def provider(tmp_path):
    path=tmp_path/'allowed';path.write_text('owner@example.com')
    return SingleUserGoogle(allowed_email_file=path, client_id='client-id',client_secret='fake-secret',
        base_url='https://memory.example.com',redirect_path='/oauth2/callback',
        required_scopes=SCOPES,valid_scopes=SCOPES,client_storage=MemoryStore(),
        jwt_signing_key=b'01234567890123456789012345678901',allowed_client_redirect_uris=REDIRECTS,
        forward_resource=False,enable_cimd=False)

def access(email='owner@example.com', verified=True):
    return AccessToken(token='synthetic',client_id='synthetic-client',scopes=SCOPES,
        expires_at=int(time.time())+600,claims={'sub':'google-user-id','email':email,'email_verified':verified})

def test_only_exact_verified_account_and_reloaded_allowlist(tmp_path):
    async def run():
        p=provider(tmp_path)
        for email,verified,allowed in [('owner@example.com',True,True),('other@example.com',True,False),
            ('owner@example.com','false',False),('owner@example.com',False,False),('owner@example.com','true',True)]:
            with patch.object(SingleUserGoogle,'active_mapping',return_value=True), patch.object(GoogleProvider,'load_access_token',return_value=access(email,verified)):
                assert bool(await p.load_access_token('synthetic')) is allowed
        p.allowed_email_file.write_text('other@example.com')
        with patch.object(SingleUserGoogle,'active_mapping',return_value=True), patch.object(GoogleProvider,'load_access_token',return_value=access()):
            assert await p.load_access_token('synthetic') is None
    anyio.run(run)

def test_oauth_metadata_authentication_tools_and_no_admin(tmp_path):
    async def run():
        p=provider(tmp_path)
        calls=[]
        def backend(request):
            calls.append(request)
            assert request.headers['Authorization']=='Bearer scoped-writer-key'
            if request.url.path=='/v1/memories': return httpx2.Response(200,json={'id':'synthetic'} if request.method=='POST' else {'items':[]})
            if request.url.path=='/v1/usage':
                return httpx2.Response(200,json={'counts':[]})
            return httpx2.Response(202,json={'pending':True})
        client=httpx2.AsyncClient(base_url='http://ai-memory-api:8080',transport=httpx2.MockTransport(backend),headers={'Authorization':'Bearer scoped-writer-key'})
        app=make_server(p,client).http_app(path='/mcp',stateless_http=True,json_response=True,allowed_hosts=['memory.example.com'])
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url='https://memory.example.com') as http:
                metadata=(await http.get('/.well-known/oauth-authorization-server')).json()
                assert metadata['issuer']=='https://memory.example.com/'
                assert 'S256' in metadata['code_challenge_methods_supported']
                headers={'Accept':'application/json, text/event-stream'}
                body={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}
                assert (await http.post('/mcp',json=body,headers=headers)).status_code==401
                with patch.object(SingleUserGoogle,'active_mapping',return_value=True), patch.object(GoogleProvider,'load_access_token',return_value=access('other@example.com')):
                    assert (await http.post('/mcp',json=body,headers={**headers,'Authorization':'Bearer synthetic'})).status_code==401
                with patch.object(SingleUserGoogle,'active_mapping',return_value=True), patch.object(GoogleProvider,'load_access_token',return_value=access()):
                    response=await http.post('/mcp',json=body,headers={**headers,'Authorization':'Bearer synthetic'})
                    assert response.status_code==200,response.text
                    tools=response.json()['result']['tools']
                    assert {t['name'] for t in tools}=={'search_memories','list_memories','get_memory',
                        'ingest_memory','add_memory','get_usage_stats'}
                    assert next(t for t in tools if t['name']=='add_memory')['annotations']['readOnlyHint'] is False
                    write={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'add_memory','arguments':{'content':'Synthetic fact','source_record_id':'synthetic'}}}
                    response=await http.post('/mcp',json=write,headers={**headers,'Authorization':'Bearer synthetic'})
                    assert response.status_code==200 and not response.json()['result'].get('isError',False),response.text
                    assert len(calls)==1
                    stats = {
                        'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                        'params': {'name': 'get_usage_stats',
                                   'arguments': {'hours': 168}},
                    }
                    response = await http.post(
                        '/mcp', json=stats,
                        headers={**headers, 'Authorization': 'Bearer synthetic'})
                    assert not response.json()['result'].get('isError', False)
                    assert calls[-1].method == 'GET'
                    assert calls[-1].url.path == '/v1/usage'
                    assert calls[-1].url.params['hours'] == '168'
                    assert next(t for t in tools if t['name']=='get_usage_stats')[
                        'annotations']['readOnlyHint'] is True
        await client.aclose()
    anyio.run(run)

def test_invalid_proxy_token_is_rejected(tmp_path):
    async def run(): assert await provider(tmp_path).load_access_token('not-a-valid-signed-token') is None
    anyio.run(run)

def test_encrypted_disk_state_survives_restart(tmp_path):
    async def run():
        key=Fernet.generate_key()
        a=FernetEncryptionWrapper(key_value=DiskStore(directory=tmp_path/'state'),fernet=Fernet(key))
        await a.put(key='test',value={'token':'plaintext-secret-canary'})
        b=FernetEncryptionWrapper(key_value=DiskStore(directory=tmp_path/'state'),fernet=Fernet(key))
        assert await b.get(key='test')=={'token':'plaintext-secret-canary'}
        assert all(b'plaintext-secret-canary' not in f.read_bytes() for f in (tmp_path/'state').rglob('*') if f.is_file())
    anyio.run(run)

def test_google_token_verifier_rejects_wrong_client_and_expired_token():
    async def run():
        for wrong in [True,False]:
            def endpoint(request):
                if request.url.path.endswith('tokeninfo'):
                    return httpx2.Response(200,json={'aud':'other-client' if wrong else 'client-id','sub':'user','scope':' '.join(SCOPES),'expires_in':600,'email':'owner@example.com','email_verified':True})
                return httpx2.Response(200,json={'email':'owner@example.com','verified_email':True})
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(endpoint)) as client:
                p=GoogleTokenVerifier(audience='client-id',required_scopes=SCOPES,http_client=client)
                assert bool(await p.verify_token('synthetic')) is (not wrong)
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r:httpx2.Response(400,json={'error':'invalid_token'}))) as client:
            assert await GoogleTokenVerifier(audience='client-id',http_client=client).verify_token('expired') is None
    anyio.run(run)
