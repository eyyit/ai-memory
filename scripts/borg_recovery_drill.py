#!/usr/bin/env python3
"""Restore a memory-only archive through Borg, then run isolation tests."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1]
SITE_FILE = SITE_ROOT / 'private/runtime.json'
SITE = json.loads(SITE_FILE.read_text()) if SITE_FILE.exists() else {}

import subprocess
import sys
import tempfile
import uuid

REPO = SITE.get('borg_repository', '/data/backup/server')
BACKUPS = Path(
  SITE.get('backups_root', '/var/lib/containers/ai-memory-backups')
)
SNAPSHOT = BACKUPS / 'home-recovery-current.tar.gz'
BORG = '/borg-env/bin/borg'


def command(*args, restore_dir=None):
  result = [
    'docker',
    'run',
    '--rm',
    '--network',
    'none',
    '--env-file',
    str(
      Path(SITE.get('credentials_root', '/etc/ai-memory/secrets'))
      / 'borgbackup_passphrase.docker'
    ),
    '-v',
    '/root:/root',
    '-v',
    f'{REPO}:{REPO}',
    '-v',
    f'{BACKUPS}:{BACKUPS}:ro',
  ]
  if restore_dir is not None:
    result += ['-v', f'{restore_dir}:/restore', '--workdir', '/restore']
  return result + ['borgbackup:latest', BORG, *args]


def main():
  if os.geteuid() != 0:
    raise SystemExit('Run as root after approving the displayed commands')
  os.umask(0o077)
  if SNAPSHOT.is_symlink() or not SNAPSHOT.is_file():
    raise SystemExit('Prepare the private memory snapshot first')
  timestamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
  archive = REPO + '::memory-recovery-drill-' + timestamp
  archive += '-' + uuid.uuid4().hex[:8]
  subprocess.run(
    command('create', '-C', 'lz4', archive, str(SNAPSHOT)), check=True
  )
  with tempfile.TemporaryDirectory(
    prefix='borg-recovery-drill-', dir=BACKUPS
  ) as temp:
    member = SNAPSHOT.as_posix().lstrip('/')
    subprocess.run(
      command(
        'extract', '--strip-components', '3', archive, member, restore_dir=temp
      ),
      check=True,
    )
    restored = Path(temp) / SNAPSHOT.name
    subprocess.run(
      [
        sys.executable,
        str(Path(__file__).with_name('recovery.py')),
        'drill',
        '--bundle',
        str(restored),
      ],
      check=True,
    )
  print('Borg-backed isolated recovery drill passed:', archive)
  print('No production data was replaced; no archives were pruned')


if __name__ == '__main__':
  main()
