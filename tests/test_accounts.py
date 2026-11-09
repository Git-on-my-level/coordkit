"""Capacity observation is file-only, bounded and independent of bus delivery."""
import contextlib
import copy
import io
import json
import os
from unittest.mock import patch

from coordkit import accounts, cli, mcp_server
from coordkit.manifest import ManifestError, load
from tests.support import FakeCase

AT = 1893456000  # 2030-01-01T00:00:00Z


def facts():
    return {'schema_version': 1, 'generated_at': '2030-01-01T00:00:00Z', 'accounts': [
        {'id': 'claude-1', 'windows': [
            {'name': '5h', 'used_ratio': 0.125, 'resets_at': '2030-01-01T05:00:00Z',
             'observed_at': '2029-12-31T23:59:00Z'},
            {'name': 'weekly', 'used_ratio': 1, 'resets_at': None,
             'observed_at': '2029-12-31T21:59:59Z'}]},
        {'id': 'undeclared:claude_statusline:~/.claude-2', 'windows': [
            {'name': 'weekly', 'used_ratio': 0, 'resets_at': '2030-01-07T00:00:00Z',
             'observed_at': '2029-12-31T23:00:00Z'}]}]}


class AccountsTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.facts = self.root/'facts.json'
        self.facts.write_text(json.dumps(facts()))

    def enable(self):
        self.file.write_text(self.file.read_text() + '\n[accounts]\nfacts_file = "facts.json"\n')
        self.manifest = load(self.file)

    def test_manifest_defaults_validation_and_relative_path(self):
        self.assertEqual(accounts.snapshot(self.manifest)['status'], 'disabled')
        with patch('os.open', side_effect=AssertionError('disabled must not read')):
            self.assertFalse(accounts.snapshot(self.manifest)['available'])
        self.enable()
        self.assertEqual(self.manifest.accounts.max_age, '2h')
        self.assertTrue(accounts.snapshot(self.manifest, AT)['available'])
        base = self.file.read_text().split('\n[accounts]')[0]
        for setting in ('max_age = "0s"', 'max_age = 7200', 'facts_file = 4', 'unknown = "x"'):
            self.file.write_text(base + '\n[accounts]\n' + setting)
            with self.subTest(setting=setting), self.assertRaises(ManifestError):
                load(self.file)

    def test_percent_and_independent_observation_and_publication_freshness(self):
        self.enable()
        data = accounts.snapshot(self.manifest, AT)
        first = data['accounts'][0]['windows']
        self.assertEqual([w['used_pct'] for w in first], [12.5, 100])
        self.assertEqual([w['stale'] for w in first], [False, True])
        self.assertEqual(first[0]['observed_at'], '2029-12-31T23:59:00Z')
        self.assertIsNone(first[1]['resets_at'])
        self.manifest.accounts.max_age = '30s'
        self.assertTrue(accounts.snapshot(self.manifest, AT)['accounts'][0]['windows'][0]['stale'])
        self.manifest.accounts.max_age = '2h'
        data = facts(); data['generated_at'] = '2029-12-31T21:00:00Z'
        self.facts.write_text(json.dumps(data))
        self.assertTrue(all(w['stale'] for a in accounts.snapshot(self.manifest, AT)['accounts'] for w in a['windows']))
        data['generated_at'] = '2030-01-02T00:00:00Z'
        self.facts.write_text(json.dumps(data))
        self.assertTrue(accounts.snapshot(self.manifest, AT)['accounts'][0]['windows'][0]['stale'])

    def test_missing_garbled_oversized_and_nonregular_sources_fail_open(self):
        self.enable()
        self.facts.unlink()
        self.assertEqual(accounts.snapshot(self.manifest)['status'], 'unavailable')
        for content in (b'{', b'\xff', b'[]', b'x' * (accounts.MAX_BYTES + 1)):
            self.facts.write_bytes(content)
            self.assertEqual(accounts.snapshot(self.manifest)['status'], 'unavailable')
        self.facts.unlink()
        self.facts.mkdir()
        self.assertFalse(accounts.snapshot(self.manifest)['available'])
        self.facts.rmdir()
        os.mkfifo(self.facts)
        self.assertFalse(accounts.snapshot(self.manifest)['available'], 'FIFO must never wait for a writer')
        self.facts.unlink()
        self.facts.symlink_to(self.facts.name)
        self.assertFalse(accounts.snapshot(self.manifest)['available'], 'symlink loops fail open')
        self.facts.unlink()
        self.facts.write_text(json.dumps(facts()))
        with patch('os.open', side_effect=PermissionError()):
            self.assertFalse(accounts.snapshot(self.manifest)['available'])

    def test_invalid_structure_and_values_are_unavailable_without_partial_rows(self):
        self.enable()
        bad = []
        for value in (True, -0.1, 1.1, float('nan'), float('inf'), None, '0.1'):
            data = facts(); data['accounts'][0]['windows'][0]['used_ratio'] = value; bad.append(data)
        for key, value in (('observed_at', None), ('observed_at', '2030-01-01'), ('resets_at', 'bad')):
            data = facts(); data['accounts'][0]['windows'][0][key] = value; bad.append(data)
        for value in (None, {}, [None], [dict(id='a', windows=[None])]):
            data = facts(); data['accounts'] = value; bad.append(data)
        data = facts(); data['accounts'] *= 2; bad.append(data)
        data = facts(); data['accounts'][0]['windows'] *= 2; bad.append(data)
        data = facts(); data['schema_version'] = 2; bad.append(data)
        data = facts(); data['accounts'] *= accounts.MAX_ACCOUNTS + 1; bad.append(data)
        data = facts(); data['accounts'][0]['windows'] *= accounts.MAX_WINDOWS + 1; bad.append(data)
        for data in bad:
            self.facts.write_text(json.dumps(data))
            result = accounts.snapshot(self.manifest, AT)
            self.assertEqual((result['available'], result['accounts']), (False, []), data)

    def test_join_declared_discovered_and_unknown_accounts(self):
        self.enable()
        data = accounts.snapshot(self.manifest, AT)
        self.assertEqual(accounts.account_windows(data, '.claude-1')[0],
                         dict(name='5h', used_pct=12.5, resets_at='2030-01-01T05:00:00Z', stale=False))
        self.assertEqual(accounts.account_windows(data, '.claude-2')[0]['used_pct'], 0)
        for name in (None, '.claude-3'):
            self.assertEqual(accounts.account_windows(data, name), [])
        duplicate = copy.deepcopy(data['accounts'][1]); duplicate['id'] = 'claude-2'
        duplicate['windows'][0]['used_pct'] = 70
        data['accounts'].append(duplicate)
        self.assertEqual(accounts.account_windows(data, '.claude-2')[0]['used_pct'], 70)

    def test_cli_json_text_and_mcp_paging_do_not_touch_bus_or_external_tools(self):
        self.enable()
        with patch('coordkit.process.run', side_effect=AssertionError('no external calls')), \
                patch('coordkit.bus.read_path', side_effect=AssertionError('no bus reads')), \
                patch('coordkit.bus.append', side_effect=AssertionError('no bus writes')):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(['--manifest', str(self.file), 'accounts', '--json']), 0)
            self.assertEqual(len(json.loads(out.getvalue())['accounts']), 2)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(['--manifest', str(self.file), 'accounts']), 0)
            self.assertIn('12.5%', out.getvalue())
            self.assertEqual(len(out.getvalue().splitlines()), 3, 'one row per account')
            first = mcp_server.call_tool(self.manifest, 'accounts', {'limit': 1})
            self.assertTrue(first['available']); self.assertTrue(first['truncated'])
            last = mcp_server.call_tool(self.manifest, 'accounts', {'cursor': first['next_cursor']})
            self.assertEqual(last['accounts'][0]['id'], facts()['accounts'][1]['id'])
            self.assertIsNone(last['next_cursor'])
            self.facts.write_text('{')
            self.assertEqual(mcp_server.call_tool(self.manifest, 'accounts', {})['status'], 'unavailable')
        self.assertFalse(self.manifest.paths.bus_log.exists())

    def test_agents_status_joins_one_snapshot_and_survives_missing_facts(self):
        self.enable()
        from coordkit import routing
        def identity(manifest, name):
            return {'config_dir': str(self.root/'.claude-1')} if name == 'worker' else {}
        with patch.object(routing, 'roster_identity', side_effect=identity), \
                patch.object(accounts, 'snapshot', wraps=accounts.snapshot) as read:
            result = mcp_server.call_tool(self.manifest, 'agents_status', {})
        self.assertEqual(read.call_count, 1)
        row = next(row for row in result['agents'] if row['name'] == 'worker')
        self.assertEqual(row['account'], '.claude-1')
        self.assertEqual(row['account_windows'][0]['used_pct'], 12.5)
        for content in (None, '{', '[]'):
            if content is None:
                self.facts.unlink()
            else:
                self.facts.write_text(content)
            result = mcp_server.call_tool(self.manifest, 'agents_status', {})
            self.assertTrue(result['agents'])
            self.assertTrue(all(row['account_windows'] == [] for row in result['agents']))
        self.assertFalse(self.manifest.paths.bus_log.exists())
