import os
from unittest.mock import patch
from coordkit import deliver, hooks
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class PermissionTests(FakeCase):
    def test_atomic_replacement_preserves_existing_mode(self):
        target = self.root/'settings.json'
        for mode in (0o600, 0o640, 0o644):
            target.write_text('{}\n'); target.chmod(mode)
            write_json(target, {'updated': True})
            self.assertEqual(target.stat().st_mode & 0o777, mode)
            self.assertEqual(read_json(target), {'updated': True})

    def test_new_settings_private_even_with_permissive_umask(self):
        target = self.root/'new/settings.json'
        previous = os.umask(0)
        try:
            deliver.install(self.manifest, target)
        finally:
            os.umask(previous)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_hook_lifecycle_preserves_private_settings(self):
        target = self.root/'settings.json'
        target.write_text('{}\n'); target.chmod(0o600)
        with patch('coordkit.paths.install_root', return_value=self.root/'old'):
            deliver.install(self.manifest, target)
        with patch('coordkit.paths.install_root', return_value=self.root/'new'):
            hooks.reconcile(self.manifest)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        deliver.uninstall(self.manifest, target)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_failed_serialization_preserves_target_and_cleans_temp(self):
        target = self.root/'settings.json'
        write_json(target, {'original': True})
        with self.assertRaises(TypeError):
            write_json(target, {'invalid': object()})
        self.assertEqual(read_json(target), {'original': True})
        self.assertFalse(list(self.root.glob('settings.json.*.tmp')))
