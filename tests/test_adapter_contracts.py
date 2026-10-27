"""Exercise real client serialization/local Qdrant, not only fake service adapters."""
import uuid

from qdrant_client import QdrantClient

from ai_memory.adapters import QdrantIndex


def test_real_qdrant_index_isolation_and_delete():
    index = object.__new__(QdrantIndex)
    index.client = QdrantClient(location=":memory:")
    index.collection = "contract"
    home, work = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        index.upsert("home", home, [1.0, 0.0], {"content": "home evidence"})
        index.upsert("work", work, [1.0, 0.0], {"content": "work evidence"})
        index.upsert("home", home, [0.9, 0.1], {"content": "revised evidence"})
        assert [mid for mid, _ in index.search("home", [1.0, 0.0], 10)] == [home]
        assert [mid for mid, _ in index.search("work", [1.0, 0.0], 10)] == [work]
        index.delete("home", home)
        assert index.search("home", [1.0, 0.0], 10) == []
        assert len(index.search("work", [1.0, 0.0], 10)) == 1
    finally:
        index.client.close()


import httpx
import pytest
from fastapi.testclient import TestClient
from ai_memory.adapters import HTTPEmbedder, HTTPExtractor
from ai_memory.inference import create_app, validate_facts, EMBEDDING_REVISION

def test_inference_http_contracts():
    def respond(request):
        if request.url.path == '/v1/embed':
            return httpx.Response(200, json={'vector': [1.0] * 384, 'model_revision': EMBEDDING_REVISION})
        return httpx.Response(200, json={'memories': [{'content': 'Likes tea', 'evidence': 'I like tea'}]})
    with httpx.Client(base_url='http://inference.test', transport=httpx.MockTransport(respond)) as client:
        assert len(HTTPEmbedder('', '', client).embed('tea')) == 384
        assert HTTPExtractor('', '', client).extract('I like tea')[0]['content'] == 'Likes tea'

def test_rejects_model_space_mismatch_and_invented_evidence():
    with httpx.Client(base_url='http://inference.test', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'vector': [1] * 384, 'model_revision': 'wrong'}))) as client:
        with pytest.raises(ValueError): HTTPEmbedder('', '', client).embed('tea')
    with pytest.raises(ValueError): validate_facts([{'content': 'Likes coffee', 'evidence': 'coffee'}], 'I like tea')
    with pytest.raises(ValueError): validate_facts([{'content': 'tea', 'evidence': 'tea', 'confidence': float('nan')}], 'tea')

def test_private_inference_auth_and_redaction():
    class Models:
        def embed(self, text):
            assert 'private-value' not in text
            return [1.0] * 384
        def extract(self, text):
            assert 'private-value' not in text
            return []
    with TestClient(create_app(Models(), token='private-token')) as client:
        assert client.post('/v1/embed', json={'text': 'tea'}).status_code == 401
        assert client.post('/v1/embed', headers={'Authorization': 'Bearer private-token'},
            json={'text': 'api_key=private-value tea'}).status_code == 200
        assert client.post('/v1/extract', headers={'Authorization': 'Bearer private-token'},
            json={'text': 'a'*2001}).status_code == 413
