#!/usr/bin/python3
"""
Approved root deployment only; reuses existing Google client, never prints
secrets.
"""

import json
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[2]
SITE_FILE = SITE_ROOT / 'private/runtime.json'
SITE = json.loads(SITE_FILE.read_text()) if SITE_FILE.exists() else {}

import base64
import os
import secrets
import tomllib

if os.geteuid() != 0:
  raise SystemExit('Requires root')
config = tomllib.loads(Path('/etc/docker/oauth2_proxy/memory.conf').read_text())
if (
  config.get('redirect_url')
  != SITE.get('public_base_url', 'https://memory.example.com')
  + '/oauth2/callback'
):
  raise SystemExit('Unexpected callback URL; review first')
emails = [
  row.strip()
  for row in Path('/etc/docker/oauth2_proxy/memory.emails')
  .read_text()
  .splitlines()
  if row.strip() and not row.lstrip().startswith('#')
]
if len(emails) != 1 or '@' not in emails[0]:
  raise SystemExit('Expected exactly one permitted Google account')
folder = (
  Path(SITE.get('credentials_root', '/etc/ai-memory/secrets'))
  / 'ai-memory-oauth'
)
if folder.exists():
  raise SystemExit('Refusing to overwrite existing OAuth credentials')
values = {
  'google_client_id': config.get('client_id'),
  'google_client_secret': config.get('client_secret'),
  'jwt_signing_key': secrets.token_urlsafe(48),
  'storage_encryption_key': base64.urlsafe_b64encode(
    secrets.token_bytes(32)
  ).decode(),
  'allowed_email': emails[0],
}
if not all(isinstance(v, str) and v for v in values.values()):
  raise SystemExit('Incomplete Google client configuration')
folder.mkdir(mode=0o700)
for name, value in values.items():
  fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(fd, 'w') as f:
    f.write(value + '\n')
  os.chown(folder / name, 10002, 10002)
for name in [
  SITE.get('state_root', '/var/lib/containers') + '/ai-memory-oauth',
  SITE.get('log_root', '/var/log/containers') + '/ai-memory-oauth',
]:
  path = Path(name)
  path.mkdir(mode=0o700, exist_ok=True)
  path.chmod(0o700)
  os.chown(path, 10002, 10002)
print(
  'OAuth credentials and private state/log paths prepared; '
  'existing proxy config unchanged'
)
