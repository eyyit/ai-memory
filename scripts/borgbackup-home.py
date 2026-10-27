#!/usr/bin/env python3
"""
Prepare consistent memory recovery inputs, then run the existing server Borg
job.
"""

import fcntl
import json
import os
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1]
SITE_FILE = SITE_ROOT / 'private/runtime.json'
SITE = json.loads(SITE_FILE.read_text()) if SITE_FILE.exists() else {}

import subprocess
import sys


def main():
  if os.geteuid() != 0:
    raise SystemExit('Server Borg backup wrapper requires root')
  os.umask(0o077)
  os.nice(19)
  subprocess.run(['ionice', '-c', '3', '-p', str(os.getpid())], check=True)
  with Path('/run/lock/ai-memory-borgbackup.lock').open('a') as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      raise SystemExit('Another memory/server backup run is already active')
    subprocess.run(
      [sys.executable, str(Path(__file__).with_name('recovery.py')), 'backup'],
      check=True,
    )
    subprocess.run(
      [
        SITE.get(
          'borg_backup_script', '/srv/backup-jobs/borgbackup/borgbackup.py'
        ),
        SITE.get('borg_config_file', '/srv/backup-jobs/borgbackup/server.json'),
      ],
      check=True,
    )


if __name__ == '__main__':
  main()
