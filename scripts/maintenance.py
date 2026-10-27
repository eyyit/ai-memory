#!/usr/bin/env python3
"""
Explicit home-only canonical backup/restore; never starts from import or
cron.
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1]
SITE_FILE = SITE_ROOT / 'private/runtime.json'
SITE = json.loads(SITE_FILE.read_text()) if SITE_FILE.exists() else {}

import subprocess

p = argparse.ArgumentParser()
p.add_argument('action', choices=['backup', 'restore'])
p.add_argument('--backup-file', type=Path)
args = p.parse_args()
base = [
  'docker',
  'compose',
  '-f',
  SITE.get('docker_compose_file', '/srv/containers/docker-compose.yml'),
]


def run(*parts):
  subprocess.run(base + list(parts), check=True)


folder = Path(SITE.get('backups_root', '/var/lib/containers/ai-memory-backups'))
if args.action == 'backup':
  target = folder / f'home-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.dump'
  run(
    'exec',
    '-T',
    'ai-memory-postgres',
    'pg_dump',
    '-U',
    'ai_memory',
    '-d',
    'ai_memory',
    '-Fc',
    '-f',
    '/tmp/memory.dump',
  )
  run('cp', 'ai-memory-postgres:/tmp/memory.dump', str(target))
  target.chmod(0o600)
  run('exec', '-T', 'ai-memory-postgres', 'rm', '/tmp/memory.dump')
  print('Canonical backup saved:', target)
else:
  target = args.backup_file
  if (
    target is None
    or not target.is_file()
    or not target.name.startswith('home-')
    or target.suffix != '.dump'
  ):
    p.error('Supply an existing home-prefixed .dump file')
  # Restore replaces canonical data. Execute only in a separately approved
  # restore batch.
  run('stop', 'ai-memory-api', 'ai-memory-worker')
  try:
    with target.open('rb') as dump:
      subprocess.run(
        base
        + [
          'exec',
          '-T',
          'ai-memory-postgres',
          'pg_restore',
          '-U',
          'ai_memory',
          '-d',
          'ai_memory',
          '--clean',
          '--if-exists',
          '--single-transaction',
        ],
        stdin=dump,
        check=True,
      )
    run(
      'run',
      '--rm',
      '--no-deps',
      'ai-memory-api',
      'ai-memory',
      'reindex',
      '--reset',
    )
  finally:
    run('start', 'ai-memory-api', 'ai-memory-worker')
