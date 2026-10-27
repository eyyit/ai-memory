#!/usr/bin/env python3
"""Prepare a read-only monitor credential without printing any secret."""

import hashlib
import json
import os
import secrets
from pathlib import Path


def prepare(directory: Path) -> None:
  """Preserve existing clients and refuse conflicting monitor credentials."""
  auth_path = directory / 'auth.json'
  key_path = directory / 'monitor_key'
  auth = json.loads(auth_path.read_text())
  existing = [row for row in auth['clients']
              if row['id'] == 'nagios-monitor']
  if existing or key_path.exists():
    if len(existing) != 1 or not key_path.exists():
      raise ValueError('Incomplete monitor credential; inspect before retry')
    digest = hashlib.sha256(key_path.read_bytes().strip()).hexdigest()
    row = existing[0]
    if (row['token_sha256'] != digest or row['scopes'] != ['read'] or
        row['instance_id'] != 'home' or row.get('disabled', False)):
      raise ValueError('Existing monitor credential conflicts')
    print('Read-only monitor credential already configured')
    return
  backup = directory / 'auth.before-nagios-monitor.json'
  if backup.exists():
    raise FileExistsError('Backup already exists; inspect before retry')
  stat = auth_path.stat()
  token = secrets.token_urlsafe(32)
  # Exclusive creation prevents overwriting credentials during a retry.
  fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(fd, 'wb') as handle:
    handle.write(auth_path.read_bytes())
    handle.flush()
    os.fsync(handle.fileno())
  fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
  with os.fdopen(fd, 'w') as handle:
    handle.write(token + '\n')
    handle.flush()
    os.fsync(handle.fileno())
  os.chown(key_path, 10002, 10002)
  auth['clients'].append({
    'id': 'nagios-monitor', 'instance_id': 'home', 'scopes': ['read'],
    'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
  })
  staged = directory / 'auth.nagios-monitor.tmp'
  fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(fd, 'w') as handle:
    json.dump(auth, handle, indent=2)
    handle.write('\n')
    handle.flush()
    os.fsync(handle.fileno())
  os.chown(staged, stat.st_uid, stat.st_gid)
  os.chmod(staged, stat.st_mode & 0o777)
  os.replace(staged, auth_path)
  # The old API bind mount remains valid until API recreation.
  print('Read-only monitor credential prepared; recreate API to activate')


if __name__ == '__main__':
  prepare(Path('/etc/ai-memory/secrets/ai-memory'))
