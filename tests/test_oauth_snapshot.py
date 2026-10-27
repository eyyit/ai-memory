"""Synthetic DiskCache snapshot tests; no live OAuth state is used."""
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time

from cryptography.fernet import Fernet
from diskcache import Cache
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
import pytest

from scripts import recovery

COLLECTION = 'synthetic-oauth'
PAYLOAD = {'secret': 'synthetic-canary-' + ('x' * 8192)}


def make_store(directory, key):
  cache = Cache(
      directory=str(directory),
      disk_min_file_size=1,
      eviction_policy='none',
  )
  cache.set('__fixture_seed__', b'synthetic')
  store = DiskStore(disk_cache=cache)
  wrapped = FernetEncryptionWrapper(
      key_value=store,
      fernet=Fernet(key),
  )
  return cache, store, wrapped


def run_async(coro):
  return asyncio.run(coro)


def filenames(directory):
  database = directory / 'cache.db'
  with closing(sqlite3.connect(database)) as connection:
    return [
        row[0] for row in connection.execute(
            'SELECT filename FROM Cache WHERE filename IS NOT NULL')
    ]


def test_snapshot_integrity_external_values_and_decryption(tmp_path):
  source = tmp_path / 'source'
  target = tmp_path / 'snapshot'
  key = Fernet.generate_key()
  cache, _, wrapped = make_store(source, key)
  run_async(
      wrapped.put(
          key='synthetic-record',
          value=PAYLOAD,
          collection=COLLECTION,
      ))
  source_names = filenames(source)
  assert source_names
  assert all((source / name).is_file() for name in source_names)
  cache.close()

  recovery.snapshot_oauth(source, target)

  with closing(
      sqlite3.connect(f'file:{(target / "cache.db").as_posix()}?mode=ro',
                      uri=True)) as connection:
    assert connection.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    snapshot_names = [
        row[0] for row in connection.execute(
            'SELECT filename FROM Cache WHERE filename IS NOT NULL')
    ]
  assert snapshot_names == source_names
  assert all((target / name).is_file() for name in snapshot_names)

  restored_cache, _, restored = make_store(target, key)
  try:
    assert run_async(restored.get(key='synthetic-record',
                                  collection=COLLECTION)) == PAYLOAD
  finally:
    restored_cache.close()


@pytest.mark.parametrize('bad_name', [
    '../escape',
    '/outside/value',
    'cache.db',
    'cache.db-wal',
    'cache.db-shm',
])
def test_snapshot_rejects_unsafe_value_file_paths(tmp_path, bad_name):
  source = tmp_path / 'source'
  target = tmp_path / 'snapshot'
  key = Fernet.generate_key()
  cache, _, wrapped = make_store(source, key)
  run_async(
      wrapped.put(key='synthetic-record', value=PAYLOAD, collection=COLLECTION))
  names = filenames(source)
  assert names
  cache.close()

  with closing(sqlite3.connect(source / 'cache.db')) as connection:
    connection.execute(
        'UPDATE Cache SET filename = ? WHERE filename IS NOT NULL',
        (bad_name,),
    )
    connection.commit()

  with pytest.raises(ValueError):
    recovery.snapshot_oauth(source, target)


@pytest.mark.skipif(not hasattr(Path, 'symlink_to'),
                    reason='symlinks unavailable')
def test_snapshot_rejects_symlink_value_file(tmp_path):
  source = tmp_path / 'source'
  target = tmp_path / 'snapshot'
  outside = tmp_path / 'outside-value'
  outside.write_bytes(b'synthetic external target')
  key = Fernet.generate_key()
  cache, _, wrapped = make_store(source, key)
  run_async(
      wrapped.put(key='synthetic-record', value=PAYLOAD, collection=COLLECTION))
  names = filenames(source)
  assert names
  cache.close()

  value_path = source / names[0]
  value_path.unlink()
  try:
    value_path.symlink_to(outside)
  except (OSError, NotImplementedError):
    pytest.skip('symlink creation is not permitted')

  with pytest.raises(ValueError):
    recovery.snapshot_oauth(source, target)


