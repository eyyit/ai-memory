from sqlalchemy import select
from ai_memory.db import make_engine, make_session_factory, initialize_schema
from ai_memory.models import ExtractionJob
from ai_memory.service import MemoryService
from ai_memory.adapters import NoopIndex

class Extractor:
    def extract(self, text): return [{'content': 'Prefers hiking', 'evidence': text}]
class Embedder:
    def embed(self, text): return [1.0, 0.0]

def test_queued_extraction_and_evidence_survive_restart(tmp_path):
    url = f"sqlite:///{(tmp_path / 'canonical.db').as_posix()}"
    engine = make_engine(url)
    initialize_schema(engine)
    svc = MemoryService(make_session_factory(engine), Extractor(), Embedder(), NoopIndex())
    svc.ingest(instance_id='home', source='test', source_record_id='1', text='I prefer hiking')
    engine.dispose()
    engine = make_engine(url)
    initialize_schema(engine)
    svc = MemoryService(make_session_factory(engine), Extractor(), Embedder(), NoopIndex())
    try:
        assert svc.queue_status('home')['extraction_pending'] == 1
        assert svc.process_extractions(instance_id='home')['processed'] == 1
        mid = svc.list('home')[0]['id']
        assert svc.get('home', mid)['sources'][0]['source_text'] == 'I prefer hiking'
        assert svc.get('other', mid) is None
        assert svc.ingest(instance_id='home', source='test', source_record_id='1', text='I prefer hiking')['duplicate']
    finally: engine.dispose()

def test_unversioned_database_is_refused(tmp_path):
    import pytest
    from ai_memory.db import Base
    engine = make_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    Base.metadata.create_all(engine)
    with pytest.raises(RuntimeError, match='Unversioned'):
        initialize_schema(engine)
    engine.dispose()
