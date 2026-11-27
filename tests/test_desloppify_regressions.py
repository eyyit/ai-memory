"""Regression tests for M-02, M-09, and M-10 in ai-memory."""

from datetime import timedelta
from ai_memory.db import Base, make_engine, make_session_factory
from ai_memory.models import Memory, now_utc
from ai_memory.service import MemoryService


class _DupExtractor:
    def extract(self, text):
        return [
            {"content": "Server runs Debian 13", "type": "fact", "confidence": 0.9},
            {"content": "  server   runs debian 13  ", "type": "fact", "confidence": 0.8},
            {"content": "Distinct second fact", "type": "fact", "confidence": 0.9},
        ]


class _FakeEmbedder:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


class _FakeIndex:
    def __init__(self):
        self.upserted = []
        self.deleted = []

    def upsert(self, iid, mid, vec, payload):
        self.upserted.append((iid, mid))

    def delete(self, iid, mid):
        self.deleted.append((iid, mid))

    def search(self, iid, vec, limit):
        return []


def _make_service(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'mem.db'}")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)
    idx = _FakeIndex()
    return MemoryService(sf, _DupExtractor(), _FakeEmbedder(), idx), sf, idx


def test_m02_duplicate_extracted_facts_do_not_rollback_chunk(tmp_path):
    svc, _, _ = _make_service(tmp_path)
    svc.ingest(instance_id="inst", source="test", source_record_id="r1", text="raw text")
    res = svc.process_extractions(instance_id="inst", max_items=5)
    assert res["processed"] == 1
    assert res["failed"] == 0
    assert res["memories_created"] == 2


def test_m09_pinned_memory_survives_more_than_200_newer_memories(tmp_path):
    svc, sf, _ = _make_service(tmp_path)
    pinned_res = svc.add_memory("inst", "Core pinned rule", "pin-1", memory_type="rule", pinned=True)
    # Age the pinned memory so 210 newer memories sort ahead of it
    with sf.begin() as db:
        m = db.get(Memory, pinned_res["id"])
        m.updated_at = now_utc() - timedelta(days=365)
    for i in range(210):
        svc.add_memory("inst", f"Routine fact number {i}", f"fact-{i}")
    brief = svc.context_brief("inst", pinned_limit=20, recent_limit=10)
    pinned_ids = [item["id"] for item in brief["pinned"]]
    assert pinned_res["id"] in pinned_ids


def test_m10_readding_deleted_memory_restores_and_reindexes(tmp_path):
    svc, _, idx = _make_service(tmp_path)
    first = svc.add_memory("inst", "Important homelab note", "note-1")
    mid = first["id"]
    assert svc.delete("inst", mid) is True
    assert svc.get("inst", mid) is None

    second = svc.add_memory("inst", "Important homelab note", "note-2")
    assert second["id"] == mid
    assert second["duplicate"] is False
    got = svc.get("inst", mid)
    assert got is not None
    assert got["content"] == "Important homelab note"
    svc.process_outbox(instance_id="inst", max_items=10)
    assert ("inst", mid) in idx.upserted
