import importlib.util
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ai_memory.adapters import HTTPExtractor, QuotaExhaustedError, SwitchingExtractor
from ai_memory.agy_bridge import (
    create_bridge_app,
    is_quota_error,
    select_best_flash_model,
)
from ai_memory.models import ExtractionJob, now_utc


def test_select_best_flash_model_picks_highest_version():
    sample = """
    gemini-3.8-flash-high   Gemini 3.8 Flash (High)
    gemini-3.8-flash-low    Gemini 3.8 Flash (Low)
    gemini-4.0-flash-low    Gemini 4.0 Flash (Low)
    gemini-4.10-flash-low   Gemini 4.10 Flash (Low)
    gemini-4.2-flash-low    Gemini 4.2 Flash (Low)
    """
    assert select_best_flash_model(sample) == 'gemini-4.10-flash-low'
    assert select_best_flash_model('no matching models', fallback='gemini-3.8-flash-low') == 'gemini-3.8-flash-low'


def test_is_quota_error_detects_rate_limit_signals():
    assert is_quota_error('Error: HTTP 429 RESOURCE_EXHAUSTED')
    assert is_quota_error('', 'Weekly quota exhausted')
    assert not is_quota_error('SyntaxError: invalid JSON')


def test_http_extractor_raises_quota_error_on_429():
    def handler(request):
        return httpx.Response(429, headers={'Retry-After': '7200'}, json={'detail': 'quota'})

    with httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(handler)) as client:
        ext = HTTPExtractor('http://localhost', 'tok', client=client)
        with pytest.raises(QuotaExhaustedError) as exc:
            ext.extract('I prefer tea.')
    assert exc.value.retry_after == 7200


def test_switching_extractor_reads_live_mode_file(tmp_path):
    calls = []

    class Dummy:
        def __init__(self, name):
            self.name = name

        def extract(self, text):
            calls.append(self.name)
            return [{'content': self.name, 'evidence': text, 'confidence': 1.0}]

    mode_file = tmp_path / 'mode.txt'
    mode_file.write_text('agy\n', encoding='utf-8')
    sw = SwitchingExtractor(Dummy('agy'), Dummy('qwen'), mode_file=str(mode_file))
    sw.extract('hello')
    mode_file.write_text('qwen\n', encoding='utf-8')
    sw.extract('hello')
    assert calls == ['agy', 'qwen']


def test_quota_exhausted_backs_off_up_to_24h_without_losing_job(rig):
    svc, sessions, extractor, _, _ = rig
    extractor.extract = lambda text: (_ for _ in ()).throw(QuotaExhaustedError('weekly limit', retry_after=7200))
    svc.ingest(instance_id='home', source='test', source_record_id='q1', text='I prefer tea.')
    before = now_utc()
    assert svc.process_extractions(instance_id='home')['failed'] == 1
    with sessions() as db:
        job = db.scalar(select(ExtractionJob))
        assert job.attempts == 1
        assert job.last_error.startswith('QuotaExhausted:')
        avail = job.available_at.replace(tzinfo=before.tzinfo) if job.available_at.tzinfo is None else job.available_at
        assert avail >= before + timedelta(seconds=7100)


def test_bridge_app_auth_extract_and_429(tmp_path):
    model_file = tmp_path / 'model.txt'
    model_file.write_text('gemini-4.0-flash-low\n', encoding='utf-8')
    seen_models = []

    def fake_extract(text, *, agy_bin='agy', model=''):
        seen_models.append(model)
        if 'quota' in text:
            raise QuotaExhaustedError('exhausted', retry_after=3600)
        return [{'content': 'Prefers tea.', 'type': 'preference', 'evidence': text, 'confidence': 0.9}]

    app = create_bridge_app(token='secret-tok', model_file=str(model_file), extractor_fn=fake_extract)
    with TestClient(app) as client:
        assert client.get('/health').json()['model'] == 'gemini-4.0-flash-low'
        assert client.post('/v1/extract', json={'text': 'I prefer tea.'}).status_code == 401
        ok = client.post('/v1/extract', headers={'Authorization': 'Bearer secret-tok'}, json={'text': 'I prefer tea.'})
        assert ok.status_code == 200 and ok.json()['memories'][0]['content'] == 'Prefers tea.'
        rl = client.post('/v1/extract', headers={'Authorization': 'Bearer secret-tok'}, json={'text': 'trigger quota'})
        assert rl.status_code == 429 and rl.headers['retry-after'] == '3600'
    assert seen_models == ['gemini-4.0-flash-low', 'gemini-4.0-flash-low']


def test_nagios_queue_state_warns_on_quota_and_critical_at_7_days():
    script_path = Path(__file__).resolve().parents[1] / 'deploy' / 'scripts' / 'check-memory.py'
    spec = importlib.util.spec_from_file_location('check_memory', script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 2 days old quota block -> WARNING (1), not CRITICAL (2)
    state, metrics = mod.queue_state([
        {'name': 'extraction', 'pending': 3, 'failed': 3, 'quota_blocked': 3, 'attempts': 5, 'age': 2 * 86400},
        {'name': 'index', 'pending': 0, 'failed': 0, 'quota_blocked': 0, 'attempts': 0, 'age': 0},
    ])
    assert state == 1 and 'extraction_quota_blocked=3' in metrics

    # 7 days old quota block -> CRITICAL (2)
    state7, _ = mod.queue_state([
        {'name': 'extraction', 'pending': 3, 'failed': 3, 'quota_blocked': 3, 'attempts': 12, 'age': 7 * 86400},
        {'name': 'index', 'pending': 0, 'failed': 0, 'quota_blocked': 0, 'attempts': 0, 'age': 0},
    ])
    assert state7 == 2
