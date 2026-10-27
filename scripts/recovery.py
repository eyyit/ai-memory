#!/usr/bin/env python3
"""Prepare a private Borg snapshot and perform an isolated recovery drill."""

import argparse
import hashlib
import json
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SITE_ROOT = Path(__file__).resolve().parents[1]
SITE_FILE = SITE_ROOT / 'private/runtime.json'
SITE = json.loads(SITE_FILE.read_text()) if SITE_FILE.exists() else {}

import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid

BACKUPS = Path(
  SITE.get('backups_root', '/var/lib/containers/ai-memory-backups')
)
OAUTH = Path(
  SITE.get('oauth_state_root', '/var/lib/containers/ai-memory-oauth')
)
CREDS = Path(SITE.get('credentials_root', '/etc/ai-memory/secrets'))
DB_CONTAINER = 'ai-memory-postgres'
MAIN_KEYS = (
  'postgres_password',
  'postgres_admin_password',
  'database_url',
  'inference_token',
  'qdrant_api_key',
  'client_key',
  'writer_key',
  'admin_key',
  'auth.json',
)
OAUTH_KEYS = (
  'google_client_id',
  'google_client_secret',
  'jwt_signing_key',
  'storage_encryption_key',
  'allowed_email',
)


def run(*args, **kwargs):
  return subprocess.run(list(args), check=True, **kwargs)


def digest(path):
  with path.open('rb') as stream:
    return hashlib.file_digest(stream, 'sha256').hexdigest()


def copy_regular(source, target):
  if not stat.S_ISREG(source.lstat().st_mode):
    raise ValueError('Recovery inputs must be regular files')
  target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
  shutil.copyfile(source, target)
  target.chmod(0o600)


def snapshot_oauth(source, target, timeout_seconds=5):
  """Snapshot DiskCache using SQLite backup and a short shared writer lock."""
  source = Path(source)
  target = Path(target)
  database = source / 'cache.db'
  if source.is_symlink() or not source.is_dir():
    raise ValueError('OAuth store must be a real directory')
  if not stat.S_ISREG(database.lstat().st_mode):
    raise ValueError('OAuth database must be a regular file')
  if target.exists() or target.is_symlink():
    raise ValueError('OAuth snapshot destination must not exist')
  target.mkdir(parents=True, mode=0o700)
  deadline = time.monotonic() + timeout_seconds

  def check_deadline(*args):
    if time.monotonic() >= deadline:
      raise TimeoutError('OAuth snapshot exceeded its time budget')

  uri = database.resolve().as_uri()
  with closing(
    sqlite3.connect(uri + '?mode=rw', uri=True, timeout=timeout_seconds)
  ) as lock:
    # DiskCache writes use BEGIN IMMEDIATE too. Readers remain permitted.
    lock.execute('BEGIN IMMEDIATE')
    try:
      check_deadline()
      snapshot = target / 'cache.db'
      with closing(sqlite3.connect(uri + '?mode=ro', uri=True)) as reader:
        with closing(sqlite3.connect(snapshot)) as output:
          reader.backup(output, pages=256, progress=check_deadline)
          output.execute('PRAGMA journal_mode=DELETE')
          names = output.execute(
            'SELECT filename FROM Cache WHERE filename IS NOT NULL'
          ).fetchall()
      snapshot.chmod(0o600)
      # DiskCache blob files are immutable; copy only committed references.
      for (name,) in names:
        check_deadline()
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
          raise ValueError('Invalid OAuth value-file path')
        original = source / relative
        if not original.resolve().is_relative_to(source.resolve()):
          raise ValueError('OAuth value-file path leaves the store')
        destination = target / relative
        if relative in {
          Path('cache.db'),
          Path('cache.db-wal'),
          Path('cache.db-shm'),
        }:
          raise ValueError('OAuth value file collides with the database')
        if not stat.S_ISREG(original.lstat().st_mode):
          raise ValueError('OAuth value files must be regular files')
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with original.open('rb') as input_stream:
          with destination.open('wb') as output_stream:
            while chunk := input_stream.read(1024 * 1024):
              check_deadline()
              output_stream.write(chunk)
        destination.chmod(0o600)
      check_deadline()
      lock.commit()
    except BaseException:
      lock.rollback()
      raise


