"""Recovery integrity and unsafe archive rejection using synthetic inputs."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    'recovery',
    Path(__file__).parents[1] / 'scripts/recovery.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


def complete_stage(path):
  path.mkdir()
  (path / 'canonical.dump').write_bytes(b'synthetic-dump')
  (path / 'oauth-state').mkdir()
  (path / 'oauth-state' / 'synthetic').write_bytes(b'encrypted-state-fixture')
  for service, names in [('ai-memory', recovery.MAIN_KEYS),
                         ('ai-memory-oauth', recovery.OAUTH_KEYS)]:
    folder = path / 'credentials' / service
    folder.mkdir(parents=True)
    for name in names:
      (folder / name).write_text('synthetic-' + name)
  return path


def test_complete_snapshot_roundtrip_and_corruption_detection(
    tmp_path, monkeypatch):
  monkeypatch.setattr(
      recovery, 'run',
      lambda *args, **kwargs: SimpleNamespace(stdout='synthetic-commit\n'))
  stage = complete_stage(tmp_path / 'stage')
  archive = tmp_path / 'bundle.tar.gz'
  recovery.write_archive(stage, archive)
  restored = tmp_path / 'restored'
  restored.mkdir()
  recovery.safe_extract(archive, restored)
  recovery.verify_manifest(restored)
  assert (restored / 'credentials/ai-memory-oauth/storage_encryption_key'
         ).stat().st_mode & 0o777 == 0o600
  (restored /
   'credentials/ai-memory-oauth/storage_encryption_key').write_text('corrupted')
  with pytest.raises(ValueError, match='checksums'):
    recovery.verify_manifest(restored)


@pytest.mark.parametrize(
    'kind',
    ['parent', 'absolute', 'symlink', 'hardlink', 'duplicate', 'oversize'])
def test_unsafe_archive_is_rejected_before_any_extraction(tmp_path, kind):
  archive = tmp_path / 'unsafe.tar.gz'
  with tarfile.open(archive, 'w:gz') as output:
    member = tarfile.TarInfo('oauth-state/safe')
    if kind == 'parent':
      member.name = 'oauth-state/../../escape'
    if kind == 'absolute':
      member.name = '/oauth-state/escape'
    if kind in {'symlink', 'hardlink'}:
      member.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
      member.linkname = '/etc/shadow'
    else:
      member.size = 4
    output.addfile(member, io.BytesIO(b'test') if member.isfile() else None)
    if kind == 'duplicate':
      output.addfile(member, io.BytesIO(b'test'))
  restored = tmp_path / 'restored'
  restored.mkdir()
  with pytest.raises(ValueError):
    recovery.safe_extract(archive,
                          restored,
                          max_bytes=3 if kind == 'oversize' else 1024)
  assert list(restored.iterdir()) == []


def test_unexpected_or_missing_files_are_rejected(tmp_path, monkeypatch):
  monkeypatch.setattr(
      recovery, 'run',
      lambda *args, **kwargs: SimpleNamespace(stdout='synthetic-commit\n'))
  stage = complete_stage(tmp_path / 'stage')
  recovery.write_archive(stage, tmp_path / 'archive.tar.gz')
  (stage / 'oauth-state' /
   'manifest.json').write_text('unexpected nested manifest')
  with pytest.raises(ValueError, match='checksums'):
    recovery.verify_manifest(stage)
  (stage / 'oauth-state' / 'manifest.json').unlink()
  (stage / 'canonical.dump').unlink()
  manifest = json.loads((stage / 'manifest.json').read_text())
  manifest['files'].pop('canonical.dump')
  (stage / 'manifest.json').write_text(json.dumps(manifest))
  with pytest.raises(ValueError, match='incomplete'):
    recovery.verify_manifest(stage)


def backup_fixture(tmp_path, monkeypatch, fail_dump=False, fail_copy=False):
  stage = complete_stage(tmp_path / 'inputs')
  backups = tmp_path / 'backups'
  backups.mkdir(mode=0o700)
  monkeypatch.setattr(recovery, 'BACKUPS', backups)
  monkeypatch.setattr(recovery, 'CREDS', stage / 'credentials')
  oauth_root = tmp_path / 'oauth-root'
  recovery.shutil.copytree(stage / 'oauth-state', oauth_root / 'oauth')
  monkeypatch.setattr(recovery, 'OAUTH', oauth_root)
  original_stat = Path.stat

  def owned_by_root(path, *args, **kwargs):
    result = original_stat(path, *args, **kwargs)
    if path == backups:
      fields = list(result)
      fields[4] = 0
      return recovery.os.stat_result(fields)
    return result

  monkeypatch.setattr(Path, 'stat', owned_by_root)
  calls = []

  def fake_run(*args, **kwargs):
    calls.append(args)
    if 'pg_dump' in args:
      kwargs['stdout'].write(b'synthetic-canonical-dump')
      if fail_dump:
        raise RuntimeError('Synthetic dump failure')
    return SimpleNamespace(stdout='synthetic-commit\n')

  monkeypatch.setattr(recovery, 'run', fake_run)
  monkeypatch.setattr(recovery, 'snapshot_oauth', recovery.shutil.copytree)
  if fail_copy:

    def failed_copy(*args):
      raise RuntimeError('Synthetic copy failure')

    monkeypatch.setattr(recovery, 'snapshot_oauth', failed_copy)
  return backups, calls


def test_backup_keeps_gateway_running_and_publishes_complete_snapshot(
    tmp_path, monkeypatch):
  backups, calls = backup_fixture(tmp_path, monkeypatch)
  recovery.backup()
  actions = [command[1] for command in calls if command[0] == 'docker']
  assert actions == ['exec']
  bundle = backups / 'home-recovery-current.tar.gz'
  assert bundle.stat().st_mode & 0o777 == 0o600
  restored = tmp_path / 'restored'
  restored.mkdir()
  recovery.safe_extract(bundle, restored)
  recovery.verify_manifest(restored)
  assert list(backups.iterdir()) == [bundle]


@pytest.mark.parametrize('failure', ['dump', 'copy'])
def test_failed_backup_preserves_snapshot_without_service_changes(
    tmp_path, monkeypatch, failure):
  backups, calls = backup_fixture(tmp_path,
                                  monkeypatch,
                                  fail_dump=failure == 'dump',
                                  fail_copy=failure == 'copy')
  previous = backups / 'home-recovery-current.tar.gz'
  previous.write_bytes(b'previous-good-snapshot')
  with pytest.raises(RuntimeError):
    recovery.backup()
  assert previous.read_bytes() == b'previous-good-snapshot'
  assert all(command[1] == 'exec' for command in calls
             if command[0] == 'docker')
  assert list(backups.iterdir()) == [previous]
