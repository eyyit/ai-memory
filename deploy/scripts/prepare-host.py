#!/usr/bin/env python3
"""Run only in an approved deployment batch. Generates secrets without printing them."""
import hashlib
import json
import os
from pathlib import Path
import secrets

if os.geteuid() != 0:
    raise SystemExit('Run with sudo only after deployment approval')
config = Path('/etc/ai-memory/secrets/ai-memory')
if config.exists():
    raise SystemExit('Configuration already exists; refusing to overwrite secrets')
config.parent.mkdir(mode=0o700, exist_ok=True)
config.mkdir(mode=0o700)
def write(name, value, uid=10002):
    target = config / name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as output: output.write(value + '\n')
    os.chown(target, uid, uid)
password = secrets.token_hex(32)
write('postgres_password', password, 70)
write('postgres_admin_password', secrets.token_hex(32), 70)
write('database_url', 'postgresql+psycopg://ai_memory:' + password + '@ai-memory-postgres:5432/ai_memory')
write('inference_token', secrets.token_hex(32))
write('qdrant_api_key', secrets.token_hex(32))
clients = []
for name, scopes in [('reader', ['read']), ('writer', ['read', 'write']), ('admin', ['admin'])]:
    key = secrets.token_hex(32)
    write('client_key' if name == 'reader' else name + '_key', key)
    clients.append({'id': name, 'token_sha256': hashlib.sha256(key.encode()).hexdigest(), 'scopes': scopes, 'instance_id': 'home'})
write('auth.json', json.dumps({'clients': clients}, indent=2))
for directory, uid in [('/var/lib/containers/ai-memory-postgres', 70),
    ('/var/lib/containers/ai-memory-qdrant/storage', 10002), ('/var/lib/containers/ai-memory-qdrant/snapshots', 10002),
    ('/var/lib/containers/ai-memory-inference/models', 10002), ('/var/lib/containers/ai-memory-backups', 0)]:
    target = Path(directory)
    target.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chown(target, uid, uid)
    target.chmod(0o700)
for name, uid in [('api',10002), ('worker',10002), ('inference',10002), ('postgres',70), ('qdrant',10002)]:
    target = Path('/var/log/containers/ai-memory-' + name)
    target.mkdir(mode=0o700, exist_ok=True)
    os.chown(target, uid, uid)
print('Prepared home-only paths and private credentials. No services started.')
