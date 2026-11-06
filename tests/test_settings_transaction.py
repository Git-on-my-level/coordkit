import fcntl
import os
import threading
from unittest.mock import patch
from coordkit import state
from coordkit.errors import ManifestError
from tests.support import FakeCase


class SettingsTransactionTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.target = self.root/'settings.json'
        state.write_json(self.target, {'theme': 'original'})

    def test_final_hash_check_catches_edit_during_temp_flush(self):
        actual_fsync = os.fsync
        fired = False
        calls = []
        def fsync(fd):
            nonlocal fired
            actual_fsync(fd)
            if not fired:
                fired = True
                state.write_json(self.target, {'theme': 'concurrent'})
        def transform(current):
            calls.append(current.copy())
            return dict(current, managed=True)
        with patch.object(state.os, 'fsync', side_effect=fsync):
            state.update_json(self.target, transform)
        self.assertEqual(len(calls), 2)
        self.assertEqual(state.read_json(self.target), {'theme': 'concurrent', 'managed': True})

    def test_second_hash_mismatch_aborts_without_replacing_latest_edit(self):
        revision = 0
        def change_again(before, after):
            nonlocal revision
            revision += 1
            state.write_json(self.target, {'revision': revision})
        with self.assertRaisesRegex(ManifestError, 'settings_update_conflict: changed twice'):
            state.update_json(self.target, lambda current: dict(current, managed=True), before_replace=change_again)
        self.assertEqual(state.read_json(self.target), {'revision': 2})
        self.assertFalse(list(self.root.glob('settings.json.*.tmp')))
        with (self.root/'settings.json.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_exclusive_sibling_lock_is_held_at_rename(self):
        actual_replace = os.replace
        checks = []
        def replace(source, target):
            if target == self.target:
                with (self.root/'settings.json.lock').open('a') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                checks.append(True)
            actual_replace(source, target)
        with patch.object(state.os, 'replace', side_effect=replace):
            state.update_json(self.target, lambda current: dict(current, managed=True))
        self.assertEqual(checks, [True])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_two_writers_computing_from_same_snapshot_keep_both_changes(self):
        first_ready, second_computed, release_first = (threading.Event() for _ in range(3))
        failures = []
        def prepare(before, after):
            first_ready.set()
            if not release_first.wait(5):
                raise TimeoutError('test release not signalled')
        def second(current):
            second_computed.set()
            return dict(current, beta=True)
        def run(transform, **kwargs):
            try:
                state.update_json(self.target, transform, **kwargs)
            except BaseException as exc:
                failures.append(exc)
        first = threading.Thread(target=run, args=(lambda current: dict(current, alpha=True),), kwargs={'before_replace': prepare}, daemon=True)
        other = threading.Thread(target=run, args=(second,), daemon=True)
        try:
            first.start()
            self.assertTrue(first_ready.wait(5))
            other.start()
            self.assertTrue(second_computed.wait(5))
        finally:
            release_first.set()
            first.join(5)
            if other.ident is not None:
                other.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(other.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual(state.read_json(self.target), {'theme': 'original', 'alpha': True, 'beta': True})
