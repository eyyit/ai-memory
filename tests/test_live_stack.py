"""Explicit opt-in against a separately approved running deployment; writes synthetic evidence."""
import os
import time
import uuid
import httpx
import pytest

@pytest.mark.skipif(os.getenv('AI_MEMORY_LIVE_TEST') != '1', reason='Deployment and model execution require next batch approval')
def test_live_automatic_extraction_provenance_search():
    marker = 'synthetic-' + str(uuid.uuid4())
    headers = {'Authorization': 'Bearer ' + os.environ['AI_MEMORY_API_TOKEN']}
    with httpx.Client(base_url=os.environ['AI_MEMORY_API_URL'], headers=headers, timeout=30) as client:
        assert client.get('/v1/memories', headers={'Authorization': ''}).status_code == 401
        payload = {'source':'live-test','source_record_id':marker,
            'text':f'My ongoing project is named {marker}. I prefer Python for this project.'}
        response = client.post('/v1/ingest',json=payload)
        assert response.status_code == 202 and response.json()['pending']
        assert client.post('/v1/ingest',json=payload).json()['duplicate']
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            items = client.get('/v1/memories/search',params={'q':marker}).json()['items']
            matches = [item for item in items if marker in item['content']]
            if matches: break
            time.sleep(3)
        else: pytest.fail('Automatic extraction failed to produce the synthetic project fact within ten minutes')
        detail = client.get('/v1/memories/'+matches[0]['id']).json()
        assert any(source['source']=='live-test' and marker in source['source_text'] for source in detail['sources'])
        # Synthetic evidence intentionally remains so restart and restore drills can verify it.