def test_concurrent_writer_snapshot_is_consistent_and_writes_resume(tmp_path):
  source = tmp_path / 'source'
  target = tmp_path / 'snapshot'
  key = Fernet.generate_key()
  initial_cache, _, initial = make_store(source, key)

  async def put_generation(store, generation):
    await store.put(key='generation',
                    value={'generation': generation},
                    collection=COLLECTION)
    await store.put(
        key='payload',
        value={
            'generation': generation,
            'blob': 'x' * 8192
        },
        collection=COLLECTION,
    )

  run_async(put_generation(initial, 0))
  initial_cache.close()
  writer_cache, _, writer = make_store(source, key)
  acquired = threading.Event()
  release = threading.Event()
  writer_errors = []

  def writer_job():

    async def update_pair():
      with writer_cache.transact():
        await put_generation(writer, 1)
        acquired.set()
        if not release.wait(timeout=5):
          raise TimeoutError('test writer release timed out')

    try:
      run_async(update_pair())
    except BaseException as error:
      writer_errors.append(error)

  snapshot_started = threading.Event()
  snapshot_errors = []

  def snapshot_job():
    snapshot_started.set()
    try:
      recovery.snapshot_oauth(source, target, timeout_seconds=5)
    except BaseException as error:
      snapshot_errors.append(error)

  writer_thread = threading.Thread(target=writer_job)
  writer_thread.start()
  assert acquired.wait(timeout=2)
  snapshot_thread = threading.Thread(target=snapshot_job)
  snapshot_thread.start()
  assert snapshot_started.wait(timeout=2)
  time.sleep(0.1)
  assert snapshot_thread.is_alive()
  release.set()
  writer_thread.join(timeout=5)
  snapshot_thread.join(timeout=5)
  assert not writer_thread.is_alive()
  assert not snapshot_thread.is_alive()
  assert writer_errors == []
  assert snapshot_errors == []
  writer_cache.close()

  snapshot_cache, _, snapshot = make_store(target, key)
  try:
    generation = run_async(snapshot.get(key='generation',
                                        collection=COLLECTION))
    payload = run_async(snapshot.get(key='payload', collection=COLLECTION))
    assert generation['generation'] == payload['generation'] == 1
  finally:
    snapshot_cache.close()

  after_cache, _, after = make_store(source, key)
  try:
    run_async(
        after.put(key='after-snapshot',
                  value={'ok': True},
                  collection=COLLECTION))
    assert run_async(after.get(key='after-snapshot',
                               collection=COLLECTION)) == {
                                   'ok': True
                               }
  finally:
    after_cache.close()


def test_deadline_failure_releases_source_writer_lock(tmp_path):
  source = tmp_path / 'source'
  target = tmp_path / 'snapshot'
  key = Fernet.generate_key()
  cache, _, wrapped = make_store(source, key)
  run_async(
      wrapped.put(key='synthetic-record', value=PAYLOAD, collection=COLLECTION))

  with pytest.raises(TimeoutError):
    recovery.snapshot_oauth(source, target, timeout_seconds=0)

  fresh_cache = Cache(
      directory=str(source),
      timeout=0.1,
      disk_min_file_size=1,
      eviction_policy='none',
  )
  fresh_store = DiskStore(disk_cache=fresh_cache)
  fresh = FernetEncryptionWrapper(
      key_value=fresh_store,
      fernet=Fernet(key),
  )
  try:
    run_async(
        fresh.put(key='after-timeout',
                  value={'ok': True},
                  collection=COLLECTION))
    assert run_async(fresh.get(key='after-timeout', collection=COLLECTION)) == {
        'ok': True
    }
  finally:
    fresh_cache.close()
    cache.close()