def write_archive(stage, archive):
  files = {
    p.relative_to(stage).as_posix(): digest(p)
    for p in stage.rglob('*')
    if p.is_file()
  }
  manifest = {
    'format': 1,
    'instance': 'home',
    'created_at': datetime.now(timezone.utc).isoformat(),
    'source_commit': run(
      'git',
      '-c',
      'safe.directory=' + str(SITE_ROOT),
      '-C',
      str(SITE_ROOT),
      'rev-parse',
      'HEAD',
      capture_output=True,
      text=True,
    ).stdout.strip(),
    'files': files,
  }
  (stage / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
  (stage / 'manifest.json').chmod(0o600)
  with tarfile.open(archive, 'w:gz') as output:
    for name in [
      'canonical.dump',
      'credentials',
      'oauth-state',
      'manifest.json',
    ]:
      output.add(stage / name, arcname=name)


def safe_extract(archive, target, max_bytes=2**34):
  """Reject traversal, links, duplicate members and unbounded extraction."""
  seen = set()
  total = 0
  with tarfile.open(archive, 'r:gz') as source:
    members = []
    for member in source:
      if len(members) >= 100000:
        raise ValueError('Too many recovery archive entries')
      name = PurePosixPath(member.name)
      if (
        name.is_absolute()
        or '..' in name.parts
        or '\\' in member.name
        or not name.parts
        or name.parts[0]
        not in {'canonical.dump', 'credentials', 'oauth-state', 'manifest.json'}
        or not (member.isfile() or member.isdir())
        or name.as_posix() in seen
      ):
        raise ValueError('Unsafe recovery archive entry')
      seen.add(name.as_posix())
      total += member.size
      if total > max_bytes:
        raise ValueError('Recovery archive exceeds drill extraction limit')
      members.append(member)
    source.extractall(target, members=members, filter='data')
  for path in target.rglob('*'):
    path.chmod(0o700 if path.is_dir() else 0o600)


def verify_manifest(stage):
  manifest = json.loads((stage / 'manifest.json').read_text())
  if manifest.get('format') != 1 or manifest.get('instance') != 'home':
    raise ValueError('Unexpected recovery format or instance')
  actual = {
    p.relative_to(stage).as_posix(): digest(p)
    for p in stage.rglob('*')
    if p.is_file() and p.relative_to(stage).as_posix() != 'manifest.json'
  }
  if actual != manifest.get('files'):
    raise ValueError('Recovery file checksums do not match')
  required = (
    {'canonical.dump'}
    | {'credentials/ai-memory/' + n for n in MAIN_KEYS}
    | {'credentials/ai-memory-oauth/' + n for n in OAUTH_KEYS}
  )
  if not required.issubset(actual) or not (stage / 'oauth-state').is_dir():
    raise ValueError('Recovery archive is incomplete')


def backup():
  BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
  if (
    BACKUPS.is_symlink()
    or BACKUPS.stat().st_uid != 0
    or stat.S_IMODE(BACKUPS.stat().st_mode) != 0o700
  ):
    raise ValueError('Backup directory must be private and not a symlink')
  target = BACKUPS / 'home-recovery-current.tar.gz'
  if target.is_symlink():
    raise ValueError('Snapshot target must not be a symlink')
  with tempfile.TemporaryDirectory(prefix='recovery-', dir=BACKUPS) as temp:
    stage = Path(temp) / 'contents'
    stage.mkdir(mode=0o700)
    snapshot_oauth(OAUTH / 'oauth', stage / 'oauth-state' / 'oauth')
    for service, names in [
      ('ai-memory', MAIN_KEYS),
      ('ai-memory-oauth', OAUTH_KEYS),
    ]:
      for name in names:
        copy_regular(
          CREDS / service / name, stage / 'credentials' / service / name
        )
    with (stage / 'canonical.dump').open('wb') as output:
      run(
        'docker',
        'exec',
        DB_CONTAINER,
        'pg_dump',
        '-U',
        'ai_memory',
        '-d',
        'ai_memory',
        '-Fc',
        stdout=output,
      )
    archive = Path(temp) / 'recovery.tar.gz'
    write_archive(stage, archive)
    archive.chmod(0o600)
    os.replace(archive, target)
  print(
    'Private consistent snapshot prepared for the existing encrypted Borg job:',
    target,
  )


DRILL_CODE = r"""
import asyncio
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet
from key_value.aio.stores.disk import DiskStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from gateway.main import create_app
async def check():
  raw = DiskStore(directory='/state/oauth')
  key_file = Path(os.environ['STORAGE_ENCRYPTION_KEY_FILE'])
  wrapped = FernetEncryptionWrapper(
    key_value=raw,
    fernet=Fernet(key_file.read_bytes().strip()),
  )
  count = 0
  for key in list(raw._cache.iterkeys()):
    value = raw._cache.get(key)
    if not isinstance(value, str):
      continue
    record = json.loads(value)
    wrapped._decrypt_value(record['value'])
    count += 1
  assert count > 0, 'Expected nonempty restored OAuth state'
  await raw.close()
  app = create_app()
  async with app.router.lifespan_context(app):
    pass
  print('Restored OAuth records decrypted; gateway initialized:', count)
asyncio.run(check())
"""


def drill(bundle):
  if (
    bundle.is_symlink()
    or not bundle.is_file()
    or not bundle.name.startswith('home-recovery-')
    or not bundle.name.endswith('.tar.gz')
  ):
    raise ValueError('Supply a restored home-recovery-*.tar.gz snapshot')
  with tempfile.TemporaryDirectory(
    prefix='recovery-drill-', dir=BACKUPS
  ) as temp:
    root = Path(temp)
    stage = root / 'restored'
    stage.mkdir(mode=0o700)
    safe_extract(bundle, stage)
    verify_manifest(stage)
    db = 'memory_recovery_drill_' + uuid.uuid4().hex
    created = False
    try:
      run(
        'docker',
        'exec',
        DB_CONTAINER,
        'createdb',
        '-U',
        'postgres',
        '-T',
        'template0',
        db,
      )
      created = True
      with (stage / 'canonical.dump').open('rb') as dump:
        run(
          'docker',
          'exec',
          '-i',
          DB_CONTAINER,
          'pg_restore',
          '-U',
          'postgres',
          '-d',
          db,
          '--no-owner',
          '--no-acl',
          '--single-transaction',
          stdin=dump,
        )
      counts = run(
        'docker',
        'exec',
        DB_CONTAINER,
        'psql',
        '-U',
        'postgres',
        '-d',
        db,
        '-At',
        '-c',
        'SELECT (SELECT count(*) FROM memories), '
        '(SELECT count(*) FROM source_revisions), '
        '(SELECT count(*) FROM extraction_jobs), '
        '(SELECT count(*) FROM outbox);',
        capture_output=True,
        text=True,
      ).stdout.strip()
      print('Disposable PostgreSQL restore passed; counts:', counts)
    finally:
      if created:
        run('docker', 'exec', DB_CONTAINER, 'dropdb', '-U', 'postgres', db)
    state = stage / 'oauth-state'
    for path in [state, *state.rglob('*')]:
      os.chown(path, 10002, 10002)
    args = [
      'docker',
      'run',
      '--rm',
      '--network',
      'none',
      '--read-only',
      '--user',
      '10002:10002',
      '--cap-drop',
      'ALL',
      '--security-opt',
      'no-new-privileges:true',
      '--cpus',
      '1',
      '--memory',
      '512m',
      '--tmpfs',
      '/tmp:rw,noexec,nosuid,size=64m',
      '--entrypoint',
      'python',
      '-i',
      '-v',
      f'{state}:/state:rw',
    ]
    fields = {
      name.upper(): stage / 'credentials' / 'ai-memory-oauth' / name
      for name in OAUTH_KEYS
    }
    fields['BACKEND_KEY'] = stage / 'credentials' / 'ai-memory' / 'writer_key'
    for field, path in fields.items():
      os.chown(path, 10002, 10002)
      args += [
        '-v',
        f'{path}:/run/secrets/{field}:ro',
        '-e',
        f'{field}_FILE=/run/secrets/{field}',
      ]
    args += ['ai-memory-oauth:latest']
    run(*args, input=DRILL_CODE, text=True)
  print(
    'Recovery drill passed; live database, sessions and secrets '
    'were not replaced'
  )


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('action', choices=['backup', 'drill'])
  parser.add_argument('--bundle', type=Path)
  args = parser.parse_args()
  if os.geteuid() != 0:
    parser.error(
      'Run as root only after approving the displayed batch commands'
    )
  os.umask(0o077)
  if args.action == 'backup':
    backup()
  elif args.bundle is None:
    parser.error('drill requires --bundle')
  else:
    drill(args.bundle)


if __name__ == '__main__':
  main()
