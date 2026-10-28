"""Block accidental publication of private history and detected secrets."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VERSION = '8.30.1'


def git(*args):
  return subprocess.check_output(['git', '-C', str(ROOT), *args])


def forbidden(name):
  path = PurePosixPath(name)
  denied = {'private', 'secrets', 'exports', 'backups', 'models', 'data',
            '.review', '.review-transfer'}
  return (
    bool(set(path.parts) & denied)
    or path.name == '.gitleaksignore'
    or path.name == '.env'
    or path.name.startswith('.env.') and path.name != '.env.example'
    or name.endswith(('.zip', '.tgz', '.tar', '.tar.gz', '.dump', '.sql.gz',
                      '.db', '.sqlite', '.sqlite3', '.pem', '.key',
                      '.p12', '.pfx'))
  )


def scan_policy(tips, patterns, check_paths=True):
  if not check_paths and not patterns:
    return
  commits = git('rev-list', *tips).decode().splitlines()
  for commit in commits:
    paths = git('ls-tree', '-r', '--name-only', '-z', commit).decode()
    if check_paths and any(
      forbidden(name) for name in paths.split('\0') if name
    ):
      raise ValueError('Private file in history: ' + commit[:12])
  objects = git('rev-list', '--objects', *tips).splitlines()
  for entry in objects:
    oid = entry.split(b' ', 1)[0].decode()
    kind = git('cat-file', '-t', oid).decode().strip()
    if kind not in {'blob', 'commit', 'tag'}:
      continue
    data = git('cat-file', '-p', oid).decode(errors='replace')
    if any(pattern.search(data) for pattern in patterns):
      raise ValueError('Private identifier in Git object: ' + oid[:12])


def reject_ignore_overrides():
  ignored = ROOT / '.gitleaksignore'
  if ignored.exists() or ignored.is_symlink():
    raise ValueError('Repository-local Gitleaks ignores require review')


def main():
  global ROOT
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--pre-push', action='store_true')
  parser.add_argument('--ci', action='store_true')
  parser.add_argument('--repository', type=Path)
  parser.add_argument('--policy', type=Path)
  parser.add_argument('--scanner', type=Path)
  parser.add_argument('--secret-config', type=Path)
  parser.add_argument('--secrets-only', action='store_true')
  args = parser.parse_args()
  if args.repository:
    ROOT = args.repository.resolve()
  if args.pre_push:
    tips = []
    for line in sys.stdin:
      fields = line.split()
      if len(fields) != 4:
        raise ValueError('Invalid Git pre-push input')
      oid = fields[1]
      if set(oid) != {'0'}:
        tips.append(oid)
    if not tips:
      return
  else:
    tips = ['--all']
  reject_ignore_overrides()
  for tip in tips:
    if tip != '--all':
      git('rev-parse', '--verify', tip + '^{commit}')
  patterns = []
  if not args.ci and not args.secrets_only:
    policy_file = args.policy or ROOT / 'private/push-policy.json'
    policy = json.loads(policy_file.read_text())
    if not policy.get('patterns'):
      raise ValueError('Private identifier rules must not be empty')
    patterns = [re.compile(value, re.I) for value in policy['patterns']]
  scan_policy(tips, patterns, check_paths=not args.secrets_only)
  tool = args.scanner or (
    Path(os.environ['RUNNER_TEMP']) / 'gitleaks' if args.ci
    else ROOT / 'private/tools/gitleaks'
  )
  config = args.secret_config or ROOT / '.gitleaks.toml'
  version = subprocess.check_output([str(tool), 'version'], text=True).strip()
  if version != VERSION:
    raise ValueError('Pinned Gitleaks version missing or incorrect')
  subprocess.run([
    str(tool), 'git', str(ROOT), '--config', str(config),
    '--gitleaks-ignore-path=' + str(config.with_suffix('.ignore')),
    '--log-opts=' + ' '.join(tips), '--redact=100', '--no-banner',
    '--ignore-gitleaks-allow',
  ], check=True)
  print('Push guard passed: complete history checked')


if __name__ == '__main__':
  try:
    main()
  except (OSError, ValueError, subprocess.CalledProcessError):
    print('Push blocked: private history, detected secret, or guard failure. '
          'Run the guard locally to investigate; do not bypass it.',
          file=sys.stderr)
    sys.exit(1)
