import copy
import contextlib
from unittest.mock import patch
from coordkit import deliver, hooks
from coordkit.manifest import ManifestError
from coordkit.state import read_json, write_json
from tests.support import FakeCase


class HookConcurrencyTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.target = self.root/'settings.json'
        write_json(self.target, {'theme': 'original'})
        with patch('coordkit.paths.install_root', return_value=self.root/'old'):
            deliver.install(self.manifest, self.target)
        self.receipt = deliver.receipt_path(self.manifest, self.target)

    def race_during_receipt(self, edit, error=None):
        actual_write = hooks.write_json
        fired = False
        def write(path, doc):
            nonlocal fired
            actual_write(path, doc)
            if path == self.receipt and doc.get('pending_after') and not fired:
                fired = True
                current = read_json(self.target)
                edit(current)
                actual_write(self.target, current)
        with patch('coordkit.paths.install_root', return_value=self.root/'new'):
            expected_error = self.assertRaisesRegex(ManifestError, error) if error else contextlib.nullcontext()
            with patch.object(hooks, 'write_json', side_effect=write), expected_error:
                hooks.reconcile(self.manifest)
        self.assertTrue(fired)

    def test_unrelated_concurrent_edit_is_remerged_once(self):
        self.race_during_receipt(lambda doc: doc.update(theme='concurrent'))
        self.assertEqual(read_json(self.target)['theme'], 'concurrent')
        self.assertNotIn(str(self.root/'old'), self.target.read_text())
        self.assertIn(str(self.root/'new'), self.target.read_text())

    def test_owned_concurrent_edit_is_never_overwritten(self):
        def edit(doc):
            doc['hooks']['PostToolUse'][0]['hooks'][0]['command'] = 'user replacement'
        self.race_during_receipt(edit, error='hook_rollback_conflict')
        snapshot = self.target.read_bytes()
        with self.assertRaisesRegex(ManifestError, 'hook_rollback_conflict'):
            hooks.reconcile(self.manifest)
        self.assertEqual(self.target.read_bytes(), snapshot)

    def test_pending_recovery_rereads_before_settings_write(self):
        record = read_json(self.receipt)
        record['pending_after'] = copy.deepcopy(record['after'])
        with patch('coordkit.paths.install_root', return_value=self.root/'new'):
            command = deliver.command(self.manifest)
        for groups in record['pending_after']['hooks'].values():
            for group in groups:
                group['hooks'][0]['command'] = command
        write_json(self.receipt, record)
        actual_reverse = hooks.reverse
        def race(*args):
            result = actual_reverse(*args)
            current = read_json(self.target); current['theme'] = 'recovery edit'
            write_json(self.target, current)
            return result
        with patch('coordkit.paths.install_root', return_value=self.root/'new'), patch.object(hooks, 'reverse', side_effect=race) as reverse:
            hooks.reconcile(self.manifest)
        self.assertEqual(reverse.call_count, 2)
        self.assertEqual(read_json(self.target)['theme'], 'recovery edit')
        self.assertIn(str(self.root/'new'), self.target.read_text())

    def test_install_receipt_window_edit_remerges_and_keeps_fresh_rollback(self):
        target = self.root/'new-settings.json'
        write_json(target, {'theme': 'original'})
        receipt = deliver.receipt_path(self.manifest, target)
        actual_write = deliver.write_json
        fired = False
        def race(path, document):
            nonlocal fired
            actual_write(path, document)
            if path == receipt and not fired:
                fired = True
                actual_write(target, {'theme': 'install edit', 'hooks': {'Stop': [
                    {'hooks': [{'type': 'command', 'command': 'user-hook'}]}]}})
        with patch.object(deliver, 'write_json', side_effect=race):
            deliver.install(self.manifest, target)
        self.assertTrue(fired)
        self.assertEqual(read_json(target)['theme'], 'install edit')
        self.assertEqual(read_json(receipt)['before']['theme'], 'install edit')
        deliver.uninstall(self.manifest, target)
        self.assertEqual(read_json(target)['hooks']['Stop'][0]['hooks'][0]['command'], 'user-hook')

    def test_uninstall_remerges_edit_between_read_and_lock(self):
        actual_reverse = hooks.reverse
        fired = False
        def race(*args):
            nonlocal fired
            result = actual_reverse(*args)
            if not fired:
                fired = True
                current = read_json(self.target)
                current['theme'] = 'uninstall edit'
                current['hooks']['Stop'] = [{'hooks': [{'type': 'command', 'command': 'user-hook'}]}]
                write_json(self.target, current)
            return result
        with patch.object(hooks, 'reverse', side_effect=race) as reverse:
            deliver.uninstall(self.manifest, self.target)
        self.assertEqual(reverse.call_count, 2)
        self.assertTrue(read_json(self.receipt)['restored'])
        self.assertEqual(read_json(self.target)['theme'], 'uninstall edit')
        self.assertEqual(read_json(self.target)['hooks']['Stop'][0]['hooks'][0]['command'], 'user-hook')

    def test_uninstall_concurrent_owned_edit_aborts_without_marking_restored(self):
        actual_reverse = hooks.reverse
        fired = False
        def race(*args):
            nonlocal fired
            result = actual_reverse(*args)
            if not fired:
                fired = True
                current = read_json(self.target)
                current['hooks']['PostToolUse'][0]['hooks'][0]['command'] = 'concurrent owned edit'
                write_json(self.target, current)
            return result
        with patch.object(hooks, 'reverse', side_effect=race), self.assertRaisesRegex(ManifestError, 'hook_rollback_conflict'):
            deliver.uninstall(self.manifest, self.target)
        self.assertEqual(read_json(self.target)['hooks']['PostToolUse'][0]['hooks'][0]['command'], 'concurrent owned edit')
        self.assertFalse(read_json(self.receipt)['restored'])


class LegacyHookInstallConcurrencyTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.target = self.root/'settings.json'
        self.backup = self.root/'settings.backup.json'
        write_json(self.target, {'theme': 'original'})
        self.backup.write_bytes(self.target.read_bytes())

    def test_edit_after_snapshot_aborts_instead_of_overwriting_backup_changes(self):
        actual_transform = hooks.transformation
        def race(*args):
            result = actual_transform(*args)
            write_json(self.target, {'theme': 'concurrent'})
            return result
        with patch.object(hooks, 'transformation', side_effect=race):
            with self.assertRaisesRegex(ManifestError, 'hooks_changed_since_backup'):
                hooks.install(self.manifest, self.target, self.backup, fresh_ok=True)
        self.assertEqual(read_json(self.target), {'theme': 'concurrent'})
        self.assertFalse(hooks.receipt_path(self.manifest).exists())

    def test_receipt_window_edit_aborts_with_independent_recovery_evidence(self):
        actual_write = hooks.write_json
        receipt = hooks.receipt_path(self.manifest)
        def race(path, document):
            actual_write(path, document)
            if path == receipt:
                actual_write(self.target, {'theme': 'concurrent'})
        with patch.object(hooks, 'write_json', side_effect=race):
            with self.assertRaisesRegex(ManifestError, 'hooks_changed_since_backup'):
                hooks.install(self.manifest, self.target, self.backup, fresh_ok=True)
        self.assertEqual(read_json(self.target), {'theme': 'concurrent'})
        self.assertEqual(read_json(receipt)['before'], {'theme': 'original'})
        self.assertFalse(read_json(receipt)['restored'])
