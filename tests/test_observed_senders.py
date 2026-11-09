"""Foreign writers need no coordkit dispatch hook to join the local roster."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from coordkit import addressed, bus, mcp_server, routing
from coordkit.manifest import UsageLane
from coordkit.state import name_key, read_json, write_json
from tests.test_addressed import AddressedCase


class ObservedSenderTests(AddressedCase):
    def register(self):
        return read_json(routing.home(self.manifest)/'register.json', {})

    def roster(self, at=None):
        return {row['name']: row for row in mcp_server.agent_rows(
            self.manifest, self.rows(), {}, time.time() if at is None else at)['agents']}

    def test_direct_foreign_append_registers_with_post_recency(self):
        self.incumbent_row('foreign', 'external-worker', 'all', 'msg', ago=10)
        self.assertEqual(self.register(), {})
        row = self.roster()['external-worker']
        self.assertEqual(row['status'], 'listening')
        self.assertIn('posted', row['why'])
        self.assertTrue(row['listening'])
        self.assertEqual(self.register()['external-worker']['registered_by'], 'observed')
        self.assertEqual(routing.registry(self.manifest)['external-worker']['source'], 'observed')
        self.assertEqual(addressed.target_kind(self.manifest, 'external-worker'), 'name')
        self.assertEqual((routing.home(self.manifest)/'register.json').stat().st_mode & 0o777, 0o600)

    def test_observation_window_is_separate_from_liveness_ttl(self):
        self.incumbent_row('idle', 'idle-worker', 'all', 'msg', ago=3600)
        self.incumbent_row('old', 'old-worker', 'all', 'msg', ago=86401)
        self.incumbent_row('future', 'future-worker', 'all', 'msg', ago=-100)
        rows = self.roster(at=self.now)
        self.assertEqual(rows['idle-worker']['status'], 'idle')
        self.assertFalse(rows['idle-worker']['listening'])
        self.assertNotIn('old-worker', self.register())
        self.assertNotIn('future-worker', self.register())
        self.assertEqual(self.roster(at=self.now+86401)['idle-worker']['status'], 'idle')

    def test_activity_ttl_is_configurable_and_waiter_evidence_wins(self):
        self.manifest.addressed.activity_ttl = '5s'
        self.incumbent_row('foreign', 'external-worker', 'all', 'msg', ago=10)
        self.assertEqual(self.roster()['external-worker']['status'], 'idle')
        cursor = self.manifest.paths.bus_log.parent/'state/cursors'/f'{name_key("external-worker")}.wait'
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text('0')
        with routing.locked(cursor.with_suffix('.wait.lock')):
            row = self.roster()['external-worker']
            self.assertEqual(row['status'], 'listening')
            self.assertEqual(row['why'], 'armed bus wait lock')

    def test_internal_service_and_invalid_senders_do_not_register(self):
        for i, name in enumerate(('lead', 'coordkit', 'all', 'service-agent',
                                  'coordkit-report', self.manifest.mcp.identity, '../bad')):
            self.incumbent_row(str(i), name, 'all', 'msg', ago=10)
        self.roster()
        self.assertEqual(self.register(), {})

    def test_relay_connector_copy_and_question_rows_do_not_register(self):
        for i, extra in enumerate(({'origin': 'relay:peer'}, {'_relay': {'seq': 1}},
                                   {'origin': 'mcp'}, {'copy_of': 'original'}, {'question': 'q1'})):
            name = 'synthetic-' + str(i)
            self.incumbent_row(name, name, 'all', 'msg', ago=10, **extra)
        self.roster()
        self.assertEqual(self.register(), {})
        for i in range(5):
            self.assertIsNone(addressed.target_kind(self.manifest, 'synthetic-' + str(i)))
            self.assertFalse(addressed.listening(self.manifest, 'synthetic-' + str(i))[0])

    def test_relay_row_does_not_block_genuine_local_observation(self):
        self.incumbent_row('remote', 'mixed-worker', 'all', 'msg', ago=20, origin='relay:peer')
        self.incumbent_row('local', 'mixed-worker', 'all', 'msg', ago=10)
        self.assertEqual(self.roster()['mixed-worker']['status'], 'listening')
        self.assertEqual(routing.registry(self.manifest)['mixed-worker']['source'], 'observed')

    def test_named_lane_identity_is_safe_and_reader_environment_is_ignored(self):
        self.manifest.lanes.append(UsageLane('lane-worker', str(self.root/'account'),
                                            '/lane-cwd', 'opus', 'high', session_id='lane-session'))
        for name in ('lane-worker', 'unknown-worker'):
            self.incumbent_row(name, name, 'all', 'msg', ago=10,
                               identity={'session_id': 'untrusted'}, session_id='untrusted')
        with patch.dict(os.environ, CLAUDE_CONFIG_DIR='/reader-account', CLAUDE_CODE_SESSION_ID='reader-session'):
            rows = self.roster()
        self.assertEqual(rows['lane-worker']['session_id'], 'lane-session')
        self.assertEqual(rows['lane-worker']['config_dir'], str(self.root/'account'))
        self.assertIsNone(rows['unknown-worker']['session_id'])
        self.assertIsNone(rows['unknown-worker']['config_dir'])
        self.assertEqual(set(self.register()['unknown-worker']['identity']), {'refreshed_at'})

    def test_repeated_reads_do_not_rewrite_or_probe_identity(self):
        self.incumbent_row('foreign', 'external-worker', 'all', 'msg', ago=10)
        self.roster()
        path = routing.home(self.manifest)/'register.json'
        original = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(routing, '_store_identities', wraps=routing._store_identities) as store, \
                patch.object(routing, 'probe_identity') as probe:
            self.roster()
            store.assert_not_called()
            probe.assert_not_called()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)

    def test_first_sightings_are_batched_into_one_atomic_replacement(self):
        from coordkit import state
        for i in range(20):
            self.incumbent_row(str(i), 'external-' + str(i), 'all', 'msg', ago=10)
        with patch.object(state, 'update_json', wraps=state.update_json) as update:
            self.roster()
            update.assert_called_once()
        self.assertEqual(len(self.register()), 20)

    def test_busy_registration_lock_does_not_block_roster_read(self):
        self.incumbent_row('foreign', 'external-worker', 'all', 'msg', ago=10)
        # Hold the actual writer lock until the read returns. A blocking observer
        # would stall here until the timeout releases the lock, failing the test.
        with ThreadPoolExecutor(max_workers=1) as pool:
            with routing.locked(routing.home(self.manifest)/'register.lock'):
                rows = pool.submit(self.roster).result(timeout=2)
                self.assertIn('external-worker', rows)
                self.assertEqual(self.register(), {})
        self.assertEqual(self.roster()['external-worker']['status'], 'listening')

    def test_observed_identity_is_collected_outside_writer_lock_without_probes(self):
        self.manifest.lanes.append(UsageLane('lane-worker', str(self.root/'account'),
                                            '/lane-cwd', 'opus', 'high', session_id='lane-session'))
        self.incumbent_row('foreign', 'lane-worker', 'all', 'msg', ago=10)
        available = []
        original = routing.identity_sources
        def collect(*args, **kwargs):
            with routing.locked(routing.home(self.manifest)/'register.lock', blocking=False) as lock:
                available.append(lock is not None)
            return original(*args, **kwargs)
        with patch.object(routing, 'identity_sources', side_effect=collect), \
                patch.object(routing, 'probe_identity') as probe:
            routing.observe_senders(self.manifest, self.rows(), self.now)
        self.assertEqual(available, [True], 'identity file reads must not hold the writer lock')
        probe.assert_not_called()
        self.assertEqual(self.register()['lane-worker']['identity']['session_id'], 'lane-session')

    def test_high_cardinality_roster_bounds_first_sightings_and_reuses_loaded_rows(self):
        template = self.incumbent_row('template', 'external', 'all', 'msg', ago=10)
        rows = [dict(template, id=str(i), frm=f'a-external-{i:05}') for i in range(5000)]
        with patch.object(bus, 'read_path', return_value=rows) as read, \
                patch.object(routing, 'identity_sources', wraps=routing.identity_sources) as identity, \
                patch.object(routing, '_store_identities', wraps=routing._store_identities) as store, \
                patch.object(routing, 'probe_identity') as probe:
            mcp_server.read_call(self.manifest, 'agents_status', {'limit': 1})
        read.assert_called_once()
        store.assert_called_once()
        probe.assert_not_called()
        # First sightings have bounded identity work even when the roster page
        # is tiny; the extra lookup projects the requested roster entry.
        self.assertLessEqual(identity.call_count, routing.OBSERVATION_BATCH_LIMIT + 1)
        expected = {f'a-external-{i:05}' for i in range(4950, 5000)}
        self.assertEqual(set(self.register()), expected)
        routing.observe_senders(self.manifest, rows, self.now)
        self.assertEqual(len(self.register()), 100, 'later reads can register another bounded batch')

    def test_observation_scan_stops_at_tail_bound_even_without_new_names(self):
        visits = []
        class MeasuredRow(dict):
            def get(self, key, default=None):
                if key == 'frm':
                    visits.append(self['id'])
                return super().get(key, default)
        template = self.incumbent_row('template', 'external', 'all', 'msg', ago=10)
        # Every tail row is ineligible: the candidate cap alone cannot stop a
        # full-log scan, and the eligible row just outside the tail must be ignored.
        rows = [MeasuredRow(template, id='outside-tail')]
        rows.extend(MeasuredRow(template, id=str(i), origin='mcp')
                    for i in range(routing.OBSERVATION_ROW_LIMIT))
        for _ in range(2):
            visits.clear()
            routing.observe_senders(self.manifest, rows, self.now)
            self.assertEqual(len(visits), routing.OBSERVATION_ROW_LIMIT)
            self.assertNotIn('outside-tail', visits)
            self.assertEqual(self.register(), {})

    def test_explicit_registration_during_identity_collection_is_preserved(self):
        self.incumbent_row('foreign', 'external-worker', 'all', 'msg', ago=10)
        def collect(*args, **kwargs):
            routing.write_register(self.manifest, 'external-worker', 'wait', {'session_id': 'explicit'})
            return {'session_id': 'observed'}
        with patch.object(routing, 'identity_sources', side_effect=collect), \
                patch.object(routing, '_store_identities', wraps=routing._store_identities) as store:
            routing.observe_senders(self.manifest, self.rows(), self.now)
        store.assert_called_once()  # Only the concurrent explicit writer stores.
        entry = self.register()['external-worker']
        self.assertEqual(entry['registered_by'], 'wait')
        self.assertEqual(entry['identity']['session_id'], 'explicit')

    def test_removed_manifest_cache_cannot_be_promoted_by_old_or_new_rows(self):
        for via in ('post', 'hook', 'observed'):
            name = 'declared-' + via
            self.manifest.addressed.agents[name] = {'fallback': 'lead-a'}
            self.incumbent_row(name, name, 'all', 'msg', ago=20)
            if via == 'post':
                routing.write_register(self.manifest, name, 'post', env={})
            elif via == 'hook':
                routing.refresh_identity(self.manifest, name)
            self.roster()
            self.assertIn(name, self.register())
            del self.manifest.addressed.agents[name]
            self.incumbent_row('new-' + name, name, 'all', 'msg', ago=10)
            self.roster()
            self.assertIsNone(addressed.target_kind(self.manifest, name))
            self.assertNotIn('registered_by', self.register()[name])

    def test_observation_preserves_existing_registration_and_manifest_policy(self):
        routing.write_register(self.manifest, 'runtime-worker', 'wait', {'session_id': 'real'})
        before = self.register()['runtime-worker']
        self.incumbent_row('runtime', 'runtime-worker', 'all', 'msg', ago=10)
        self.incumbent_row('declared', 'lead-a', 'all', 'msg', ago=10)
        self.roster()
        self.assertEqual(self.register()['runtime-worker'], before)
        self.assertEqual(routing.registry(self.manifest)['lead-a']['source'], 'manifest')
        self.assertNotIn('registered_by', self.register()['lead-a'])

    def test_concurrent_observation_and_explicit_registration_preserve_all_names(self):
        for i in range(8):
            self.incumbent_row(str(i), 'external-' + str(i), 'all', 'msg', ago=10)
        rows = self.rows()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(routing.observe_senders, self.manifest, rows, self.now) for _ in range(3)]
            futures.append(pool.submit(routing.write_register, self.manifest, 'explicit-worker', 'wait', {}))
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(self.register()['explicit-worker']['registered_by'], 'wait')
        # Observers may skip a contended writer; a later read retries safely.
        routing.observe_senders(self.manifest, rows, self.now)
        self.assertEqual(set(self.register()), {'external-' + str(i) for i in range(8)} | {'explicit-worker'})

    def test_optional_observation_write_failure_keeps_roster_available(self):
        self.incumbent_row('foreign', 'external-worker', 'all', 'msg', ago=10)
        with patch.object(routing, '_store_identities', side_effect=OSError('read-only state')):
            with self.assertLogs('coordkit.routing', level='WARNING'):
                self.assertIn('external-worker', self.roster())
