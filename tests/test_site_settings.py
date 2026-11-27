import importlib.util
import json
import shutil
from pathlib import Path


def _load_recovery_copy(tmp_path, name):
  root = Path(__file__).resolve().parents[1]
  scripts = tmp_path / 'scripts'
  scripts.mkdir(exist_ok=True)
  target = scripts / 'recovery.py'
  shutil.copyfile(root / 'scripts/recovery.py', target)
  spec = importlib.util.spec_from_file_location(name, target)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_recovery_reads_optional_private_paths(tmp_path):
  private = tmp_path / 'private'
  private.mkdir()
  settings = {
    'backups_root': str(tmp_path / 'backups'),
    'oauth_state_root': str(tmp_path / 'oauth'),
    'credentials_root': str(tmp_path / 'secrets'),
  }
  (private / 'runtime.json').write_text(json.dumps(settings))
  module = _load_recovery_copy(tmp_path, 'recovery_private')
  assert str(module.BACKUPS) == settings['backups_root']
  assert str(module.OAUTH) == settings['oauth_state_root']
  assert str(module.CREDS) == settings['credentials_root']


def test_recovery_reads_etc_fallback_and_rejects_symlink(tmp_path):
  module = _load_recovery_copy(tmp_path, 'recovery_etc_fallback')
  etc_file = tmp_path / 'etc_runtime.json'
  settings = {
    'backups_root': str(tmp_path / 'etc-backups'),
    'oauth_state_root': str(tmp_path / 'etc-oauth'),
    'credentials_root': str(tmp_path / 'etc-secrets'),
  }
  etc_file.write_text(json.dumps(settings))
  module.ETC_SITE_FILE = etc_file
  assert module._load_site_settings() == settings

  link = tmp_path / 'link_runtime.json'
  link.symlink_to(etc_file)
  module.ETC_SITE_FILE = link
  assert module._load_site_settings() == {}


def test_recovery_reads_env_override(tmp_path, monkeypatch):
  override = tmp_path / 'override.json'
  settings = {
    'backups_root': str(tmp_path / 'env-backups'),
    'oauth_state_root': str(tmp_path / 'env-oauth'),
    'credentials_root': str(tmp_path / 'env-secrets'),
  }
  override.write_text(json.dumps(settings))
  monkeypatch.setenv('AI_MEMORY_RUNTIME_CONFIG', str(override))
  module = _load_recovery_copy(tmp_path, 'recovery_env')
  assert str(module.BACKUPS) == settings['backups_root']
  assert str(module.OAUTH) == settings['oauth_state_root']
  assert str(module.CREDS) == settings['credentials_root']
