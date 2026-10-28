#!/usr/bin/python3
"""Shared user-global hook dispatcher, preserving native repository hooks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.parse
import urllib.request

LIB = Path('/usr/local/libexec/git-push-guard')
HOOKS = Path('/usr/local/share/git-push-guard/hooks')
CONFIG = Path('/etc/git-push-guard/repos.json')


def git(*args):
  return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL)


def common_directory(name):
  try:
    return Path(git('rev-parse', '--path-format=absolute',
                    '--git-common-dir').decode().strip())
  except subprocess.CalledProcessError:
    if name == 'pre-push' or not os.environ.get('GIT_DIR'):
      raise
    # During git init, the first reference update precedes a valid HEAD.
    directory = Path(os.environ['GIT_DIR']).resolve()
    pointer = directory / 'commondir'
    if pointer.is_file():
      directory = (directory / pointer.read_text().strip()).resolve()
    return directory


def github_destination(value):
  if value.startswith('git@github.com:'):
    path = value[len('git@github.com:'):]
  else:
    parts = urllib.parse.urlsplit(value)
    if parts.hostname != 'github.com' or parts.query or parts.fragment:
      raise ValueError('Unapproved backup destination')
    if parts.scheme == 'https':
      if parts.username or parts.password or parts.port:
        raise ValueError('Unapproved backup HTTPS destination')
    elif parts.scheme == 'ssh':
      if parts.username != 'git' or parts.password or parts.port:
        raise ValueError('Unapproved backup SSH destination')
    else:
      raise ValueError('Unapproved backup transport')
    path = parts.path.lstrip('/')
  path = path.removesuffix('.git')
  pieces = path.split('/')
  if len(pieces) != 2 or not all(pieces):
    raise ValueError('Invalid GitHub backup repository')
  return path.lower()


def validate_private_metadata(data, approved):
  expected_id = approved.get('repository_id')
  if (type(expected_id) is not int or expected_id <= 0
      or data.get('id') != expected_id
      or data.get('full_name', '').lower() != approved['repository'].lower()
      or data.get('private') is not True
      or data.get('visibility') != 'private'):
    raise ValueError('Backup repository privacy or identity unverified')


class NoRedirect(urllib.request.HTTPRedirectHandler):
  def redirect_request(self, *args, **kwargs):
    return None


def verify_private(approved, token_file):
  token = Path(token_file).read_text().strip()
  if not token or '\n' in token or '\r' in token:
    raise ValueError('GitHub metadata authentication unavailable')
  request = urllib.request.Request(
    'https://api.github.com/repos/' + approved['repository'],
    headers={
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'User-Agent': 'AI-Memory-Global-Push-Guard',
    },
  )
  opener = urllib.request.build_opener(NoRedirect)
  with opener.open(request, timeout=10) as response:
    data = json.load(response)
  validate_private_metadata(data, approved)


def backup_exception(root, common, destination, config):
  checkouts = {str(root.resolve()), str(common.parent.resolve())}
  for approved in config.get('private_backup_exceptions', []):
    if approved['checkout'] not in checkouts:
      continue
    if github_destination(destination) != approved['repository'].lower():
      raise ValueError('Backup push destination changed; push blocked')
    verify_private(approved, config['github_metadata_token_file'])
    return True
  return False


def main():
  name = Path(sys.argv[0]).name
  common = common_directory(name)
  native = common / 'hooks' / name
  if native.exists() and native.resolve() == Path(__file__).resolve():
    raise ValueError('Recursive native hook configuration')
  if name != 'pre-push':
    if native.is_file() and os.access(native, os.X_OK):
      os.execv(str(native), [str(native), *sys.argv[1:]])
    return
  data = sys.stdin.buffer.read()
  try:
    root = Path(git('rev-parse', '--show-toplevel').decode().strip())
  except subprocess.CalledProcessError:
    root = Path(git('rev-parse', '--absolute-git-dir').decode().strip())
  config = json.loads(CONFIG.read_text())
  if len(sys.argv) != 3:
    raise ValueError('Missing Git push destination')
  exempt = backup_exception(root, common, sys.argv[2], config)
  policy_map = config['repo_policies']
  policy = (policy_map.get(str(root.resolve()))
            or policy_map.get(str(common.parent.resolve())))
  args = [
    '/usr/bin/python3', str(LIB / 'check-push.py'), '--pre-push',
    '--repository', str(root), '--scanner', str(LIB / 'gitleaks'),
    '--secret-config', str(LIB / 'gitleaks.toml'),
  ]
  args += ['--policy', policy] if policy else ['--secrets-only']
  if not exempt:
    subprocess.run(args, input=data, check=True)
  else:
    print('Private backup exception verified; publication scan skipped')
  if native.is_file() and os.access(native, os.X_OK):
    subprocess.run([str(native), *sys.argv[1:]], input=data, check=True)


if __name__ == '__main__':
  try:
    main()
  except (OSError, ValueError, subprocess.CalledProcessError):
    print('Push/hook blocked: global guard failed; investigate before retry.',
          file=sys.stderr)
    sys.exit(1)
