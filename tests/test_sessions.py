import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from coordkit import bus, sessions


def session(root, pid, **fields):
    (root/'sessions').mkdir(parents=True, exist_ok=True)
    record = dict(dict(pid=pid, sessionId=f'sid-{pid}', name='omi-75', nameSource='derived', status='idle'), **fields)
    (root/'sessions'/f'{pid}.json').write_text(json.dumps(record))


class SessionDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        session(self.home/'.claude', 101)
        session(self.home/'.claude-1', 202, tmux='cl2-deploy:@31.%31')
        session(self.home/'.claude-1', 303, tmux='cl2-lead:@12.%12', name='lead', nameSource='set')
        session(self.home/'.claude-2', 404)
        (self.home/'.claude-1'/'sessions'/'bad.json').write_text('{')

    def rows(self, dead=()):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
            return sessions.discover(self.home, is_alive=lambda pid: pid not in dead)

    def test_scans_every_profile_and_skips_dead_and_broken(self):
        self.assertEqual([sessions.key(r) for r in self.rows(dead={404})],
                         ['.claude:101', '.claude-1:202', '.claude-1:303'])

    def test_derived_name_collision_is_reported_not_resolved(self):
        rows = self.rows()
        self.assertEqual(sessions.collisions(rows)['omi-75'], ['.claude:101', '.claude-1:202', '.claude-2:404'])
        self.assertEqual(sessions.resolve(rows, 'omi-75'), (None, 'no_session: omi-75'))

    def test_lane_resolves_by_tmux_session_explicit_name_or_key(self):
        rows = self.rows()
        self.assertEqual(sessions.resolve(rows, 'cl2-deploy')[0]['pid'], 202)
        self.assertEqual(sessions.resolve(rows, 'lead')[0]['pid'], 303)
        self.assertEqual(sessions.resolve(rows, '.claude:101')[0]['session_id'], 'sid-101')

    def test_ambiguous_lane_is_refused(self):
        session(self.home/'.claude-2', 505, tmux='cl2-deploy:@40.%40')
        row, error = sessions.resolve(self.rows(), 'cl2-deploy')
        self.assertIsNone(row)
        self.assertEqual(error, 'ambiguous_session: cl2-deploy -> .claude-1:202, .claude-2:505')


class OrphanedWaitTests(unittest.TestCase):
    def test_orphaned_waiter_exits_so_the_lane_stays_wakeable(self):
        err = io.StringIO()
        with mock.patch.object(bus.os, 'getppid', return_value=1), \
                mock.patch.dict(os.environ, {bus.ORPHAN_OK_ENV: ''}), redirect_stderr(err), \
                self.assertRaises(SystemExit) as stop:
            bus.refuse_orphaned_wait('cl2-deploy')
        self.assertEqual(stop.exception.code, 4)
        self.assertIn('BUS_WAIT orphaned cl2-deploy', err.getvalue())

    def test_attached_or_supervised_waiter_continues(self):
        with mock.patch.object(bus.os, 'getppid', return_value=4242):
            bus.refuse_orphaned_wait('lead')
        with mock.patch.object(bus.os, 'getppid', return_value=1), \
                mock.patch.dict(os.environ, {bus.ORPHAN_OK_ENV: '1'}):
            bus.refuse_orphaned_wait('lead')


if __name__ == '__main__':
    unittest.main()
