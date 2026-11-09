"""Exercise real argv through the strict fake, never a mocked AnxClient success.

Pinned to ANX `cards create`, `note`, `work cadence`, `ask`, and bridge help.
Update the fake contract and these scenarios together when the CLI changes.
"""
import contextlib
import io
import json
import os
from unittest.mock import patch

from coordkit import bus, lifecycle, mirror
from coordkit.process import run
from tests.support import FakeCase


class AnxContractsTests(FakeCase):
    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def anx_calls(self):
        return [c['args'] for c in self.calls() if c['tool'] == 'anx']

    def test_hold_body_request_key_and_resume_past_failed_row(self):
        bus.configure(self.manifest)
        bus.BUS.parent.mkdir(parents=True, exist_ok=True)
        rows = [dict(id='h1', kind='hold', frm='worker', prs=['142'], text='verify evidence', origin='coordkit'),
                dict(id='f1', kind='handoff', frm='worker', to='lead', text='ready for review',
                     subject='card:task', origin='coordkit')]
        bus.BUS.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.respond({'tool': 'anx', 'contains': ['cards', 'create'], 'exit': 2, 'stderr': 'temporarily unavailable'})
        with self.assertRaises(SystemExit):
            self.quiet(bus.append, [])
        cursor = self.manifest.paths.root/'state/mirror.cursor'
        self.assertFalse(cursor.exists())
        self.respond()
        self.quiet(bus.append, [])
        self.assertEqual(cursor.read_text().strip(), '2')
        before = self.anx_calls()
        self.quiet(bus.append, [])
        self.assertEqual(self.anx_calls(), before, 'resuming a completed cursor must make no writes')
        creates = [a for a in before if 'create' in a]
        self.assertEqual(creates[0], creates[1], 'an uncertain hold creation must retain its request key and body')
        args = creates[-1]
        self.assertEqual(args[args.index('--body') + 1], 'verify evidence')
        self.assertEqual(args[args.index('--request-key') + 1], 'ck-sample-hold-142-worker')
        self.assertEqual(args[args.index('--column') + 1], 'blocked')
        self.assertTrue(any('move' in a and 'review' in a for a in before))
        self.assertTrue(any('note' in a and 'handoff f1: ready for review' in a for a in before))

    def test_mirror_other_invocations_pin_resolution_notes_and_promises(self):
        ruling = dict(id='r1', kind='ruling', frm='lead', owner='worker', subject='card:task', text='verify')
        rows = [ruling]
        fixtures = [ruling, dict(id='a1', kind='ack', re='r1'),
                    dict(id='b1', kind='blocker', frm='worker', owner='lead', subject='card:task', text='blocked'),
                    dict(id='h1', kind='hold', frm='worker', prs=['142'], text='verify evidence')]
        for row in fixtures:
            mirror.mirror(self.manifest, row, rows)
        mirror.mirror(self.manifest, dict(kind='unhold', frm='lead', prs=['142']), rows + [fixtures[-1]])
        calls = self.anx_calls()
        self.assertTrue(any(a[-4:] == ['--column', 'blocked', '--request-key', 'ck-sample-hold-142-worker'] for a in calls))
        self.assertTrue(any(a[-5:] == ['cards', 'resolve', 'card:hold-pr-142-worker', '--reason', 'unhold'] for a in calls))
        self.assertTrue(any(a[-2:] == ['--next', '30m'] for a in calls))
        self.assertTrue(any(a[-3:] == ['--evidence', 'bus:a1', '--met'] for a in calls))
        self.assertTrue(any(a[-2:] == ['--evidence', 'bus:b1'] for a in calls))
        self.assertTrue(all(a[:5] == ['--workspace', 'anx', '--as', 'ck-sample-lead', '--json'] for a in calls))

    def test_lifecycle_provisions_board_card_body_and_cadence(self):
        self.file.write_text(self.file.read_text() + '\n[loop.loops.audit]\n')
        self.respond(*[{'tool': 'anx', 'contains': [kind, 'get'], 'exit': 2,
                        'json': {'ok': False, 'error': {'code': 'not_found'}}} for kind in ('boards', 'cards')])
        self.assertEqual(self.quiet(lifecycle.initialize, 'sample', self.file, 'anx', None, 'owner/repo'), 0)
        commands = [a[5:] for a in self.anx_calls()]
        self.assertIn(['boards', 'get', 'ck-sample'], commands)
        self.assertIn(['boards', 'create', '--title', 'ck-sample', '--summary', 'Coordkit sample work'], commands)
        self.assertIn(['cards', 'create', '--board', 'ck-sample', '--handle', 'loop-audit', '--title', 'Loop audit',
                       '--body', 'STOP file present', '--column', 'in_progress'], commands)
        self.assertIn(['work', 'cadence', 'card:loop-audit', '--every', '30m', '--budget', '30m',
                       '--stop-rule', 'STOP file present'], commands)
        self.assertIn(['orient'], commands)

    def test_lifecycle_reads_and_bridge_calls_use_the_same_contract(self):
        self.manifest.loop.loops = {'audit': {}}
        self.manifest.anx.bridge_answers = True
        self.respond({'tool': 'anx', 'contains': ['orient'], 'json': {'ok': True, 'result': {'principal': {'id': 'lead'}}}},
                     {'tool': 'anx', 'contains': ['boards', 'get'], 'json': {'ok': True, 'result': {'board': {'id': 'board'}}}},
                     {'tool': 'agentctl', 'contains': ['config', 'doctor'], 'json': {'ok': True, 'result': {'valid': True}}},
                     {'tool': 'anx', 'contains': ['cards', 'get', 'card:loop-audit'], 'json': {'ok': True, 'result': {
                         'card': {'handle': 'loop-audit', 'board_ref': 'board:ck-sample'}}}},
                     {'tool': 'anx', 'contains': ['context'], 'json': {'ok': True, 'result': {'work': {'cadence': {
                         'every_seconds': 1800, 'progress_budget_seconds': 1800, 'stop_rule': 'STOP file present'}}}}})
        # Only service installation is stubbed. Every ANX command crosses the strict fake.
        with patch.object(lifecycle, 'templates', return_value=[]), patch.object(lifecycle, 'installed_jobs', return_value=[]):
            self.assertEqual(self.quiet(lifecycle.up, self.manifest, True), 0)
        self.manifest.mode = 'shadow'
        self.assertEqual(self.quiet(lifecycle.doctor, self.manifest), 0)
        commands = [a[a.index('--json') + 1:] if '--json' in a else a[4:] for a in self.anx_calls()]
        for command in (['cards', 'list'], ['boards', 'get', 'ck-sample'], ['cards', 'get', 'card:loop-audit'],
                        ['context', 'card:loop-audit'], ['bridge', 'status'], ['bridge', 'install'], ['bridge', 'add']):
            self.assertIn(command, commands)

    def test_fake_rejects_missing_required_flags_and_unknown_flags(self):
        # This negative control prevents a future permissive fake from hiding CLI drift again.
        prefix = [os.environ['ANX'], '--workspace', 'anx', '--as', 'test', '--json']
        cases = [(['cards', 'create'], ['--board', 'ck-sample', '--handle', 'hold-pr-142-worker',
                                     '--title', 'Hold PR 142', '--body', 'verify', '--column', 'blocked']),
                 (['boards', 'create'], ['--title', 'sample', '--summary', 'work']),
                 (['work', 'cadence', 'card:task'], ['--every', '1h', '--budget', '1h', '--stop-rule', 'STOP'])]
        for command, flags in cases:
            self.assertEqual(run(prefix + command + flags).returncode, 0)
            for i in range(0, len(flags), 2):
                result = run(prefix + command + flags[:i] + flags[i+2:])
                with self.subTest(command=command, missing=flags[i]):
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('ANX contract', result.stderr)
            self.assertEqual(run(prefix + command + flags + ['--invented-flag']).returncode, 2)
