"""Regression tests for M-02, M-09, M-10, and deep-dive P2s in ai-memory."""

import importlib.util
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from ai_memory.adapters import QdrantIndex, QuotaExhaustedError
from ai_memory.db import Base, make_engine, make_session_factory
from ai_memory.models import Memory, ProviderCooldown, now_utc
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

    def search(self, iid, vec, limit, *, memory_type=None):
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


def test_m10_same_key_readd_of_deleted_memory_raises_conflict(tmp_path):
    svc, _, _ = _make_service(tmp_path)
    first = svc.add_memory("inst", "Important homelab note", "note-1")
    mid = first["id"]
    assert svc.delete("inst", mid) is True
    assert svc.get("inst", mid) is None

    with pytest.raises(ValueError, match="deleted memory"):
        svc.add_memory("inst", "Important homelab note", "note-1")
    assert svc.get("inst", mid) is None


def test_provider_quota_cooldown_suppresses_queued_jobs_and_workers_until_expiry(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'quota.db'}")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)

    class _QuotaExtractor:
        provider = "agy"

        def __init__(self):
            self.calls = 0
            self.fail_with_quota = True

        def extract(self, text):
            self.calls += 1
            if self.fail_with_quota:
                raise QuotaExhaustedError("429 quota", retry_after=7200)
            return [{"content": f"Fact from {text}", "type": "fact", "confidence": 0.9}]

    extractor = _QuotaExtractor()
    worker1 = MemoryService(sf, extractor, _FakeEmbedder(), _FakeIndex())
    worker2 = MemoryService(sf, extractor, _FakeEmbedder(), _FakeIndex())

    for i in range(3):
        worker1.ingest(instance_id="inst", source="test", source_record_id=f"rec-{i}", text=f"body-{i}")

    res1 = worker1.process_extractions(instance_id="inst", max_items=10)
    assert res1["failed"] == 1
    assert res1["processed"] == 0
    assert extractor.calls == 1

    # Second worker sharing the same DB must also suppress calls during cooldown
    res2 = worker2.process_extractions(instance_id="inst", max_items=10)
    assert res2 == {"processed": 0, "failed": 0, "memories_created": 0}
    assert extractor.calls == 1

    # Expire cooldown and allow extraction to succeed
    extractor.fail_with_quota = False
    with sf.begin() as db:
        cd = db.get(ProviderCooldown, "agy")
        assert cd is not None
        cd.cooldown_until = now_utc() - timedelta(seconds=1)

    res3 = worker2.process_extractions(instance_id="inst", max_items=10)
    assert res3["processed"] == 2
    assert res3["failed"] == 0
    assert extractor.calls == 3


def test_type_filtered_semantic_search_filters_before_candidate_limit(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'search.db'}")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)

    class _TypedVectorIndex:
        def __init__(self):
            self.items = []
            self.last_memory_type = None

        def upsert(self, iid, mid, vec, payload):
            self.items.append((iid, mid, payload["type"]))

        def delete(self, iid, mid):
            pass

        def search(self, iid, vec, limit, *, memory_type=None):
            self.last_memory_type = memory_type
            # Simulate 35 higher-ranked 'fact' points ahead of the 'preference' point
            ranked = [
                (mid, 0.99 - i * 0.001)
                for i, (item_iid, mid, mtype) in enumerate(self.items)
                if item_iid == iid and (memory_type is None or mtype == memory_type)
            ]
            return ranked[:limit]

    idx = _TypedVectorIndex()
    svc = MemoryService(sf, _DupExtractor(), _FakeEmbedder(), idx)
    for i in range(35):
        svc.add_memory("inst", f"Unrelated routine fact {i}", f"fact-{i}", memory_type="fact")
    pref = svc.add_memory("inst", "Prefers dark mode in IDE", "pref-1", memory_type="preference")
    svc.process_outbox(instance_id="inst", max_items=100)

    # Query has no lexical overlap with "Prefers dark mode in IDE", so only semantic search can find it
    results = svc.search("inst", "visual theme setting", limit=10, memory_type="preference")
    assert idx.last_memory_type == "preference"
    assert [r["id"] for r in results] == [pref["id"]]

    # Also verify QdrantIndex.search builds a query_filter including type when memory_type is passed
    qidx = QdrantIndex.__new__(QdrantIndex)
    qidx.collection = "memories"
    qidx.client = mock.MagicMock()
    qidx.client.collection_exists.return_value = True
    qidx.client.query_points.return_value = mock.Mock(points=[])
    qidx.search("inst", [0.1, 0.2], 30, memory_type="preference")
    _, kwargs = qidx.client.query_points.call_args
    must = kwargs["query_filter"].must
    keys_values = {(cond.key, cond.match.value) for cond in must}
    assert ("instance_id", "inst") in keys_values
    assert ("type", "preference") in keys_values


def test_borg_recovery_drill_strip_components_dynamic_depths():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "borg_recovery_drill.py"
    spec = importlib.util.spec_from_file_location("borg_recovery_drill", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Bundled default path has 4 parent directories -> strip_components must be 4
    member, strip_n = mod.strip_components_for_member(
        Path("/var/lib/containers/ai-memory-backups/home-recovery-current.tar.gz")
    )
    assert member == "var/lib/containers/ai-memory-backups/home-recovery-current.tar.gz"
    assert strip_n == 4

    # Shallower and deeper overrides
    assert mod.strip_components_for_member(Path("/backups/snap.tar.gz")) == ("backups/snap.tar.gz", 1)
    assert mod.strip_components_for_member(Path("/a/b/c/d/e/snap.tar.gz")) == ("a/b/c/d/e/snap.tar.gz", 5)
    assert mod.strip_components_for_member(Path("/snap.tar.gz")) == ("snap.tar.gz", 0)
