import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).parent.resolve()
sys.path[:0] = [str(ROOT / 'test-deps'), str(ROOT / 'proposed')]
if (ROOT / 'test-temp').is_dir():
  tempfile.tempdir = str(ROOT / 'test-temp')
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import sessionmaker
try:
  import usage
except ModuleNotFoundError:
  from ai_memory import usage


class Service:
  def __init__(self, engine):
    self.sessions = sessionmaker(bind=engine)
    self.value = [{'id': 'example'}]
    self.raises = False

  def search(self, instance_id, query, limit=10):
    if self.raises:
      raise ValueError('private query and credentials must not leak')
    return self.value

  def list(self, instance_id, *, limit=50, offset=0):
    return self.value

  def get(self, instance_id, memory_id):
    return self.value[0] if self.value else None

  def add_memory(self, instance_id, content, source_record_id):
    return {'id': 'example', 'duplicate': True}

  def ingest(self, *, instance_id, source, source_record_id, text):
    return {'pending': True}


class UsageTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.engine = create_engine(
      'sqlite:///' + str(Path(self.temp.name) / 'test.db'))
    self.addCleanup(self.engine.dispose)
    usage.metadata.create_all(self.engine)
    usage._last_prune = 0
    usage._errors = 0
    self.service = Service(self.engine)
    self.client = usage.UsageService(self.service, 'writer', 'rest')

  def rows(self):
    with self.service.sessions() as db:
      return [dict(r) for r in db.execute(select(usage.buckets)).mappings()]

  def test_counts_aggregate_without_private_arguments(self):
    for _ in range(2):
      self.client.search('home', 'private secret example')
    rows = self.rows()
    self.assertEqual(rows[0]['calls'], 2)
    self.assertEqual(rows[0]['results'], 2)
    self.assertNotIn('private secret example', str(rows))
    self.assertNotIn('writer', str(rows))

  def test_monitor_is_separate_from_client(self):
    self.client.search('home', 'a')
    monitor = usage.UsageService(self.service, 'nagios-monitor', 'rest')
    monitor.search('home', 'b')
    self.assertEqual({r['traffic'] for r in self.rows()}, {'client', 'monitor'})

  def test_empty_and_error_outcomes(self):
    self.service.value = []
    self.client.search('home', 'a')
    self.service.raises = True
    with self.assertRaises(ValueError):
      self.client.search('home', 'b')
    self.assertEqual({r['outcome'] for r in self.rows()}, {'empty', 'error'})

  def test_keyword_ingest_and_duplicate_write(self):
    self.client.ingest(instance_id='home', source='test',
                       source_record_id='test', text='private')
    self.client.add_memory('home', 'private', 'source-id')
    self.assertEqual({r['outcome'] for r in self.rows()},
                     {'success', 'duplicate'})

  def test_instance_isolation(self):
    self.client.search('home', 'a')
    self.client.search('other', 'b')
    result = usage.report(self.service, 'home')
    self.assertEqual(sum(r['calls'] for r in result['counts']), 1)

  def test_metrics_failure_does_not_break_operation(self):
    usage.metadata.drop_all(self.engine)
    self.assertEqual(self.client.search('home', 'a'), self.service.value)
    self.assertEqual(usage._errors, 1)

  def test_retention_only_deletes_usage(self):
    with self.service.sessions.begin() as db:
      db.execute(insert(usage.buckets).values(
        instance_id='home', hour=datetime.now(timezone.utc)-timedelta(days=31),
        operation='search', traffic='client', transport='rest',
        outcome='success', calls=1, results=1, duration_sum=1, duration_max=1))
    self.client.search('home', 'a')
    self.assertEqual(len(self.rows()), 1)

  def test_counts_persist_across_service_recreation(self):
    self.client.search('home', 'a')
    result = usage.report(Service(self.engine), 'home')
    self.assertEqual(result['counts'][0]['calls'], 1)

  def test_invalid_report_window_rejected(self):
    for hours in [0, 721]:
      with self.assertRaises(ValueError):
        usage.report(self.service, 'home', hours)

  def test_postgres_decimal_aggregates_are_json_safe(self):
    import json
    row = usage.normalize({
      'calls': Decimal(2), 'results': Decimal(3),
      'duration_sum': 0.4, 'duration_max': 0.3,
    })
    self.assertEqual(row['duration_average'], 0.2)
    self.assertEqual(row['calls'], 2)
    json.dumps(row)


if __name__ == '__main__':
  unittest.main()
