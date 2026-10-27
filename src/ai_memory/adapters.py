import math
from typing import Protocol
import httpx
from .inference import EMBEDDING_REVISION, validate_facts

class Extractor(Protocol):
    def extract(self, text: str) -> list[dict]: ...
class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...
class VectorIndex(Protocol):
    def upsert(self, instance_id, memory_id, vector, payload): ...
    def delete(self, instance_id, memory_id): ...
    def search(self, instance_id, vector, limit): ...

class HTTPExtractor:
    def __init__(self, url, token, client=None):
        self.client = client or httpx.Client(base_url=url, headers={'Authorization': 'Bearer ' + token}, timeout=300)
    def extract(self, text):
        response = self.client.post('/v1/extract', json={'text': text})
        response.raise_for_status()
        return validate_facts(response.json()['memories'], text)

class HTTPEmbedder:
    def __init__(self, url, token, client=None):
        self.client = client or httpx.Client(base_url=url, headers={'Authorization': 'Bearer ' + token}, timeout=30)
    def embed(self, text):
        response = self.client.post('/v1/embed', json={'text': text})
        response.raise_for_status()
        data = response.json()
        vector = data['vector']
        if data.get('model_revision') != EMBEDDING_REVISION or len(vector) != 384 or any(not math.isfinite(x) for x in vector):
            raise ValueError('Embedding revision/dimension mismatch')
        return vector

class NoopIndex:
    def upsert(self, *args): pass
    def delete(self, *args): pass
    def search(self, *args): return []
    def clear_instance(self, *args): pass

class QdrantIndex:
    def __init__(self, url: str, collection: str, api_key: str | None = None):
        from qdrant_client import QdrantClient
        self.client = QdrantClient(url=url, api_key=api_key)
        self.collection = collection

    def _ensure(self, size: int):
        from qdrant_client.http import models
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(self.collection, vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE))
            self.client.create_payload_index(self.collection, "instance_id", field_schema=models.PayloadSchemaType.KEYWORD)

    @staticmethod
    def _point_id(instance_id: str, memory_id: str) -> str:
        return __import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, f"ai-memory:{instance_id}:{memory_id}").hex

    def upsert(self, instance_id, memory_id, vector, payload):
        from qdrant_client.http import models
        self._ensure(len(vector))
        self.client.upsert(self.collection, [models.PointStruct(id=self._point_id(instance_id, memory_id), vector=vector, payload={**payload, "instance_id": instance_id, "memory_id": memory_id})], wait=True)

    def delete(self, instance_id, memory_id):
        from qdrant_client.http import models
        if not self.client.collection_exists(self.collection):
            return
        self.client.delete(self.collection, models.PointIdsList(points=[self._point_id(instance_id, memory_id)]), wait=True)

    def clear_instance(self, instance_id):
        from qdrant_client.http import models
        if self.client.collection_exists(self.collection):
            selector = models.FilterSelector(filter=models.Filter(must=[models.FieldCondition(
                key="instance_id", match=models.MatchValue(value=instance_id))]))
            self.client.delete(self.collection, selector, wait=True)

    def search(self, instance_id, vector, limit):
        from qdrant_client.http import models
        if not self.client.collection_exists(self.collection):
            return []
        response = self.client.query_points(self.collection, query=vector,
            query_filter=models.Filter(must=[models.FieldCondition(key="instance_id", match=models.MatchValue(value=instance_id))]),
            limit=limit, with_payload=True)
        hits = response.points
        return [(str(hit.payload["memory_id"]), float(hit.score)) for hit in hits if hit.payload and "memory_id" in hit.payload]
