from datetime import timedelta
from sqlalchemy import select, func
from ai_memory.models import Memory, MemoryRevision, MemoryEvent, SourceRecord, SourceRevision, ExtractionJob, OutboxItem, now_utc
from ai_memory.service import chunks

def add(svc, iid='home', rid='1', text='I prefer hiking'):
    return svc.ingest(instance_id=iid, source='test', source_record_id=rid, text=text)

def test_ingest_commits_redacted_evidence_without_calling_model(rig):
    svc, sessions, extractor, _, _ = rig
    result = add(svc, text='I prefer hiking. password=private-value')
    assert result['pending'] and result['memories_created'] == 0 and not extractor.inputs
    with sessions() as db:
        assert 'private-value' not in db.scalar(select(SourceRevision)).redacted_text
        assert db.scalar(select(ExtractionJob)).completed_at is None

def test_automatic_extraction_idempotence_provenance_and_index(rig):
    svc, sessions, extractor, _, index = rig
    extractor.results = [{'content': 'Prefers hiking', 'confidence': 0.8}]
    add(svc)
    assert svc.process_extractions(instance_id='home')['memories_created'] == 1
    assert add(svc)['duplicate'] and not add(svc)['pending']
    add(svc, rid='2')
    svc.process_extractions(instance_id='home')
    mid = svc.list('home')[0]['id']
    detail = svc.get('home', mid)
    assert len(detail['sources']) == 2 and detail['sources'][0]['source_text'] == 'I prefer hiking'
    assert svc.queue_status('home') == {'extraction_pending': 0, 'index_pending': 1}
    assert svc.process_outbox(instance_id='home')['processed'] == 1
    assert ('home', mid) in index.rows

def test_failure_retry_survives_without_reimport(rig):
    svc, sessions, extractor, _, _ = rig
    original = extractor.extract
    extractor.extract = lambda text: (_ for _ in ()).throw(RuntimeError('private-error-text'))
    add(svc)
    assert svc.process_extractions(instance_id='home')['failed'] == 1
    with sessions.begin() as db:
        job = db.scalar(select(ExtractionJob))
        assert job.attempts == 1 and job.lease_token is None and 'private-error-text' not in job.last_error
        job.available_at = now_utc() - timedelta(seconds=1)
    extractor.extract = original
    extractor.results = [{'content': 'Prefers hiking'}]
    assert svc.process_extractions(instance_id='home')['processed'] == 1
    assert svc.list('home')

def test_old_worker_cannot_commit_after_lease_reassignment(rig):
    svc, sessions, extractor, _, _ = rig
    add(svc)
    def replaced(text):
        with sessions.begin() as db:
            row = db.scalar(select(ExtractionJob))
            row.lease_token = 'replacement-lease'
        return [{'content': 'Stale fact'}]
    extractor.extract = replaced
    assert svc.process_extractions(instance_id='home')['processed'] == 0
    assert not svc.list('home')

def test_expired_extraction_lease_is_reclaimed(rig):
    svc, sessions, extractor, _, _ = rig
    add(svc)
    with sessions.begin() as db:
        job = db.scalar(select(ExtractionJob))
        job.locked_at = now_utc() - timedelta(minutes=16)
        job.lease_token = 'crashed'
    extractor.results = [{'content': 'Recovered fact'}]
    assert svc.process_extractions(instance_id='home')['processed'] == 1

def test_source_versions_remain_exact(rig):
    svc, _, extractor, _, _ = rig
    extractor.results = [{'content': 'Likes hiking'}]
    add(svc)
    svc.process_extractions(instance_id='home')
    mid = svc.list('home')[0]['id']
    extractor.results = [{'content': 'Likes cycling'}]
    add(svc, text='I now like cycling')
    svc.process_extractions(instance_id='home')
    assert svc.get('home', mid)['sources'][0]['source_text'] == 'I prefer hiking'
    new = next(x for x in svc.list('home') if x['content'] == 'Likes cycling')
    assert svc.get('home', new['id'])['sources'][0]['source_revision'] == 2

def test_nested_redaction_and_revision_history(rig):
    svc, sessions, extractor, _, _ = rig
    extractor.results = [{'content': 'Uses a private tool', 'entities': ['token=hidden'], 'attributes': {'password': 'hidden'}}]
    svc.ingest(instance_id='home', source='test', source_record_id='api_key=hidden', text='fact',
        metadata={'nested': ['Bearer abcdefghijklmnop']})
    svc.process_extractions(instance_id='home')
    mid = svc.list('home')[0]['id']
    svc.revise('home', mid, content='Uses another tool', reason='password=hidden')
    with sessions() as db:
        assert 'hidden' not in str(db.scalar(select(Memory)).entities)
        assert 'hidden' not in str(db.scalar(select(Memory)).attributes)
        rows = db.scalars(select(MemoryRevision).order_by(MemoryRevision.revision)).all()
        assert [row.revision for row in rows] == [1, 2]
        assert 'hidden' not in rows[-1].change_reason

def test_explicit_fact_needs_no_model_and_preserves_idempotency(rig):
    svc, _, extractor, _, _ = rig
    result = svc.add_memory('home', 'Prefers tea', 'explicit-1')
    assert not extractor.inputs and svc.get('home', result['id'])
    assert svc.add_memory('home', 'Prefers tea', 'explicit-1')['duplicate']
    import pytest
    with pytest.raises(ValueError):
        svc.add_memory('home', 'Different fact', 'explicit-1')

def test_isolation_and_soft_delete(rig):
    svc, _, _, _, index = rig
    home = svc.add_memory('home', 'Likes hiking', '1')['id']
    svc.add_memory('other', 'Likes cycling', '1')
    assert svc.get('other', home) is None
    svc.process_outbox(instance_id='home')
    assert svc.delete('home', home) and svc.get('home', home) is None
    svc.process_outbox(instance_id='home')
    assert ('home', home) not in index.rows
    assert svc.list('other')

def test_lexical_search_remains_when_models_unavailable(rig):
    svc, _, _, embedder, _ = rig
    svc.add_memory('home', 'Enjoys long distance cycling', '1')
    embedder.embed = lambda text: (_ for _ in ()).throw(RuntimeError('offline'))
    assert svc.search('home', 'cycling')
    assert not svc.search('home', 'unrelated')

def test_index_retries_preserve_canonical_records(rig):
    svc, sessions, _, _, index = rig
    mid = svc.add_memory('home', 'Likes tea', '1')['id']
    index.fail = True
    assert svc.process_outbox(instance_id='home')['failed'] == 1
    with sessions.begin() as db:
        job = db.scalar(select(OutboxItem))
        assert job.attempts == 1 and job.completed_at is None
        job.available_at = now_utc() - timedelta(seconds=1)
    index.fail = False
    assert svc.process_outbox(instance_id='home')['processed'] == 1
    assert svc.get('home', mid)

def test_chunking_covers_long_source_and_jobs_complete(rig):
    svc, sessions, extractor, _, _ = rig
    text = 'a' * 5000
    add(svc, text=text)
    with sessions() as db:
        rows = db.scalars(select(ExtractionJob).order_by(ExtractionJob.chunk_number)).all()
        assert len(rows) == len(chunks(text)) and all(len(x.text) <= 2000 for x in rows)
    svc.process_extractions(instance_id='home', max_items=1)
    with sessions() as db: assert db.scalar(select(SourceRevision)).extracted_at is None
    svc.process_extractions(instance_id='home')
    with sessions() as db: assert db.scalar(select(SourceRevision)).extracted_at is not None
