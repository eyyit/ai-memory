#!/usr/bin/env python3
"""Read-only Nagios probes; never print memory contents or credentials."""

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request


def request_json(url: str, headers: dict, payload: dict | None = None) -> dict:
  """Make a bounded HTTP request without following redirects."""
  from urllib.request import HTTPRedirectHandler, build_opener

  class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
      return None

  data = None if payload is None else json.dumps(payload).encode()
  request = Request(url, data=data, headers={
    **headers, 'Content-Type': 'application/json',
  })
  with build_opener(NoRedirect()).open(request, timeout=8) as response:
    return json.loads(response.read(1048576))


def queue_state(rows: list[dict]) -> tuple[int, str]:
  """Judge age and retries, allowing young healthy queues to stay OK."""
  state = 0
  metrics = []
  for row in rows:
    name = row['name']
    age = max(0, float(row['age'] or 0))
    attempts = int(row['attempts'] or 0)
    warning, critical = (900, 3600) if name == 'extraction' else (300, 900)
    if age >= critical or attempts >= 10:
      state = 2
    elif age >= warning or attempts >= 3:
      state = max(state, 1)
    age_name = 'oldest_age' if name == 'extraction' else 'ready_age'
    metrics.extend([
      f'{name}_pending={row["pending"]}',
      f'{name}_failed_pending={row["failed"]}',
      f'{name}_max_attempts={attempts};3;10',
      f'{name}_{age_name}={age:.0f}s;{warning};{critical}',
    ])
  return state, ' '.join(metrics)


def queue_probe() -> tuple[int, str]:
  """Read job summaries in a PostgreSQL read-only transaction."""
  from sqlalchemy import create_engine, text
  from ai_memory.config import Settings

  settings = Settings.from_env()
  engine = create_engine(settings.database_url, connect_args={
    'connect_timeout': 5, 'options': '-c statement_timeout=5000',
  })
  rows = []
  try:
    with engine.connect() as connection:
      connection.execute(text('SET TRANSACTION READ ONLY'))
      for name, table, age_column in [
        ('extraction', 'extraction_jobs', 'created_at'),
        ('index', 'outbox', 'available_at'),
      ]:
        # Table and column names come only from this fixed allowlist.
        query = text(f'''
          SELECT count(*) AS pending,
            count(*) FILTER (WHERE last_error IS NOT NULL) AS failed,
            coalesce(max(attempts), 0) AS attempts,
            coalesce(extract(epoch FROM
              CURRENT_TIMESTAMP - min({age_column})), 0) AS age
          FROM {table}
          WHERE instance_id = :instance AND completed_at IS NULL
        ''')
        row = dict(connection.execute(
          query, {'instance': settings.instance_id}).mappings().one())
        rows.append({'name': name, **row})
  finally:
    engine.dispose()
  state, metrics = queue_state(rows)
  return state, 'job age and retries checked | ' + metrics


def search_probe() -> tuple[int, str]:
  """Verify read-only REST authentication and retrieval of an existing row."""
  token = Path('/run/secrets/monitor_key').read_text().strip()
  if not token:
    raise ValueError('Empty monitoring credential')
  headers = {'Authorization': 'Bearer ' + token}
  base = 'http://127.0.0.1:8080'
  principal = request_json(base + '/v1/me', headers)
  if set(principal['scopes']) != {'read'}:
    return 2, 'monitoring credential must have only read permission'
  if principal['instance_id'] != 'home':
    return 2, 'monitoring credential belongs to the wrong instance'
  records = request_json(base + '/v1/memories?limit=1', headers)['items']
  if not records:
    return 1, 'store is empty; authenticated search cannot be verified'
  record = records[0]
  params = urlencode({'q': record['content'][:1000], 'limit': 5})
  hits = request_json(base + '/v1/memories/search?' + params, headers)
  if record['id'] not in {hit['id'] for hit in hits['items']}:
    return 2, 'an existing record was not returned by search'
  return 0, 'read-only authentication and record retrieval succeeded'


def semantic_probe() -> tuple[int, str]:
  """Check embeddings and Qdrant directly, without lexical fallback."""
  import math
  from ai_memory.config import Settings
  from ai_memory.inference import EMBEDDING_REVISION

  settings = Settings.from_env()
  embedding = request_json(settings.inference_url + '/v1/embed', {
    'Authorization': 'Bearer ' + settings.inference_token,
  }, {'text': 'memory service health check'})
  vector = embedding['vector']
  if (embedding.get('model_revision') != EMBEDDING_REVISION or
      len(vector) != 384 or not all(math.isfinite(v) for v in vector)):
    return 2, 'embedding revision or vector validation failed'
  collection = settings.qdrant_collection
  # The collection is fixed in configuration, never supplied by a caller.
  if not all(c.isalnum() or c in '-_' for c in collection):
    raise ValueError('Invalid collection name')
  hits = request_json(
    settings.qdrant_url + f'/collections/{collection}/points/query',
    {'api-key': settings.qdrant_api_key}, {
      'query': vector, 'limit': 1, 'with_payload': False,
      'filter': {'must': [{
        'key': 'instance_id', 'match': {'value': settings.instance_id},
      }]},
    })
  if not hits['result']['points']:
    return 1, 'semantic query succeeded but no indexed record was returned'
  return 0, 'embedding and Qdrant query succeeded'


def main(mode: str) -> int:
  """Return Nagios status; sanitize failures instead of printing exceptions."""
  probes = {'queue': queue_probe, 'search': search_probe,
            'semantic': semantic_probe}
  if mode not in probes:
    print('AI MEMORY UNKNOWN: invalid probe')
    return 3
  started = time.monotonic()
  try:
    state, message = probes[mode]()
  except Exception as error:
    # Connection errors can contain credentials and private URLs.
    state, message = 2, 'probe failed (' + type(error).__name__ + ')'
  elapsed = time.monotonic() - started
  labels = ['OK', 'WARNING', 'CRITICAL', 'UNKNOWN']
  separator = ' ' if ' | ' in message else ' | '
  print(f'AI MEMORY {mode.upper()} {labels[state]}: {message}'
        f'{separator}probe_seconds={elapsed:.3f}s')
  return state


if __name__ == '__main__':
  sys.exit(main(sys.argv[1] if len(sys.argv) == 2 else ''))
