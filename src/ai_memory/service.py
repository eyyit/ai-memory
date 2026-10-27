import hashlib
from datetime import timedelta
from sqlalchemy import select, func, or_, update
from sqlalchemy.exc import IntegrityError
from .models import Memory, MemorySource, MemoryRevision, MemoryEvent, SourceRecord, SourceRevision, ExtractionJob, OutboxItem, now_utc, new_id
from .security import redact_secrets, redact_value, sha256_text

def canonical_hash(content):
    return sha256_text(' '.join(content.casefold().split()))

def chunks(text, size=2000, overlap=128):
    if len(text) <= size:
        return [text]
    return [text[i:i+size] for i in range(0, len(text), size-overlap)]

class MemoryService:
    def __init__(self, session_factory, extractor, embedder, index):
        self.sessions, self.extractor, self.embedder, self.index = session_factory, extractor, embedder, index

    def ingest(self, *, instance_id, source, source_record_id, text, conversation_id=None, timestamp=None, metadata=None):
        values = dict(instance_id=instance_id, source=source, source_record_id=source_record_id,
            text=text, conversation_id=conversation_id, timestamp=timestamp, metadata=metadata)
        for attempt in range(3):
            try:
                return self._ingest(**values)
            except IntegrityError:
                if attempt == 2:
                    raise

    def _ingest(self, *, instance_id, source, source_record_id, text, conversation_id=None, timestamp=None, metadata=None):
        source, source_record_id = redact_secrets(source)[:64], redact_secrets(source_record_id)[:512]
        safe = redact_secrets(text)
        digest = sha256_text(safe)
        conversation_id = redact_secrets(conversation_id)[:512] if conversation_id else None
        with self.sessions.begin() as db:
            record = db.scalar(select(SourceRecord).where(SourceRecord.instance_id == instance_id,
                SourceRecord.source == source, SourceRecord.source_record_id == source_record_id).with_for_update())
            if record and record.raw_hash == digest:
                revision = db.scalar(select(SourceRevision).where(SourceRevision.source_record_pk == record.id).order_by(SourceRevision.revision.desc()))
                return {'source_record_id': record.id, 'duplicate': True, 'pending': revision.extracted_at is None, 'memories_created': 0}
            if not record:
                record = SourceRecord(instance_id=instance_id, source=source, source_record_id=source_record_id)
                db.add(record)
            record.raw_hash, record.redacted_text = digest, safe
            record.source_conversation_id, record.source_timestamp = conversation_id, timestamp
            record.metadata_json = redact_value(metadata or {})
            db.flush()
            number = (db.scalar(select(func.max(SourceRevision.revision)).where(SourceRevision.source_record_pk == record.id)) or 0) + 1
            revision = SourceRevision(source_record_pk=record.id, revision=number, raw_hash=digest,
                redacted_text=safe, source_conversation_id=conversation_id, source_timestamp=timestamp, metadata_json=record.metadata_json)
            db.add(revision)
            db.flush()
            for number, part in enumerate(chunks(safe)):
                db.add(ExtractionJob(instance_id=instance_id, source_revision_id=revision.id,
                    chunk_number=number, text=part))
            return {'source_record_id': record.id, 'duplicate': False, 'pending': True, 'memories_created': 0}

    def _store_fact(self, db, instance_id, revision, row):
        content = redact_secrets(str(row['content'])).strip()
        if not content:
            return None, False
        digest = canonical_hash(content)
        memory = db.scalar(select(Memory).where(Memory.instance_id == instance_id, Memory.content_hash == digest))
        created = memory is None
        if created:
            memory = Memory(instance_id=instance_id, content=content, content_hash=digest,
                memory_type=str(row.get('type', 'fact'))[:64], entities=redact_value(row.get('entities') or []),
                attributes=redact_value(row.get('attributes') or {}))
            db.add(memory)
            db.flush()
            db.add(MemoryRevision(memory_id=memory.id, revision=1, content=content, memory_type=memory.memory_type,
                entities=memory.entities, attributes=memory.attributes))
            db.add(MemoryEvent(instance_id=instance_id, memory_id=memory.id, event_type='memory.created',
                payload={'source_revision_id': revision.id}))
            db.add(OutboxItem(instance_id=instance_id, memory_id=memory.id))
        link = db.scalar(select(MemorySource).where(MemorySource.memory_id == memory.id, MemorySource.source_revision_id == revision.id))
        if not link:
            db.add(MemorySource(memory_id=memory.id, source_record_pk=revision.source_record_pk,
                source_revision_id=revision.id, evidence=redact_secrets(str(row.get('evidence', revision.redacted_text))),
                confidence=max(0, min(1, float(row.get('confidence', 0.5))))))
        return memory.id, created

    def add_memory(self, instance_id, content, source_record_id, source='explicit'):
        source = redact_secrets(source)[:64]
        source_record_id = redact_secrets(source_record_id)[:512]
        safe = redact_secrets(content).strip()
        if not safe:
            raise ValueError('Memory cannot be empty')
        # Explicit submissions are already distilled facts; preserve provenance without model inference.
        for attempt in range(3):
            try:
                with self.sessions.begin() as db:
                    record = db.scalar(select(SourceRecord).where(SourceRecord.instance_id == instance_id,
                        SourceRecord.source == source, SourceRecord.source_record_id == source_record_id).with_for_update())
                    if record:
                        revision = db.scalar(select(SourceRevision).where(SourceRevision.source_record_pk == record.id).order_by(SourceRevision.revision.desc()))
                        if record.raw_hash != sha256_text(safe):
                            raise ValueError('Idempotency key already used for different content')
                        mid = db.scalar(select(MemorySource.memory_id).where(MemorySource.source_revision_id == revision.id))
                        return {'id': mid, 'duplicate': True}
                    record = SourceRecord(instance_id=instance_id, source=redact_secrets(source)[:64],
                        source_record_id=redact_secrets(source_record_id)[:512], redacted_text=safe, raw_hash=sha256_text(safe))
                    db.add(record)
                    db.flush()
                    revision = SourceRevision(source_record_pk=record.id, revision=1, raw_hash=record.raw_hash,
                        redacted_text=safe, metadata_json={}, extracted_at=now_utc())
                    db.add(revision)
                    db.flush()
                    mid, created = self._store_fact(db, instance_id, revision, {'content': safe, 'evidence': safe, 'confidence': 1.0, 'attributes': {'origin': 'explicit'}})
                    return {'id': mid, 'duplicate': not created}
            except IntegrityError:
                if attempt == 2: raise

    def _claim(self, model, instance_id):
        cutoff = now_utc() - timedelta(minutes=15)
        with self.sessions.begin() as db:
            ready = [model.completed_at.is_(None), model.available_at <= now_utc(),
                or_(model.locked_at.is_(None), model.locked_at <= cutoff)]
            if instance_id: ready.append(model.instance_id == instance_id)
            item = db.scalar(select(model).where(*ready).order_by(model.available_at).with_for_update(skip_locked=True).limit(1))
            if item is None: return None
            token = new_id()
            # Compare-and-set also protects claims when SQLite ignores FOR UPDATE.
            claimed = db.execute(update(model).where(model.id == item.id, *ready)
                .values(locked_at=now_utc(), lease_token=token).execution_options(synchronize_session=False))
            if claimed.rowcount != 1: return None
            return item.id, token, item.instance_id

    def _failure(self, model, job_id, token, exc):
        with self.sessions.begin() as db:
            row = db.scalar(select(model).where(model.id == job_id, model.lease_token == token).with_for_update())
            if row:
                row.attempts += 1
                row.locked_at, row.lease_token = None, None
                row.last_error = type(exc).__name__ + ': processing failed; check provider'
                row.available_at = now_utc() + timedelta(seconds=min(3600, 2 ** min(row.attempts, 12)))

    def process_extractions(self, *, instance_id=None, max_items=10):
        processed = failed = created = 0
        for _ in range(max_items):
            claim = self._claim(ExtractionJob, instance_id)
            if not claim: break
            job_id, token, iid = claim
            try:
                with self.sessions() as db:
                    job = db.get(ExtractionJob, job_id)
                    source = job.text
                rows = self.extractor.extract(source)
                if not isinstance(rows, list): raise ValueError('Extraction must return an array')
                with self.sessions.begin() as db:
                    job = db.scalar(select(ExtractionJob).where(ExtractionJob.id == job_id,
                        ExtractionJob.lease_token == token, ExtractionJob.locked_at > now_utc() - timedelta(minutes=15)).with_for_update())
                    if job is None: continue
                    revision = db.scalar(select(SourceRevision).where(SourceRevision.id == job.source_revision_id).with_for_update())
                    job_created = 0
                    for row in rows:
                        _, new = self._store_fact(db, iid, revision, redact_value(row))
                        job_created += int(new)
                    job.completed_at, job.locked_at, job.lease_token, job.last_error = now_utc(), None, None, None
                    db.flush()
                    remaining = db.scalar(select(func.count()).select_from(ExtractionJob).where(
                        ExtractionJob.source_revision_id == revision.id, ExtractionJob.completed_at.is_(None)))
                    if not remaining: revision.extracted_at = now_utc()
                processed += 1
                created += job_created
            except Exception as exc:
                self._failure(ExtractionJob, job_id, token, exc)
                failed += 1
        return {'processed': processed, 'failed': failed, 'memories_created': created}

    def process_outbox(self, *, instance_id=None, max_items=100):
        processed = failed = 0
        for _ in range(max_items):
            claim = self._claim(OutboxItem, instance_id)
            if not claim: break
            job_id, token, iid = claim
            try:
                # Keep memory row locked across the bounded indexing call so revision/delete cannot race it.
                with self.sessions.begin() as db:
                    item = db.scalar(select(OutboxItem).where(OutboxItem.id == job_id, OutboxItem.lease_token == token).with_for_update())
                    if item is None: continue
                    memory = db.scalar(select(Memory).where(Memory.id == item.memory_id, Memory.instance_id == iid).with_for_update())
                    if memory and memory.active:
                        self.index.upsert(iid, memory.id, self.embedder.embed(memory.content), {'content': memory.content, 'type': memory.memory_type})
                    else:
                        self.index.delete(iid, item.memory_id)
                    item.completed_at, item.locked_at, item.lease_token, item.last_error = now_utc(), None, None, None
                processed += 1
            except Exception as exc:
                self._failure(OutboxItem, job_id, token, exc)
                failed += 1
        return {'processed': processed, 'failed': failed}

    def queue_status(self, instance_id):
        with self.sessions() as db:
            return {name: db.scalar(select(func.count()).select_from(model).where(
                model.instance_id == instance_id, model.completed_at.is_(None)))
                for name, model in [('extraction_pending', ExtractionJob), ('index_pending', OutboxItem)]}

    def reindex(self, instance_id, *, reset=False):
        if reset: self.index.clear_instance(instance_id)
        with self.sessions.begin() as db:
            memories = db.scalars(select(Memory).where(Memory.instance_id == instance_id, Memory.active.is_(True))).all()
            for memory in memories: db.add(OutboxItem(instance_id=instance_id, memory_id=memory.id))
            return len(memories)

    def search(self, instance_id, query, limit=10):
        query = redact_secrets(query)
        semantic = []
        try:
            semantic = self.index.search(instance_id, self.embedder.embed(query), limit*3)
        except Exception: pass
        with self.sessions() as db:
            scope = [Memory.instance_id == instance_id, Memory.active.is_(True)]
            if db.bind.dialect.name == 'postgresql':
                vector = func.to_tsvector('english', Memory.content)
                tsquery = func.websearch_to_tsquery('english', query)
                lexical = db.scalars(select(Memory.id).where(*scope, vector.op('@@')(tsquery))
                    .order_by(func.ts_rank_cd(vector, tsquery).desc()).limit(limit*3)).all()
            else:
                words = query.casefold().split()
                rows = db.scalars(select(Memory).where(*scope)).all()
                ranked = sorted(((row.id, sum(row.content.casefold().count(word) for word in words)) for row in rows), key=lambda x:x[1], reverse=True)
                lexical = [mid for mid, score in ranked if score > 0][:limit*3]
            scores = {}
            for ids in [lexical, [mid for mid, _ in semantic]]:
                for rank, mid in enumerate(ids, 1): scores[mid] = scores.get(mid, 0) + 1/(60+rank)
            if not scores: return []
            rows = db.scalars(select(Memory).where(*scope, Memory.id.in_(scores))).all()
            by_id = {row.id: row for row in rows}
            order = sorted((mid for mid in scores if mid in by_id), key=scores.get, reverse=True)[:limit]
            return [{'id': mid, 'content': by_id[mid].content, 'type': by_id[mid].memory_type, 'score': scores[mid]} for mid in order]

    def revise(self, instance_id: str, memory_id: str, *, content: str | None = None,
               memory_type: str | None = None, attributes: dict | None = None,
               entities: list | None = None, reason: str = "updated") -> Memory | None:
        with self.sessions.begin() as db:
            memory = db.scalar(select(Memory).where(Memory.id == memory_id, Memory.instance_id == instance_id).with_for_update())
            if memory is None: return None
            safe = redact_secrets(content if content is not None else memory.content)
            changed = safe != memory.content or (memory_type and memory_type != memory.memory_type) or attributes is not None or entities is not None
            if not changed: return memory
            latest = db.scalar(select(func.max(MemoryRevision.revision)).where(MemoryRevision.memory_id == memory.id)) or 0
            memory.content, memory.content_hash = safe, canonical_hash(safe)
            if memory_type: memory.memory_type = redact_secrets(memory_type)[:64]
            if attributes is not None: memory.attributes = redact_value(attributes)
            if entities is not None: memory.entities = redact_value(entities)
            memory.updated_at = now_utc()
            db.add(MemoryRevision(memory_id=memory.id, revision=latest + 1, content=memory.content,
                memory_type=memory.memory_type, entities=memory.entities, attributes=memory.attributes,
                change_reason=redact_secrets(reason)[:128]))
            db.add(OutboxItem(instance_id=instance_id, memory_id=memory.id))
            db.add(MemoryEvent(instance_id=instance_id, memory_id=memory.id, event_type="memory.revised", payload={"reason": redact_secrets(reason), "revision": latest + 1}))
            return memory

    def delete(self, instance_id: str, memory_id: str) -> bool:
        with self.sessions.begin() as db:
            memory = db.scalar(select(Memory).where(Memory.id == memory_id,
                Memory.instance_id == instance_id).with_for_update())
            if memory is None or not memory.active: return False
            memory.active = False
            memory.updated_at = now_utc()
            db.add(OutboxItem(instance_id=instance_id, memory_id=memory.id, operation="delete"))
            db.add(MemoryEvent(instance_id=instance_id, memory_id=memory.id,
                event_type="memory.deleted", payload={}))
            return True

    def get(self, instance_id: str, memory_id: str) -> dict | None:
        with self.sessions() as db:
            m = db.scalar(select(Memory).where(Memory.instance_id == instance_id, Memory.id == memory_id, Memory.active.is_(True)))
            if not m: return None
            links = db.scalars(select(MemorySource).where(MemorySource.memory_id == m.id)).all()
            sources = []
            for link in links:
                src = db.get(SourceRecord, link.source_record_pk)
                revision = db.get(SourceRevision, link.source_revision_id)
                if src and revision and src.instance_id == instance_id:
                    sources.append({"source": src.source, "source_record_id": src.source_record_id,
                        "conversation_id": revision.source_conversation_id, "timestamp": revision.source_timestamp,
                        "source_revision": revision.revision,
                        "source_text": revision.redacted_text, "metadata": revision.metadata_json,
                        "evidence": link.evidence, "confidence": link.confidence})
            return {"id": m.id, "content": m.content, "type": m.memory_type, "entities": m.entities,
                "attributes": m.attributes, "sensitivity": m.sensitivity, "sources": sources,
                "created_at": m.created_at, "updated_at": m.updated_at}

    def list(self, instance_id: str, *, limit=50, offset=0) -> list[dict]:
        with self.sessions() as db:
            rows = db.scalars(select(Memory).where(Memory.instance_id == instance_id, Memory.active.is_(True))
                .order_by(Memory.updated_at.desc()).offset(offset).limit(limit)).all()
            return [{"id": x.id, "content": x.content, "type": x.memory_type, "entities": x.entities,
                "attributes": x.attributes, "sensitivity": x.sensitivity, "updated_at": x.updated_at} for x in rows]
