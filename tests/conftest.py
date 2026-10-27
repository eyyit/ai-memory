import pytest

from ai_memory.db import Base, make_engine, make_session_factory
from ai_memory.service import MemoryService


class FakeExtractor:
    def __init__(self): self.inputs = []; self.results = []
    def extract(self, text):
        self.inputs.append(text)
        return list(self.results)


class FakeEmbedder:
    def __init__(self): self.inputs = []
    def embed(self, text): self.inputs.append(text); return [float(len(text) or 1), 1.0]


class FakeIndex:
    def __init__(self): self.rows = {}; self.fail = False
    def upsert(self, instance_id, memory_id, vector, payload):
        if self.fail: raise RuntimeError("temporary index outage")
        self.rows[(instance_id, memory_id)] = (vector, payload)
    def delete(self, instance_id, memory_id): self.rows.pop((instance_id, memory_id), None)
    def search(self, instance_id, vector, limit): return []


@pytest.fixture
def rig():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    extractor, embedder, index = FakeExtractor(), FakeEmbedder(), FakeIndex()
    svc = MemoryService(sessions, extractor, embedder, index)
    yield svc, sessions, extractor, embedder, index
    engine.dispose()
