import importlib.util
import json
import shutil
from pathlib import Path


def test_recovery_reads_optional_private_paths(tmp_path):
  root = Path(__file__).resolve().parents[1]
  scripts = tmp_path / 'scripts'
  scripts.mkdir()
  private = tmp_path / 'private'
  private.mkdir()
  settings = {
    'backups_root': str(tmp_path / 'backups'),
    'oauth_state_root': str(tmp_path / 'oauth'),
    'credentials_root': str(tmp_path / 'secrets'),
  }
  (private / 'runtime.json').write_text(json.dumps(settings))
  target = scripts / 'recovery.py'
  shutil.copyfile(root / 'scripts/recovery.py', target)
  spec = importlib.util.spec_from_file_location('recovery_private', target)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  assert str(module.BACKUPS) == settings['backups_root']
  assert str(module.OAUTH) == settings['oauth_state_root']
  assert str(module.CREDS) == settings['credentials_root']
