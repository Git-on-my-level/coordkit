"""OpenCode usage reads its local SQLite store read-only, scoped by store and workspace."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from unittest.mock import patch

from coordkit import usage
from coordkit.manifest import ManifestError, UsageLane, load
from tests.support import FakeCase

AT = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


def millis(stamp):
    return int(stamp.timestamp() * 1000)


class OpenCodeUsageTests(FakeCase):
    def lane(self, name='worker', cwd='/work/repo', session='', harness='opencode'):
        return UsageLane(name, str(self.root/'oc'), cwd, 'morph-kimik31', 'high',
                         session_id=session, harness=harness)

    def store(self):
        directory = self.root/'oc'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory/'opencode.db'
        connection = sqlite3.connect(path)
        connection.executescript('''
            CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT NOT NULL);
            CREATE TABLE session_message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL,
                time_created INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL);
            CREATE TABLE message (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                time_created INTEGER NOT NULL, data TEXT NOT NULL);
        ''')
        return connection

    def assistant(self, model='morph-kimik31', input=10, output=2, read=100, write=3, at=AT):
        return {'time': {'created': millis(at)},
                'model': {'id': model, 'providerID': 'morph'},
                'tokens': {'input': input, 'output': output, 'reasoning': 4,
                           'cache': {'read': read, 'write': write}}}

    def message(self, connection, session, identifier, data, at=AT, table='session_message'):
        if table == 'message':
            connection.execute('INSERT INTO message VALUES (?, ?, ?, ?)',
                               (identifier, session, millis(at), json.dumps(data)))
            return
        kind = 'assistant' if isinstance(data, dict) and 'tokens' in data else 'user'
        connection.execute('INSERT INTO session_message VALUES (?, ?, ?, ?, ?)',
                           (identifier, session, kind, millis(at), json.dumps(data)))

    def test_counts_window_turns_models_and_context(self):
        connection = self.store()
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        connection.execute("INSERT INTO session_v2 VALUES ('ses_other', '/elsewhere')")
        self.message(connection, 'ses_repo', 'm1', self.assistant(at=AT-timedelta(minutes=30)))
        self.message(connection, 'ses_repo', 'm2', self.assistant(model='morph-kimik31', input=5, output=7, read=50, write=0,
                                                                 at=AT-timedelta(minutes=5)))
        self.message(connection, 'ses_repo', 'old', self.assistant(at=AT-timedelta(hours=2)))
        self.message(connection, 'ses_repo', 'user', {'role': 'user', 'time': {'created': millis(AT)}})
        self.message(connection, 'ses_other', 'other', self.assistant(input=999, at=AT-timedelta(minutes=5)))
        # A nested child session shares the workspace and counts like a subagent.
        connection.execute("INSERT INTO session_v2 VALUES ('ses_child', '/work/repo')")
        self.message(connection, 'ses_child', 'child', self.assistant(model='morph-dsv41flash-1017',
                                                                     input=1, output=1, read=0, write=0,
                                                                     at=AT-timedelta(minutes=4)))
        connection.commit(); connection.close()
        reading = usage.collect([self.lane()], AT)[0][1]
        self.assertEqual(reading.turns, 3)
        self.assertEqual((reading.input_tokens, reading.output_tokens), (16, 10))
        self.assertEqual((reading.cache_read_input_tokens, reading.cache_creation_input_tokens), (150, 3))
        self.assertEqual(reading.latest_model, 'morph-dsv41flash-1017')
        self.assertEqual(set(reading.models), {'morph-kimik31', 'morph-dsv41flash-1017'})
        self.assertEqual(reading.context_now, 1)
        self.assertEqual(reading.unreadable_files, 0)

    def test_v1_message_table_and_session_pin(self):
        connection = self.store()
        connection.execute("INSERT INTO session VALUES ('ses_v1', '/work/repo')")
        data = {'role': 'assistant', 'modelID': 'morph-kimik31', 'providerID': 'morph',
                'time': {'created': millis(AT-timedelta(minutes=10))},
                'tokens': {'input': 40, 'output': 4, 'cache': {'read': 8, 'write': 0}}}
        self.message(connection, 'ses_v1', 'v1', data, table='message')
        connection.commit(); connection.close()
        lane = self.lane(session='ses_v1')
        reading = usage.collect([lane], AT)[0][1]
        self.assertEqual((reading.turns, reading.input_tokens, reading.cache_read_input_tokens), (1, 40, 8))
        self.assertEqual(reading.latest_model, 'morph-kimik31')
        self.assertEqual(usage.opencode_observed_model(lane)[0], 'morph-kimik31')
        self.assertTrue(usage.model_matches('opencode/morph-kimik31', 'morph-kimik31'))
        self.assertTrue(usage.model_matches('morph-kimik31', 'morph-kimik31'))
        self.assertTrue(usage.model_matches('morph/morph-kimik31', 'morph/morph-kimik31'))
        self.assertFalse(usage.model_matches('other/morph-kimik31', 'morph/morph-kimik31'))

    def test_migrated_session_is_counted_once_across_schema_generations(self):
        connection = self.store()
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        connection.execute("INSERT INTO session VALUES ('ses_repo', '/work/repo')")
        connection.execute("INSERT INTO session VALUES ('ses_old', '/work/repo')")
        self.message(connection, 'ses_repo', 'm-v2', self.assistant(input=10, output=1, read=0, write=0))
        legacy = {'role': 'assistant', 'modelID': 'old-model',
                  'time': {'created': millis(AT - timedelta(minutes=5))},
                  'tokens': {'input': 99, 'output': 99, 'cache': {'read': 0, 'write': 0}}}
        # Same id would replace the v2 counters; a copied id would add a second turn.
        self.message(connection, 'ses_repo', 'm-v2', legacy, at=AT - timedelta(minutes=5), table='message')
        self.message(connection, 'ses_repo', 'm-copy', legacy, at=AT - timedelta(minutes=5), table='message')
        old = self.assistant(model='legacy-model', input=4, output=1, read=0, write=0,
                             at=AT - timedelta(minutes=1))
        old['role'] = 'assistant'
        self.message(connection, 'ses_old', 'm-old', old, at=AT - timedelta(minutes=1), table='message')
        connection.commit(); connection.close()
        reading = usage.collect([self.lane()], AT)[0][1]
        self.assertEqual((reading.turns, reading.input_tokens), (2, 14))
        self.assertEqual(reading.context_now, 10)
        self.assertEqual(set(reading.models), {'morph-kimik31', 'legacy-model'})
        pinned = usage.collect([self.lane(session='ses_repo')], AT)[0][1]
        self.assertEqual((pinned.turns, pinned.input_tokens, pinned.latest_model), (1, 10, 'morph-kimik31'))

    def test_shared_workspace_scoping_and_ambiguity(self):
        connection = self.store()
        connection.execute("INSERT INTO session_v2 VALUES ('ses_a', '/work/repo')")
        connection.execute("INSERT INTO session_v2 VALUES ('ses_b', '/work/repo')")
        self.message(connection, 'ses_a', 'a', self.assistant(input=3, output=0, read=0, write=0))
        self.message(connection, 'ses_b', 'b', self.assistant(input=5, output=0, read=0, write=0))
        connection.commit(); connection.close()
        pinned = self.lane('pinned', session='ses_a')
        other = self.lane('other', session='ses_b')
        shared = self.lane('shared')
        results = {lane.name: reading for lane, reading in usage.collect([pinned, other, shared], AT)}
        self.assertEqual(results['pinned'].input_tokens, 3)
        self.assertEqual(results['other'].input_tokens, 5)
        self.assertIsNone(results['shared'])

    def test_wal_database_is_read_from_a_snapshot_without_sidecars(self):
        connection = self.store()
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA wal_autocheckpoint = 0')
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        self.message(connection, 'ses_repo', 'm1', self.assistant())
        connection.commit()
        path = self.root/'oc'/'opencode.db'
        # Capture the live WAL state, then restore just the db + wal with no -shm.
        db_bytes = path.read_bytes()
        wal_bytes = (self.root/'oc'/'opencode.db-wal').read_bytes()
        connection.close()
        path.write_bytes(db_bytes)
        (self.root/'oc'/'opencode.db-wal').write_bytes(wal_bytes)
        before = sorted(item.name for item in (self.root/'oc').iterdir())
        self.assertIn('opencode.db-wal', before)
        self.assertNotIn('opencode.db-shm', before)
        reading = usage.collect([self.lane()], AT)[0][1]
        self.assertEqual(reading.turns, 1)
        self.assertEqual(reading.input_tokens, 10)
        self.assertEqual(sorted(item.name for item in (self.root/'oc').iterdir()), before)

    def test_store_is_read_only_and_damaged_sources_fail_open(self):
        connection = self.store()
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        self.message(connection, 'ses_repo', 'm1', self.assistant())
        connection.commit(); connection.close()
        path = self.root/'oc'/'opencode.db'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(usage.collect([self.lane()], AT)[0][1].turns, 1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertFalse((self.root/'oc'/'opencode.db-wal').exists())
        path.write_bytes(b'not a database')
        reading = usage.collect([self.lane()], AT)[0][1]
        self.assertEqual((reading.turns, reading.unreadable_files), (0, 1))
        reading = usage.collect([self.lane_with_config('missing')], AT)[0][1]
        self.assertEqual(reading.turns, 0)

    def lane_with_config(self, config):
        return UsageLane('worker', str(self.root/config), '/work/repo', 'morph-kimik31', 'high',
                         harness='opencode')

    def test_agents_status_uses_opencode_store_for_lane_harness_and_shared_scope(self):
        with self.file.open('a') as stream:
            stream.write('\n[[lanes]]\nname="pinned"\nconfig_dir="oc"\ncwd="repo"\nmodel="morph-kimik31"\n'
                         'effort="high"\nharness="opencode"\nsession_id="ses_a"\n'
                         '\n[[lanes]]\nname="shared"\nconfig_dir="oc"\ncwd="repo"\nmodel="morph-kimik31"\n'
                         'effort="high"\nharness="opencode"\n')
        manifest = load(self.file)
        cwd = manifest.lanes[0].cwd
        connection = self.store()
        connection.execute('INSERT INTO session_v2 VALUES (?, ?)', ('ses_a', cwd))
        connection.execute('INSERT INTO session_v2 VALUES (?, ?)', ('ses_b', cwd))
        self.message(connection, 'ses_a', 'a', self.assistant(model='morph-pinned'))
        self.message(connection, 'ses_b', 'b', self.assistant(model='morph-shared'))
        connection.commit(); connection.close()
        from coordkit import mcp_server, routing

        def identity(unused, name):
            # A Claude-shaped roster path must not override the lane harness.
            return {'transcript_path': str(self.root/'ghost'/'missing.jsonl')} if name == 'pinned' else {}

        with patch.object(routing, 'roster_identity', side_effect=identity):
            result = mcp_server.call_tool(manifest, 'agents_status', {})
        rows = {row['name']: row for row in result['agents']}
        self.assertEqual(rows['pinned']['model'], 'morph-pinned')
        # The unpinned lane shares its store and cwd, so no model is attributable.
        self.assertIsNone(rows['shared']['model'])

    def test_manifest_harness_and_session_inference(self):
        with self.file.open('a') as stream:
            stream.write('\n[[lanes]]\nname="oc"\nconfig_dir="oc"\ncwd="repo"\nmodel="morph-kimik31"\n'
                         'effort="high"\nharness="opencode"\n')
        manifest = load(self.file)
        self.assertEqual(manifest.lanes[0].harness, 'opencode')
        self.assertEqual(usage.lane_harness(manifest.lanes[0]), 'opencode')
        inferred = UsageLane('x', 'oc', 'repo', 'm', 'h', session_id='ses_abc')
        self.assertEqual(usage.lane_harness(inferred), 'opencode')
        base = self.file.read_text()
        for harness in ('codex', 3):
            with self.subTest(harness=harness):
                self.file.write_text(base.replace('harness="opencode"', f'harness="{harness}"' if isinstance(harness, str) else 'harness=3'))
                with self.assertRaises(ManifestError):
                    load(self.file)
