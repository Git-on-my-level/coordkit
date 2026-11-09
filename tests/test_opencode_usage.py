"""OpenCode usage reads its local SQLite store read-only, scoped by store and workspace."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
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

    def test_non_wal_store_is_still_read_from_a_private_copy(self):
        connection = self.store()
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        self.message(connection, 'ses_repo', 'm1', self.assistant())
        connection.commit(); connection.close()
        live = Path(self.root/'oc'/'opencode.db')
        seen = []
        original = usage._opencode_connect

        def capture(path):
            seen.append(Path(path))
            return original(path)

        # No WAL header exists here, but the store must never be opened in place:
        # OpenCode can switch it to WAL before a lazily opened connection reads it.
        with patch.object(usage, '_opencode_connect', side_effect=capture):
            reading = usage.collect([self.lane()], AT)[0][1]
        self.assertEqual(reading.turns, 1)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], live)
        self.assertNotEqual(seen[0].parent, live.parent)
        self.assertEqual(sorted(path.name for path in live.parent.iterdir()), ['opencode.db'])

    def test_snapshot_copies_a_hot_rollback_journal(self):
        """A non-WAL store mid-commit has a hot -journal; the copy must carry it so
        SQLite rolls the partial pages back in the private directory."""
        live = self.root / 'store.db'
        live.write_bytes(b'db')
        Path(str(live) + '-journal').write_bytes(b'journal')
        copy = self.root / 'snap' / 'opencode.db'
        copy.parent.mkdir()
        self.assertTrue(usage._copy_opencode_store(live, copy))
        self.assertEqual(Path(str(copy) + '-journal').read_bytes(), b'journal')
        self.assertFalse(Path(str(copy) + '-wal').exists())

    def test_hot_rollback_journal_is_recovered_in_the_private_copy(self):
        """Copy a store mid-commit (uncommitted pages spilled to the db file, hot
        -journal present): the snapshot reads the committed value, not the
        partial write and not an unreadable result."""
        import shutil, sqlite3
        src = self.root / 'hot'
        src.mkdir()
        live = src / 'store.db'
        writer = sqlite3.connect(live, isolation_level=None)
        writer.execute('PRAGMA journal_mode=DELETE')
        writer.execute('CREATE TABLE t (v TEXT)')
        writer.execute("INSERT INTO t VALUES ('committed')")
        writer.execute('PRAGMA cache_size=1')
        writer.execute('BEGIN')
        writer.execute("UPDATE t SET v = 'partial'")
        writer.executemany('INSERT INTO t VALUES (?)', [('x' * 2000,)] * 200)  # force a spill
        self.assertTrue(Path(str(live) + '-journal').exists())
        snap = self.root / 'snap2'
        snap.mkdir()
        copy = snap / 'opencode.db'
        shutil.copyfile(live, copy)
        shutil.copyfile(str(live) + '-journal', str(copy) + '-journal')
        writer.execute('ROLLBACK'); writer.close()
        connection = usage._opencode_connect(copy)
        try:
            rows = connection.execute('SELECT v FROM t').fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [('committed',)])

    def test_failed_snapshot_copy_leaves_no_private_directory(self):
        connection = self.store()
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        self.message(connection, 'ses_repo', 'm1', self.assistant())
        connection.commit(); connection.close()
        created = []
        real_mkdtemp = tempfile.mkdtemp

        def recording(*args, **kwargs):
            path = Path(real_mkdtemp(*args, **kwargs))
            created.append(path)
            return str(path)

        with patch.object(usage.tempfile, 'mkdtemp', side_effect=recording), \
                patch.object(usage.shutil, 'copyfile', side_effect=OSError('disk full')):
            reading = usage.collect([self.lane()], AT)[0][1]
        self.assertEqual((reading.turns, reading.unreadable_files), (0, 1))
        self.assertTrue(created)
        for path in created:
            self.assertFalse(path.exists(), f'private snapshot leaked: {path}')

    def test_vanished_wal_is_treated_as_no_wal(self):
        connection = self.store()
        connection.execute('PRAGMA journal_mode = WAL')
        connection.execute('PRAGMA wal_autocheckpoint = 0')
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        self.message(connection, 'ses_repo', 'm1', self.assistant())
        connection.commit()
        live = Path(self.root/'oc'/'opencode.db')
        wal = Path(str(live) + '-wal')
        self.assertTrue(wal.exists())
        real_copy = usage.shutil.copyfile
        vanished = []

        def copy(source, destination):
            if str(source).endswith('-wal') and not vanished:
                vanished.append(True)
                Path(source).unlink()  # a checkpoint folded the WAL into the database
                raise FileNotFoundError(source)
            return real_copy(source, destination)

        try:
            with patch.object(usage.shutil, 'copyfile', side_effect=copy):
                reading = usage.collect([self.lane()], AT)[0][1]
        finally:
            connection.close()
        self.assertTrue(vanished)
        self.assertEqual(reading.unreadable_files, 0)

    def test_concurrent_checkpoint_retries_until_the_source_is_stable(self):
        connection = self.store()
        connection.execute("INSERT INTO session_v2 VALUES ('ses_repo', '/work/repo')")
        self.message(connection, 'ses_repo', 'm1', self.assistant())
        connection.commit(); connection.close()
        live = Path(self.root/'oc'/'opencode.db')
        real_copy = usage.shutil.copyfile
        copies = []

        def copy(source, destination):
            source = str(source)
            copies.append(source)
            real_copy(source, destination)
            if source == str(live) and copies.count(source) == 1:
                # Simulate a checkpoint rewriting the database right after the copy.
                info = live.stat()
                os.utime(live, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))

        with patch.object(usage.shutil, 'copyfile', side_effect=copy):
            reading = usage.collect([self.lane()], AT)[0][1]
        self.assertEqual(reading.turns, 1)
        self.assertGreaterEqual(copies.count(str(live)), 2)

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
