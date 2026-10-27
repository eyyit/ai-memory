"""Private aggregate usage counters; never store tool arguments or content."""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy import (
  BigInteger, Column, DateTime, Float, MetaData, String, Table,
  case, delete, func, select,
)

metadata = MetaData()
buckets = Table(
  'memory_usage_hourly', metadata,
  Column('instance_id', String(128), primary_key=True),
  Column('hour', DateTime(timezone=True), primary_key=True),
  Column('operation', String(16), primary_key=True),
  Column('traffic', String(16), primary_key=True),
  Column('transport', String(16), primary_key=True),
  Column('outcome', String(16), primary_key=True),
  Column('calls', BigInteger, nullable=False),
  Column('results', BigInteger, nullable=False),
  Column('duration_sum', Float, nullable=False),
  Column('duration_max', Float, nullable=False),
)
OPERATIONS = {'search', 'list', 'get', 'add_memory', 'ingest'}
METHODS = {'search': 'search', 'list': 'list', 'get': 'get',
           'add_memory': 'add_memory', 'ingest': 'ingest'}
_lock = threading.Lock()
_last_prune = 0.0
_errors = 0


def record(service, instance, operation, traffic, transport, outcome,
           results, duration):
  """Atomically increment one bounded bucket in PostgreSQL or test SQLite."""
  global _last_prune
  now = datetime.now(timezone.utc)
  with service.sessions.begin() as db:
    dialect = db.bind.dialect.name
    if dialect == 'postgresql':
      from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
      from sqlalchemy.dialects.sqlite import insert
    else:
      raise ValueError('Unsupported usage database')
    row = insert(buckets).values(
      instance_id=instance, hour=now.replace(minute=0, second=0, microsecond=0),
      operation=operation, traffic=traffic, transport=transport,
      outcome=outcome, calls=1, results=results,
      duration_sum=duration, duration_max=duration,
    )
    newer = row.excluded
    db.execute(row.on_conflict_do_update(
      index_elements=[c.name for c in buckets.primary_key],
      set_={
        'calls': buckets.c.calls + 1,
        'results': buckets.c.results + newer.results,
        'duration_sum': buckets.c.duration_sum + newer.duration_sum,
        'duration_max': case(
          (newer.duration_max > buckets.c.duration_max, newer.duration_max),
          else_=buckets.c.duration_max),
      },
    ))
    with _lock:
      if time.monotonic() - _last_prune > 3600:
        db.execute(delete(buckets).where(
          buckets.c.hour < now - timedelta(days=30)))
        _last_prune = time.monotonic()


class UsageService:
  """Observe each public operation once, after authorization."""

  def __init__(self, service, client_id, transport):
    self.service = service
    self.traffic = 'monitor' if client_id == 'nagios-monitor' else 'client'
    if transport not in {'rest', 'mcp'}:
      raise ValueError('Unknown transport')
    self.transport = transport

  def __getattr__(self, name):
    operation = METHODS.get(name)
    function = getattr(self.service, name)
    if operation is None:
      return function

    def invoke(*args, **kwargs):
      global _errors
      instance_id = args[0] if args else kwargs['instance_id']
      started = time.perf_counter()
      outcome, results = 'error', 0
      try:
        value = function(*args, **kwargs)
        outcome = 'success'
        if operation in {'search', 'list'}:
          results = len(value)
          if not results:
            outcome = 'empty'
        elif operation == 'get':
          results = int(value is not None)
          if value is None:
            outcome = 'empty'
        elif isinstance(value, dict) and value.get('duplicate'):
          outcome = 'duplicate'
        return value
      finally:
        try:
          record(self.service, instance_id, operation, self.traffic,
                 self.transport, outcome, results,
                 time.perf_counter() - started)
        except Exception:
          with _lock:
            _errors += 1
          logging.warning('Memory usage counter update failed')
    return invoke


def normalize(row):
  """PostgreSQL SUM(bigint) returns Decimal; expose portable JSON numbers."""
  row = dict(row)
  row['calls'] = int(row['calls'])
  row['results'] = int(row['results'])
  row['duration_max'] = float(row['duration_max'])
  row['duration_average'] = float(row.pop('duration_sum')) / row['calls']
  return row


def report(service, instance_id, hours=24):
  """Return aggregates only; scope by authenticated instance."""
  if not 1 <= hours <= 720:
    raise ValueError('Usage window must be between 1 and 720 hours')
  start = datetime.now(timezone.utc).replace(
    minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
  columns = [buckets.c.operation, buckets.c.traffic, buckets.c.transport,
             buckets.c.outcome]
  query = select(
    *columns, func.sum(buckets.c.calls).label('calls'),
    func.sum(buckets.c.results).label('results'),
    func.sum(buckets.c.duration_sum).label('duration_sum'),
    func.max(buckets.c.duration_max).label('duration_max'),
  ).where(buckets.c.instance_id == instance_id,
          buckets.c.hour >= start).group_by(*columns)
  with service.sessions() as db:
    rows = [dict(row) for row in db.execute(query).mappings()]
    first = db.execute(select(func.min(buckets.c.hour)).where(
      buckets.c.instance_id == instance_id)).scalar()
    timeline = db.execute(select(
      buckets.c.hour, buckets.c.traffic,
      func.sum(buckets.c.calls).label('calls'),
    ).where(buckets.c.instance_id == instance_id,
            buckets.c.hour >= start,
            buckets.c.operation.in_(['search', 'list', 'get']))
      .group_by(buckets.c.hour, buckets.c.traffic)
      .order_by(buckets.c.hour)).mappings().all()
  rows = [normalize(row) for row in rows]
  return {
    'window_start_utc': start.isoformat(), 'hour_buckets': hours,
    'earliest_retained_bucket': first.isoformat() if first else None,
    'retention_days': 30, 'counts': rows,
    'hourly_retrievals': [
      {'hour_utc': row['hour'].isoformat(), 'traffic': row['traffic'],
       'calls': int(row['calls'])} for row in timeline],
    'recorder_errors_since_api_start': _errors,
    'history_before_deployment_available': False,
  }
