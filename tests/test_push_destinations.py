"""The backup exception is narrow and refuses unverified destinations."""

import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'scripts/global-hooks.py'
spec = importlib.util.spec_from_file_location('global_hooks', MODULE)
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)


class DestinationTests(unittest.TestCase):
  @patch.object(hooks, 'git', side_effect=subprocess.CalledProcessError(1, []))
  def test_initial_reference_hook_uses_git_dir(self, git):
    with tempfile.TemporaryDirectory() as folder:
      with patch.dict(os.environ, {'GIT_DIR': folder}):
        self.assertEqual(hooks.common_directory('reference-transaction'),
                         Path(folder).resolve())

  @patch.object(hooks, 'git', side_effect=subprocess.CalledProcessError(1, []))
  def test_invalid_pre_push_repository_still_fails(self, git):
    with patch.dict(os.environ, {'GIT_DIR': '/unused/fixture'}):
      with self.assertRaises(subprocess.CalledProcessError):
        hooks.common_directory('pre-push')

  def setUp(self):
    self.approved = {
      'checkout': str(Path('/etc').resolve()),
      'repository': 'example/config-backup',
      'repository_id': 123,
    }
    self.metadata = {
      'id': 123, 'full_name': 'example/config-backup',
      'private': True, 'visibility': 'private',
    }
    self.config = {
      'private_backup_exceptions': [self.approved],
      'github_metadata_token_file': '/unused/fixture',
    }

  def test_supported_urls_normalize(self):
    for value in ['git@github.com:example/config-backup.git',
                  'ssh://git@github.com/example/config-backup.git',
                  'https://github.com/example/config-backup.git']:
      self.assertEqual(hooks.github_destination(value),
                       'example/config-backup')

  def test_unapproved_transports_rejected(self):
    for value in ['https://github.com.attacker.invalid/example/config-backup',
                  'http://github.com/example/config-backup',
                  'https://token@github.com/example/config-backup',
                  'ssh://git@other.invalid/example/config-backup',
                  'git@github.com:example/config-backup.git/extra']:
      with self.subTest(value=value), self.assertRaises(ValueError):
        hooks.github_destination(value)

  def test_private_metadata_accepted(self):
    hooks.validate_private_metadata(self.metadata, self.approved)

  def test_public_metadata_rejected(self):
    self.metadata.update(private=False, visibility='public')
    with self.assertRaises(ValueError):
      hooks.validate_private_metadata(self.metadata, self.approved)

  def test_changed_identity_rejected(self):
    for field, value in [('id', 999), ('full_name', 'other/config-backup')]:
      data = dict(self.metadata, **{field: value})
      with self.subTest(field=field), self.assertRaises(ValueError):
        hooks.validate_private_metadata(data, self.approved)

  def test_unset_identity_and_missing_privacy_rejected(self):
    with self.assertRaises(ValueError):
      hooks.validate_private_metadata({}, self.approved)
    self.approved['repository_id'] = None
    with self.assertRaises(ValueError):
      hooks.validate_private_metadata(self.metadata, self.approved)

  @patch.object(hooks, 'verify_private')
  def test_exact_destination_skips_only_after_verification(self, verify):
    self.assertTrue(hooks.backup_exception(
      Path('/etc'), Path('/etc/.git'),
      'git@github.com:example/config-backup.git', self.config,
    ))
    verify.assert_called_once()

  @patch.object(hooks, 'verify_private')
  def test_linked_worktree_uses_common_checkout(self, verify):
    self.assertTrue(hooks.backup_exception(
      Path('/another-worktree'), Path('/etc/.git'),
      'git@github.com:example/config-backup.git', self.config,
    ))
    verify.assert_called_once()

  @patch.object(hooks, 'verify_private')
  def test_wrong_destination_never_skips(self, verify):
    with self.assertRaises(ValueError):
      hooks.backup_exception(Path('/etc'), Path('/etc/.git'),
                             'git@github.com:other/public.git', self.config)
    verify.assert_not_called()

  @patch.object(hooks, 'verify_private')
  def test_another_checkout_not_exempt(self, verify):
    self.assertFalse(hooks.backup_exception(
      Path('/unrelated'), Path('/unrelated/.git'),
      'git@github.com:example/config-backup.git', self.config,
    ))
    verify.assert_not_called()

  @patch.object(hooks, 'verify_private', side_effect=OSError('Unavailable'))
  def test_visibility_failure_blocks(self, verify):
    with self.assertRaises(OSError):
      hooks.backup_exception(Path('/etc'), Path('/etc/.git'),
                             'git@github.com:example/config-backup.git',
                             self.config)


if __name__ == '__main__':
  unittest.main()
