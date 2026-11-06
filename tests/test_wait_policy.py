import contextlib
import io
from coordkit.cli import main
from coordkit.manifest import load, ManifestError
from coordkit.wait_policy import DEFAULT_BUS_KINDS, timeout_minutes, wake_worthy
from tests.support import FakeCase


class WaitPolicyTests(FakeCase):
    def test_defaults_leave_requested_timeout_unchanged(self):
        self.assertEqual((self.manifest.wait.min_timeout_minutes, self.manifest.wait.backoff), (0, 1))
        for requested in (9, 45, 55, 100):
            self.assertEqual(timeout_minutes(self.manifest.wait, requested, 500), requested)

    def test_opt_in_floor_backoff_cap_and_reset(self):
        with self.file.open('a') as f:
            f.write('\n[wait]\nmin_timeout_minutes=30\nbackoff=2\n')
        policy = load(self.file).wait
        self.assertEqual(timeout_minutes(policy, 9, 0), 30)
        self.assertEqual(timeout_minutes(policy, 9, 2), 120)
        self.assertEqual(timeout_minutes(policy, 9, 100000), 1440)
        self.assertEqual(timeout_minutes(policy, 9, 0), 30)
        for value in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                timeout_minutes(policy, value)
        with self.assertRaises(ValueError):
            timeout_minutes(policy, 9, -1)
        self.file.write_text(self.file.read_text().replace('backoff=2', 'backoff=0.5'))
        with self.assertRaises(ManifestError):
            load(self.file)

    def test_shared_kind_rule(self):
        for kind in DEFAULT_BUS_KINDS.split(','):
            self.assertTrue(wake_worthy({'kind': kind}))
        for kind in ('status', 'ack', 'state', 'heartbeat', 'lesson', 'unknown', ''):
            self.assertFalse(wake_worthy({'kind': kind}))
        self.assertTrue(wake_worthy({}, 'all'))
        self.assertTrue(wake_worthy({'kind': 'custom'}, {'custom'}))
        self.assertFalse(wake_worthy({'kind': 'ask'}, {'custom'}))

    def test_cli_helpers_are_read_only_in_shadow(self):
        self.file.write_text('mode="shadow"\n' + self.file.read_text())
        for args, rc, expected in ((['policy', '--timeout', '9'], 0, '9'),
                                   (['wake-worthy', 'status'], 3, 'ignore'),
                                   (['wake-worthy', 'ask'], 0, 'wake-worthy')):
            before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(['--manifest', str(self.file), 'wait', *args]), rc)
            self.assertEqual(out.getvalue().strip(), expected)
            self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
