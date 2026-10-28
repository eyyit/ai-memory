"""Exercise publication decisions in disposable repositories."""

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'scripts/check-push.py'
spec = importlib.util.spec_from_file_location('push_guard', MODULE)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PushGuardTests(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.old = guard.ROOT
    guard.ROOT = self.root
    self.addCleanup(setattr, guard, 'ROOT', self.old)
    self.git('init', '-b', 'main')
    self.git('config', 'user.name', 'Test Contributor')
    self.git('config', 'user.email', 'test@example.invalid')
    self.write('README.md', 'Safe example project\n')
    self.commit()

  def git(self, *args):
    return subprocess.check_output(
      ['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL,
    )

  def write(self, name, text):
    path = self.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

  def commit(self):
    self.git('add', '-A')
    self.git('commit', '-qm', 'Fixture')

  def test_clean_history_passes(self):
    guard.scan_policy(['HEAD'], [])

  def test_untracked_scanner_ignore_rejected(self):
    self.write('.gitleaksignore', '# even an untracked file needs review')
    with self.assertRaises(ValueError):
      guard.reject_ignore_overrides()

  def test_deleted_private_file_still_blocks(self):
    self.write('private/runtime.json', '{}')
    self.commit()
    self.git('rm', 'private/runtime.json')
    self.commit()
    with self.assertRaises(ValueError):
      guard.scan_policy(['HEAD'], [])

  def test_force_added_ignored_file_blocks(self):
    self.write('.gitignore', 'private/\n')
    self.commit()
    self.write('private/site.json', '{}')
    self.git('add', '-f', 'private/site.json')
    self.git('commit', '-qm', 'Force-added fixture')
    with self.assertRaises(ValueError):
      guard.scan_policy(['HEAD'], [])

  def test_private_identifier_blocks(self):
    self.write('README.md', 'https://private.example.invalid')
    self.commit()
    with self.assertRaises(ValueError):
      guard.scan_policy(['HEAD'], [re.compile(r'private\.example\.invalid')])

  def test_every_tip_is_checked(self):
    clean = self.git('rev-parse', 'HEAD').decode().strip()
    self.write('backup.dump', 'fixture')
    self.commit()
    with self.assertRaises(ValueError):
      guard.scan_policy([clean, 'HEAD'], [])

  def test_same_blob_under_private_path_blocks(self):
    self.write('private/copy.md', 'Safe example project\n')
    self.commit()
    with self.assertRaises(ValueError):
      guard.scan_policy(['HEAD'], [])

  def test_path_policy(self):
    for name in ['backup.tar.gz', 'exports/chats.json', '.env', '.env.prod',
                 'nested/secrets/key.txt', 'home.sqlite', '.gitleaksignore']:
      with self.subTest(name=name):
        self.assertTrue(guard.forbidden(name))
    for name in ['.env.example', 'src/service.py', 'tests/test_service.py']:
      with self.subTest(name=name):
        self.assertFalse(guard.forbidden(name))

  @unittest.skipUnless(os.environ.get('GITLEAKS_TEST_BIN'),
                       'Set GITLEAKS_TEST_BIN for real scanner tests')
  def test_deleted_secret_detected_by_real_scanner(self):
    token = 'ghp_' + 'aB3dE5fG7hI9jK1lM3nO5pQ7rS9tU1vW3xY5'
    self.write('settings.txt', 'token=' + token + '\n')
    self.commit()
    self.git('rm', 'settings.txt')
    self.commit()
    result = subprocess.run([
      os.environ['GITLEAKS_TEST_BIN'], 'git', str(self.root),
      '--log-opts=--all', '--redact=100', '--no-banner',
    ], capture_output=True, text=True)
    self.assertEqual(result.returncode, 1)
    self.assertNotIn(token, result.stdout + result.stderr)


if __name__ == '__main__':
  unittest.main()
