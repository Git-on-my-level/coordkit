import contextlib
import datetime as dt
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from coordkit import addressed, bus, closure, incumbent, reconcile
from coordkit.manifest import ManifestError, duration, load
from tests.support import FakeCase

AT = dt.datetime(2026, 11, 10, 3, 44, 54, tzinfo=dt.timezone.utc).timestamp()
FIXTURE = Path(__file__).with_name('fixtures') / 'ruling_closure_rows.json'


def row(rid, ts, **fields):
    return {'id': rid, 'ts': ts, 'topic': 'general', 'text': 'work', **fields}


class DerivedClosureTests(FakeCase):
    def fixture(self):
        return json.loads(FIXTURE.read_text())

    def test_synthetic_rows_demonstrate_the_defect_and_new_derivation(self):
        rows = self.fixture()
        baseline = incumbent.open_rulings(rows)
        self.assertEqual({r['id'] for r in baseline},
                         {'r-001', 'r-003', 'r-004', 'r-008', 'r-013', 'r-005', 'r-006', 'r-007'})

        # BUS_CONFIG is intentionally absent: this tests detached rows, not a
        # manifest, registry or bus path.
        view = closure.derive(rows, AT, default_fallback='lead-b')
        self.assertEqual([r['id'] for r in view['open']], ['r-008'])
        self.assertEqual({r['id'] for r in view['orphaned']},
                         {'r-001', 'r-004', 'r-009', 'r-005', 'r-007', 'r-012'})
        self.assertEqual(
            {(r['id'], r['closure_reason'], r['closed_by']) for r in view['closed']},
            {('r-003', 'superseded', 'r-004'), ('r-013', 'owner_reply', 'r-014'),
             ('r-006', 'superseded', 'r-007')})
        # The reply edge closes r-013 without any completion-word heuristic.
        self.assertEqual(next(r for r in view['closed'] if r['id'] == 'r-003')['shared_prs'], ['4101'])
        # r-006/r-007 share a noisy but mechanically valid PR mention.
        self.assertEqual(next(r for r in view['closed'] if r['id'] == 'r-006')['shared_prs'], ['4102'])

        stale = closure.latest_states(rows, AT)
        observed = stale[('worker-h', 'job-alpha')]
        self.assertEqual((observed['state'], observed['effective_state']), ('RUNNING', 'STALE'))
        self.assertTrue(observed['stale'])

    def test_owner_reply_any_kind_closes_but_bystander_copies_and_early_rows_do_not(self):
        ruling = row('r', '2026-11-09T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker')
        bystander = row('other', '2026-11-09T01:00:00Z', frm='other', to='lead', kind='answer', re='r')
        copied = row('copy', '2026-11-09T02:00:00Z', frm='worker', to='lead', kind='fyi', re='r', copy_of='x')
        self.assertEqual([r['id'] for r in closure.derive([ruling, bystander, copied], AT)['open']], ['r'])
        for kind in ('msg', 'fyi', 'ask', 'state', 'fail', 'answer'):
            reply = row('reply-' + kind, '2026-11-09T03:00:00Z', frm='WORKER', to='lead', kind=kind, re='r')
            found = closure.derive([ruling, bystander, copied, reply], AT)['closed']
            self.assertEqual((found[0]['closure_reason'], found[0]['closed_by']), ('owner_reply', reply['id']))
        self.assertTrue(closure.derive([reply, ruling], AT)['open'])

    def test_legacy_ack_and_forced_ack_close_without_a_forced_field(self):
        ruling = row('r', '2026-11-09T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker')
        owner_ack = row('a', '2026-11-09T01:00:00Z', frm='worker', to='lead', kind='ack', re='r')
        forced_ack = row('f', '2026-11-09T02:00:00Z', frm='lead', to='worker', kind='ack', re='r',
                         text='ack [forced; owner worker]')
        # Legacy behavior: any later ack closes, including a forced ack from a non-owner.
        for ack in (owner_ack, forced_ack):
            found = closure.derive([ruling, ack], AT)['closed']
            self.assertEqual((found[0]['closure_reason'], found[0]['closed_by']), ('acknowledged', ack['id']))
            self.assertNotIn('forced', found[0])
        bystander_ack = row('b', '2026-11-09T03:00:00Z', frm='other', to='lead', kind='ack', re='r')
        self.assertEqual(closure.derive([ruling, bystander_ack], AT)['closed'][0]['closure_reason'], 'acknowledged')

    def test_snapshot_reply_closes_and_unlinked_completion_does_not(self):
        rows = {r['id']: r for r in self.fixture()}
        # r-013 is closed by the owner's later fyi carrying --re.
        closed = closure.derive([rows['r-013'], rows['r-014']], AT)['closed']
        self.assertEqual((closed[0]['id'], closed[0]['closure_reason'], closed[0]['closed_by']),
                         ('r-013', 'owner_reply', 'r-014'))
        # r-001's owner reports completion in a handoff without --re; that is not closure.
        view = closure.derive([rows['r-001'], rows['r-002']], AT)
        self.assertNotIn('r-001', [r['id'] for r in view['closed']])
        self.assertIn('r-001', [r['id'] for r in view['orphaned']])

    def test_supersession_requires_sender_owner_and_shared_pr(self):
        old = row('old', '2026-11-09T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker', text='HOLD #100')
        fields = dict(ts='2026-11-09T01:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker', text='GO #100')
        later = row('new', **fields)
        found = closure.derive([old, later], AT)
        self.assertEqual([r['id'] for r in found['open']], ['new'])
        self.assertEqual(found['closed'][0]['closed_by'], 'new')
        for changed in ({'frm': 'other'}, {'owner': 'other'}, {'text': 'GO #101'}):
            candidate = dict(later, **changed)
            self.assertEqual(len(closure.derive([old, candidate], AT)['open']), 2, changed)
        self.assertEqual([r['id'] for r in closure.derive([old, dict(later, kind='decision')], AT)['open']], ['old'])

    def test_orphan_reason_fallback_and_activity_revival(self):
        ruling = row('r', '2026-11-01T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker')
        last = row('last', '2026-11-02T00:00:00Z', frm='worker', to='lead', kind='fyi')
        view = closure.derive([ruling, last], AT, fallbacks={'worker': 'backup'}, default_fallback='lead-b')
        self.assertEqual(view['open'], [])
        self.assertEqual((view['orphaned'][0]['orphan_reason'], view['orphaned'][0]['fallback']), ('silent', 'backup'))

        # A retired owner is orphaned even while active; retirement redirect wins.
        retired = closure.derive([ruling, row('live', '2026-11-10T00:00:00Z', frm='worker', to='lead', kind='msg')],
                                 AT, retired={'worker': {'owner': 'replacement'}},
                                 fallbacks={'worker': 'backup'})
        self.assertEqual(retired['orphaned'][0]['orphan_reason'], 'retired')
        self.assertEqual(retired['orphaned'][0]['fallback'], 'replacement')

        # Unknown ownership evidence is not silently treated as live forever.
        unknown = closure.derive([ruling], AT)['orphaned'][0]
        self.assertEqual(unknown['orphan_reason'], 'unknown')
        revived = closure.derive([ruling, row('new-post', '2026-11-10T00:00:00Z', frm='worker', to='lead', kind='msg')], AT)
        self.assertEqual([r['id'] for r in revived['open']], ['r'])

    def test_retired_fallback_follows_redirects_and_avoids_cycles(self):
        ruling = row('r', '2026-11-01T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker')
        # A retirement redirect that is itself retired follows its own redirect.
        view = closure.derive([ruling], AT,
                              retired={'worker': {'owner': 'backup'}, 'backup': {'owner': 'lead'}},
                              fallbacks={'worker': 'backup'}, default_fallback='lead')
        self.assertEqual(view['orphaned'][0]['fallback'], 'lead')
        # A declared fallback that is itself retired follows its own redirect.
        view = closure.derive([ruling], AT, retired={'backup': {'owner': 'lead'}},
                              fallbacks={'worker': 'backup'}, default_fallback='lead')
        self.assertEqual(view['orphaned'][0]['fallback'], 'lead')
        # A cycle never loops and falls back to the coordinator, then lead.
        view = closure.derive([ruling], AT,
                              retired={'worker': {'owner': 'backup'}, 'backup': {'owner': 'worker'}},
                              fallbacks={'worker': 'backup'}, default_fallback='coordinator')
        self.assertEqual(view['orphaned'][0]['fallback'], 'coordinator')
        view = closure.derive([ruling], AT,
                              retired={'worker': {'owner': 'backup'}, 'backup': {'owner': 'worker'},
                                       'coordinator': {'owner': 'backup'}},
                              fallbacks={'worker': 'backup'}, default_fallback='coordinator')
        self.assertEqual(view['orphaned'][0]['fallback'], 'lead')

    def test_supersession_precedes_silence_and_unknown_precedes_open(self):
        rows = {r['id']: r for r in self.fixture()}
        activity = row('act', '2026-10-22T00:00:00Z', frm='worker-b', to='lead', kind='fyi')
        view = closure.derive([rows['r-003'], rows['r-004'], activity], AT)
        self.assertEqual([(r['id'], r['closure_reason'], r['closed_by']) for r in view['closed']],
                         [('r-003', 'superseded', 'r-004')])
        self.assertEqual([(r['id'], r['orphan_reason']) for r in view['orphaned']],
                         [('r-004', 'silent')])
        view = closure.derive([rows[i] for i in ('r-001', 'r-002', 'r-005', 'r-008', 'r-009', 'r-016')], AT)
        self.assertEqual([r['id'] for r in view['open']], ['r-008'])
        self.assertEqual({(r['id'], r['orphan_reason']) for r in view['orphaned']},
                         {('r-001', 'silent'), ('r-005', 'unknown'), ('r-009', 'silent')})

    def test_exact_horizon_boundary_and_malformed_time(self):
        horizon = 72 * 3600
        ruling = row('r', '2026-11-01T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker')
        last = row('last', '2026-11-02T00:00:00Z', frm='worker', to='lead', kind='fyi')
        at = closure.timestamp(last) + horizon
        self.assertEqual(closure.derive([ruling, last], at, inactive_after=horizon)['orphaned'][0]['orphan_reason'],
                         'silent')
        self.assertEqual([r['id'] for r in closure.derive([ruling, last], at - 1, inactive_after=horizon)['open']],
                         ['r'])
        # Neither a malformed obligation timestamp nor malformed activity is evidence.
        bad = dict(ruling, ts='not-a-time')
        bad_activity = dict(last, ts='also-bad')
        self.assertEqual([r['id'] for r in closure.derive([bad, bad_activity], AT, inactive_after=horizon)['open']],
                         ['r'])

    def test_ownerless_blocker_stays_open_and_is_closable(self):
        blocker = row('b', '2026-11-01T00:00:00Z', frm='worker', to='lead', kind='blocker', needs='decide')
        self.assertEqual([r['id'] for r in closure.derive([blocker], AT)['open']], ['b'])
        self.assertEqual(closure.derive([blocker], AT)['orphaned'], [])
        answer = row('a', '2026-11-02T00:00:00Z', frm='other', to='lead', kind='answer', re='b')
        self.assertEqual(closure.derive([blocker, answer], AT)['closed'][0]['closure_reason'], 'answered')

    def test_owner_reply_excludes_escalation_and_auto_received(self):
        ruling = row('r', '2026-11-09T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker')
        for field in ('copy_of', 'escalation', 'auto_received'):
            value = True if field == 'auto_received' else 'x'
            reply = row('x', '2026-11-09T01:00:00Z', frm='worker', to='lead', kind='fyi', re='r',
                        **{field: value})
            self.assertEqual([r['id'] for r in closure.derive([ruling, reply], AT)['open']], ['r'], field)
        # Rule 4 deliberately has no copy/escalation exclusion, only auto_received.
        blocker = row('b', '2026-11-09T00:00:00Z', frm='worker', to='lead', kind='blocker', owner='lead')
        for field in ('copy_of', 'escalation'):
            reply = row('x', '2026-11-09T01:00:00Z', frm='other', to='lead', kind='decision', re='b',
                        **{field: 'x'})
            self.assertEqual(closure.derive([blocker, reply], AT)['closed'][0]['closure_reason'], 'answered')
        auto = row('y', '2026-11-09T02:00:00Z', frm='other', to='lead', kind='decision', re='b',
                   auto_received=True)
        self.assertEqual([r['id'] for r in closure.derive([blocker, auto], AT)['open']], ['b'])

    def test_blockers_keep_decision_answer_contract_and_also_orphan(self):
        blocker = row('b', '2026-11-09T00:00:00Z', frm='worker', to='lead', kind='blocker', owner='lead', needs='decide')
        answer = row('a', '2026-11-09T01:00:00Z', frm='lead', to='worker', kind='answer', re='b')
        self.assertEqual(closure.derive([blocker, answer], AT)['closed'][0]['closure_reason'], 'answered')
        reply = dict(answer, kind='fyi')
        self.assertEqual([r['id'] for r in closure.derive([blocker, reply], AT)['open']], ['b'])
        old = dict(blocker, ts='2026-11-01T00:00:00Z')
        self.assertEqual(closure.derive([old], AT, default_fallback='lead-b')['orphaned'][0]['fallback'], 'lead-b')

    def test_configurable_horizon_controls_orphans_and_running_staleness(self):
        old = row('r', '2026-11-09T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker')
        self.assertEqual(len(closure.derive([old], AT, inactive_after=30 * 86400)['open']), 1)
        self.assertEqual(len(closure.derive([old], AT, inactive_after=60)['orphaned']), 1)
        running = row('s', '2026-11-09T00:00:00Z', frm='worker', to='lead', kind='state', state='RUNNING', subject='job')
        self.assertFalse(closure.latest_states([running], AT, inactive_after=30 * 86400)[('worker', 'job')]['stale'])
        self.assertTrue(closure.latest_states([running], AT, inactive_after=60)[('worker', 'job')]['stale'])

    def test_snapshot_measurement_partitions_the_synthetic_fixture(self):
        rows = self.fixture()
        ids = {r['id'] for r in rows if r.get('kind') == 'ruling' and r.get('owner')}
        at = max(closure.timestamp(r) for r in rows if closure.timestamp(r) is not None)
        view = closure.derive(rows, at, inactive_after=duration('72h'),
                              retired={}, fallbacks={}, default_fallback='lead')
        counts = {group: sum(r.get('kind') == 'ruling' and r.get('id') in ids
                             for r in view[group])
                  for group in ('open', 'orphaned', 'closed')}
        self.assertEqual(counts, {'open': 1, 'orphaned': 4, 'closed': 3})
        self.assertEqual(sum(counts.values()), len(ids))
        by_id = {r['id']: (group, r) for group in ('open', 'orphaned', 'closed')
                 for r in view[group] if r.get('kind') == 'ruling'}
        self.assertEqual(by_id['r-008'][0], 'open')
        self.assertEqual((by_id['r-001'][0], by_id['r-001'][1]['orphan_reason']),
                         ('orphaned', 'silent'))
        self.assertEqual((by_id['r-003'][0], by_id['r-003'][1]['closure_reason']),
                         ('closed', 'superseded'))
        self.assertEqual((by_id['r-013'][0], by_id['r-013'][1]['closure_reason']),
                         ('closed', 'owner_reply'))

    def test_manifest_default_and_validation(self):
        self.assertEqual(duration(self.manifest.obligations.inactive_after), 72 * 3600)
        self.file.write_text(self.file.read_text().replace('coordinator = "coordinator"',
                                                           'coordinator = "coordinator"\ninactive_after = "0s"'))
        with self.assertRaisesRegex(ManifestError, 'inactive_after'):
            load(self.file)

    def test_cli_exposes_separate_orphans_and_stale_state(self):
        rows = [row('r', '2026-11-01T00:00:00Z', frm='lead', to='worker', kind='ruling', owner='worker'),
                row('s', '2026-11-01T00:00:00Z', frm='worker', to='lead', kind='state', state='RUNNING', subject='job')]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        bus.configure(self.manifest)
        with patch.object(bus, 'now', return_value=dt.datetime.fromtimestamp(AT, dt.timezone.utc)):
            self.assertEqual(json.loads(self.invoke('open-rulings', '--json')[0]), [])
            orphan = json.loads(self.invoke('open-rulings', '--orphaned', '--json')[0])[0]
            self.assertEqual(orphan['orphan_reason'], 'silent')
            state = json.loads(self.invoke('state', '--json')[0])[0]
        self.assertEqual((state['state'], state['effective_state']), ('RUNNING', 'STALE'))

    def test_bus_open_helpers_partition_orphans(self):
        rows = self.fixture()
        with patch.object(bus, 'CONFIG', None):
            self.assertEqual([r['id'] for r in bus.open_rulings(rows, AT)], ['r-008'])
            self.assertEqual({r['id'] for r in bus.open_rulings(rows, AT, orphaned=True)},
                             {'r-001', 'r-004', 'r-005', 'r-007'})
            self.assertEqual([r['id'] for r in bus.open_blockers(rows, AT)], [])
            self.assertEqual([r['id'] for r in bus.open_blockers(rows, AT, orphaned=True)],
                             ['r-009', 'r-012'])

    def test_cli_open_and_orphan_blockers_and_min_age(self):
        by_id = {r['id']: r for r in self.fixture()}
        recent = row('lead-b-live', '2026-11-10T02:00:00Z', frm='lead-b', to='lead', kind='fyi')
        selected = [by_id['r-013'], by_id['r-014'], by_id['r-009'], by_id['r-012'], recent]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in selected))
        bus.configure(self.manifest)
        with patch.object(bus, 'now', return_value=dt.datetime.fromtimestamp(AT, dt.timezone.utc)), \
                patch('coordkit.closure.time.time', return_value=AT):
            open_blockers = json.loads(self.invoke('open-blockers', '--json')[0])
            orphan_blockers = json.loads(self.invoke('open-blockers', '--orphaned', '--json')[0])
            self.assertEqual([r['id'] for r in open_blockers], ['r-012'])
            self.assertEqual([r['id'] for r in orphan_blockers], ['r-009'])
            self.assertEqual(json.loads(self.invoke('open-rulings', '--json')[0]), [])
            self.assertEqual(json.loads(self.invoke('open-rulings', '--min-age', '100000', '--json')[0]), [])
            self.assertNotIn('r-013', [r['id'] for r in open_blockers + orphan_blockers])

    def test_cli_state_marks_stale_running_and_honors_horizon_override(self):
        by_id = {r['id']: r for r in self.fixture()}
        live = row('worker-i-live', '2026-11-10T02:00:00Z', frm='worker-i', to='lead', kind='fyi')
        selected = [by_id['r-010'], by_id['r-011'], live]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in selected))
        bus.configure(self.manifest)
        with patch.object(bus, 'now', return_value=dt.datetime.fromtimestamp(AT, dt.timezone.utc)):
            states = {r['subject']: r for r in json.loads(self.invoke('state', '--json')[0])}
        self.assertEqual((states['job-alpha']['state'],
                          states['job-alpha']['effective_state']), ('RUNNING', 'STALE'))
        self.assertEqual((states['job-beta']['state'],
                          states['job-beta']['effective_state']), ('RUNNING', 'RUNNING'))
        self.assertFalse(states['job-beta']['stale'])
        self.manifest.obligations.inactive_after = '30d'
        bus.configure(self.manifest)
        with patch.object(bus, 'now', return_value=dt.datetime.fromtimestamp(AT, dt.timezone.utc)):
            states = {r['subject']: r for r in json.loads(self.invoke('state', '--json')[0])}
        self.assertEqual(states['job-alpha']['effective_state'], 'RUNNING')
        self.assertFalse(states['job-alpha']['stale'])

    def test_cli_state_full_shows_effective_state_when_it_differs(self):
        by_id = {r['id']: r for r in self.fixture()}
        live = row('worker-i-live', '2026-11-10T02:00:00Z', frm='worker-i', to='lead', kind='fyi')
        selected = [by_id['r-010'], by_id['r-011'], live]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in selected))
        bus.configure(self.manifest)
        with patch.object(bus, 'now', return_value=dt.datetime.fromtimestamp(AT, dt.timezone.utc)):
            out = self.invoke('state', '--full')[0]
        self.assertIn('effective_state=STALE', out)
        self.assertNotIn('effective_state=RUNNING', out)

    def test_latest_states_later_done_supersedes_running(self):
        running = row('s1', '2026-11-01T00:00:00Z', frm='worker', to='lead', kind='state',
                      state='RUNNING', subject='job')
        done = row('s2', '2026-11-02T00:00:00Z', frm='worker', to='lead', kind='state',
                   state='DONE', subject='job')
        observed = closure.latest_states([running, done], AT)[('worker', 'job')]
        self.assertEqual((observed['state'], observed['effective_state'], observed['stale']),
                         ('DONE', 'DONE', False))

    def invoke(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            bus.main(list(args), self.manifest)
        return out.getvalue(), ''

    def reconcile_once(self, dry_run=False):
        with patch('coordkit.reconcile.report', return_value={}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return reconcile.reconcile(self.manifest, dry_run, AT)

    def write_bus(self, rows):
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in rows))

    def orphan_notices(self):
        return [r for r in bus.read() if r.get('topic') == 'coordkit-orphan']

    def orphan_fixture(self, count=50, recipients=3):
        self.manifest.addressed.agents = {
            **{f'worker-{i}': {'fallback': f'backup-{i}'} for i in range(recipients)},
            **{f'backup-{i}': {} for i in range(recipients)}}
        return [row(f'r-{i:03}', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead',
                    to=f'worker-{i % recipients}', kind='blocker' if i % 2 else 'ruling',
                    owner=f'worker-{i % recipients}', text=f'Subject {i}\nsecond line') for i in range(count)]

    def test_fifty_orphans_make_three_bounded_batches_and_unchanged_rerun_is_silent(self):
        self.write_bus(self.orphan_fixture())
        self.reconcile_once()
        notices = self.orphan_notices()
        self.assertEqual(len(notices), 3)
        self.assertEqual({r['to'] for r in notices}, {'backup-0', 'backup-1', 'backup-2'})
        for notice in notices:
            text = Path(notice['spill_ref']).read_text().strip() if notice.get('spill_ref') else notice['text']
            lines = text.splitlines()
            self.assertEqual(len([line for line in lines if line.startswith('- ')]), 10)
            self.assertRegex(text, r'[67] more\.')
            self.assertIn('bus open-rulings --orphaned', text)
            self.assertIn('bus open-blockers --orphaned', text)
            self.assertRegex(lines[1], r'- (ruling|blocker) r-\d+: owner worker-\d; Subject \d+ second line')
        state_path = self.manifest.paths.root / 'reconcile/state.json'
        state = json.loads(state_path.read_text())
        self.assertEqual(len(state['orphan_notified']), 50)
        # Batch receipts outlive the rolling action retention.
        state['fired'] = {}
        state_path.write_text(json.dumps(state))
        self.reconcile_once()
        self.assertEqual(self.orphan_notices(), notices)

    def test_new_orphan_supersedes_only_its_recipient_even_outside_displayed_lines(self):
        from coordkit import questions
        rows = self.orphan_fixture()
        self.write_bus(rows)
        self.reconcile_once()
        original = {r['to']: r for r in self.orphan_notices()}
        rows.append(row('r-999', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead',
                        to='worker-1', kind='ruling', owner='worker-1', text='New work'))
        self.write_bus([*rows, *bus.read()[50:]])
        self.reconcile_once()
        notices = self.orphan_notices()
        self.assertEqual(len(notices), 4)
        updated = notices[-1]
        self.assertEqual(updated['to'], 'backup-1')
        self.assertNotEqual(updated['id'], original['backup-1']['id'])
        self.assertEqual(updated['key'], original['backup-1']['key'])
        records = questions.load_all(self.manifest)
        self.assertEqual(records[original['backup-1']['id']]['closed_by'], 'superseded')
        self.assertEqual({r['qid'] for r in records.values() if r['state'] == 'open'},
                         {original['backup-0']['id'], updated['id'], original['backup-2']['id']})
        self.reconcile_once()
        self.assertEqual(self.orphan_notices(), notices)

    def test_orphan_cap_drains_across_passes_without_receipting_deferred_rows(self):
        self.manifest.reconciler.orphan_ask_cap = 1
        self.write_bus(self.orphan_fixture())
        state_path = self.manifest.paths.root / 'reconcile/state.json'
        digest_path = self.manifest.paths.root / 'state/addressed/lead-digest.ndjson'
        self.reconcile_once(dry_run=True)
        self.assertFalse(digest_path.exists())
        self.reconcile_once()
        self.assertEqual(len(self.orphan_notices()), 1)
        state = json.loads(state_path.read_text())
        self.assertEqual(len(state['orphan_notified']), 17)
        self.assertEqual(state['orphan_recipient_cursor'], 'backup-0')
        self.assertIn('33 awaiting fallback', json.loads(digest_path.read_text())['text'])
        for expected in (2, 3, 3):
            self.reconcile_once()
            self.assertEqual(len(self.orphan_notices()), expected)
        self.assertEqual(len(json.loads(state_path.read_text())['orphan_notified']), 50)
        self.assertEqual(digest_path.read_text(), '')

    def test_resolved_orphans_drop_the_stale_backlog_snapshot(self):
        self.manifest.reconciler.orphan_ask_cap = 0
        ruling = row('r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead',
                     to='worker', kind='ruling', owner='worker')
        self.write_bus([ruling])
        digest_path = self.manifest.paths.root / 'state/addressed/lead-digest.ndjson'
        self.reconcile_once()
        self.assertIn('1 awaiting fallback', digest_path.read_text())
        addressed.to_lead_digest(self.manifest, 'finding', 'keep me', at=AT)
        self.write_bus([ruling, row('ack', '2026-11-01T00:00:00Z', origin='coordkit', frm='worker',
                                    to='lead', kind='ack', re='r')])
        queued = digest_path.read_text()
        self.reconcile_once(dry_run=True)
        self.assertEqual(digest_path.read_text(), queued)
        self.reconcile_once()
        cleared = digest_path.read_text()
        self.assertIn('keep me', cleared)
        self.assertNotIn('orphan-backlog', cleared)
        self.assertNotIn('awaiting fallback', cleared)
        self.reconcile_once()
        self.assertEqual(digest_path.read_text(), cleared)

    def test_reconcile_without_orphans_does_not_create_a_digest(self):
        self.write_bus([])
        self.reconcile_once()
        self.assertFalse((self.manifest.paths.root / 'state/addressed/lead-digest.ndjson').exists())

    def test_failed_orphan_attempts_are_capped_and_remain_retryable(self):
        from coordkit.process import ToolError
        self.manifest.reconciler.orphan_ask_cap = 2
        self.write_bus(self.orphan_fixture(count=10, recipients=5))
        original = bus.main
        def deliver(argv, manifest):
            if argv[argv.index('--to') + 1] in ('backup-0', 'backup-1'):
                raise ToolError('bus', 6, 'failed', 'transport')
            return original(argv, manifest)
        with patch('coordkit.bus.main', side_effect=deliver) as post:
            self.reconcile_once()
            self.reconcile_once()
        self.assertEqual(post.call_count, 4)
        state = json.loads((self.manifest.paths.root / 'reconcile/state.json').read_text())
        self.assertEqual(set(state['orphan_notified']), {'r-002', 'r-007', 'r-003', 'r-008'})

    def test_orphan_cursor_survives_repeated_aborts_after_failed_attempts(self):
        from coordkit.process import ToolError
        self.manifest.reconciler.orphan_ask_cap = 2
        self.write_bus(self.orphan_fixture(count=10, recipients=5))
        state_path = self.manifest.paths.root / 'reconcile/state.json'
        attempted, post_ids = [], {}

        def fail(argv, manifest):
            rid = argv[argv.index('--key') + 1].removeprefix('orphan-batch:')
            post_id = argv[argv.index('--id') + 1]
            self.assertEqual(post_ids.setdefault(rid, post_id), post_id)
            attempted.append(rid)
            if len(attempted) % 2 == 0:
                raise RuntimeError('pass aborted during delivery')
            raise ToolError('bus', 6, 'failed', 'transport')

        with patch('coordkit.bus.main', side_effect=fail):
            for expected in (['backup-0', 'backup-1'], ['backup-2', 'backup-3'], ['backup-4', 'backup-0']):
                with self.assertRaisesRegex(RuntimeError, 'pass aborted during delivery'):
                    self.reconcile_once()
                self.assertEqual(attempted[-2:], expected)
                state = json.loads(state_path.read_text())
                self.assertEqual(state['orphan_recipient_cursor'], expected[-1])
                self.assertEqual(state['orphan_notified'], {})
                self.assertFalse(any(key.startswith('orphan-batch:') for key in state['fired']))
        self.assertEqual(set(attempted), {f'backup-{i}' for i in range(5)})
        self.assertEqual(self.orphan_notices(), [])
        # Failures remain eligible, and only a successful send earns a receipt.
        self.reconcile_once()
        state = json.loads(state_path.read_text())
        self.assertEqual(set(state['orphan_notified']), {'r-001', 'r-006', 'r-002', 'r-007'})
        self.assertEqual({r['id'] for r in self.orphan_notices()}, {post_ids['backup-1'], post_ids['backup-2']})

    def test_orphan_cursor_survives_abort_after_failed_delivery_batch(self):
        from coordkit.process import ToolError
        self.manifest.reconciler.orphan_ask_cap = 2
        self.write_bus(self.orphan_fixture(count=10, recipients=5))
        attempted = []

        def fail(argv, manifest):
            attempted.append(argv[argv.index('--key') + 1].removeprefix('orphan-batch:'))
            raise ToolError('bus', 6, 'failed', 'transport')

        original_digest = addressed.to_lead_digest
        def abort_digest(manifest, source, text, **kwargs):
            if source == 'orphan-backlog':
                raise RuntimeError('digest aborted')
            return original_digest(manifest, source, text, **kwargs)

        with patch('coordkit.bus.main', side_effect=fail), \
                patch('coordkit.addressed.to_lead_digest', side_effect=abort_digest):
            for expected in (['backup-0', 'backup-1'], ['backup-2', 'backup-3'], ['backup-4', 'backup-0']):
                with self.assertRaisesRegex(RuntimeError, 'digest aborted'):
                    self.reconcile_once()
                self.assertEqual(attempted[-2:], expected)
                state = json.loads((self.manifest.paths.root / 'reconcile/state.json').read_text())
                self.assertEqual(state['orphan_recipient_cursor'], expected[-1])
                self.assertEqual(state['orphan_notified'], {})
        self.assertEqual(set(attempted), {f'backup-{i}' for i in range(5)})

    def test_abort_after_publication_before_receipt_retries_without_duplicate_batches(self):
        self.manifest.reconciler.orphan_ask_cap = 1
        self.write_bus(self.orphan_fixture())
        original = bus.main
        def abort_after_post(argv, manifest):
            original(argv, manifest)
            raise RuntimeError('aborted after publication')
        with patch('coordkit.bus.main', side_effect=abort_after_post):
            with self.assertRaisesRegex(RuntimeError, 'aborted after publication'):
                self.reconcile_once()
        state_path = self.manifest.paths.root / 'reconcile/state.json'
        state = json.loads(state_path.read_text())
        self.assertEqual(state['orphan_notified'], {})
        self.assertEqual(state['orphan_batches'], {})
        self.assertEqual(state['orphan_recipient_cursor'], 'backup-0')
        first_id = self.orphan_notices()[0]['id']
        for _ in range(4):
            self.reconcile_once()
        self.assertEqual(len(self.orphan_notices()), 3)
        self.assertEqual(self.orphan_notices()[0]['id'], first_id)
        self.assertEqual(len(json.loads(state_path.read_text())['orphan_notified']), 50)

    def test_abort_after_receipts_does_not_resend_delivered_batch(self):
        self.write_bus(self.orphan_fixture())
        original = addressed.to_lead_digest
        def abort_summary(manifest, source, text, **kwargs):
            if source == 'orphan-backlog':
                raise RuntimeError('aborted summary')
            return original(manifest, source, text, **kwargs)
        with patch('coordkit.addressed.to_lead_digest', side_effect=abort_summary):
            with self.assertRaisesRegex(RuntimeError, 'aborted summary'):
                self.reconcile_once()
        self.assertEqual(len(self.orphan_notices()), 3)
        self.reconcile_once()
        self.assertEqual(len(self.orphan_notices()), 3)
        state = json.loads((self.manifest.paths.root / 'reconcile/state.json').read_text())
        self.assertEqual(len(state['orphan_notified']), 50)

    def test_coordinator_fallback_is_one_complete_replaceable_digest_batch(self):
        self.manifest.obligations.coordinator = 'coordinator'
        rows = self.orphan_fixture()
        self.manifest.addressed.agents = {}
        self.write_bus(rows)
        self.reconcile_once()
        digest_path = self.manifest.paths.root / 'state/addressed/lead-digest.ndjson'
        entries = [json.loads(line) for line in digest_path.read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['event'], 'orphan-batch:coordinator')
        self.assertEqual(entries[0]['text'].count('\n- '), 10)
        self.assertIn('40 more.', entries[0]['text'])
        self.assertIn('bus open-blockers --orphaned', entries[0]['text'])
        self.assertEqual(self.orphan_notices(), [])
        self.reconcile_once()
        self.assertEqual(len(digest_path.read_text().splitlines()), 1)
        rows.append(row('r-999', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead',
                        to='worker-1', kind='ruling', owner='worker-1'))
        self.write_bus(rows)
        self.reconcile_once()
        entries = [json.loads(line) for line in digest_path.read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertIn('41 more.', entries[0]['text'])
        # Flushing keeps the full bounded listing and command footer, rather than
        # truncating the batch to the generic finding preview.
        sent = []
        with patch('coordkit.bus.main', side_effect=lambda argv, m, **kwargs: sent.append(argv)):
            addressed.flush_lead_digest(self.manifest, {}, AT, out=lambda _: None, rows=[])
        self.assertEqual(len(sent), 1)
        self.assertIn(entries[0]['text'], sent[0][-1])

    def test_failed_digest_batch_has_no_receipts_and_uses_recipient_cap(self):
        from coordkit.process import ToolError
        self.manifest.reconciler.orphan_ask_cap = 1
        rows = self.orphan_fixture(count=6, recipients=2)
        self.manifest.obligations.coordinator = 'backup-0'
        self.write_bus(rows)
        original = addressed.to_lead_digest
        def fail_batch(manifest, source, text, **kwargs):
            if source == 'orphan-batch:backup-0':
                raise ToolError('digest', 6, 'delivery failed', 'transport')
            return original(manifest, source, text, **kwargs)
        with patch('coordkit.addressed.to_lead_digest', side_effect=fail_batch):
            self.reconcile_once()
        state_path = self.manifest.paths.root / 'reconcile/state.json'
        state = json.loads(state_path.read_text())
        self.assertEqual(state['orphan_notified'], {})
        self.assertEqual(state['orphan_batches'], {})
        self.assertEqual(self.orphan_notices(), [])
        self.reconcile_once()
        self.assertEqual([r['to'] for r in self.orphan_notices()], ['backup-1'])
        self.assertEqual(len(json.loads(state_path.read_text())['orphan_notified']), 3)
        self.reconcile_once()
        self.assertEqual(len(json.loads(state_path.read_text())['orphan_notified']), 6)
        self.write_bus([])
        self.reconcile_once()
        digest_path = self.manifest.paths.root / 'state/addressed/lead-digest.ndjson'
        self.assertEqual(digest_path.read_text(), '')

    def test_batch_line_limit_is_configurable(self):
        self.manifest.reconciler.orphan_batch_lines = 2
        self.write_bus(self.orphan_fixture(count=5, recipients=1))
        self.reconcile_once()
        notice = self.orphan_notices()[0]
        text = Path(notice['spill_ref']).read_text() if notice.get('spill_ref') else notice['text']
        self.assertEqual(text.count('\n- '), 2)
        self.assertIn('3 more.', text)

    def test_zero_orphan_cap_preserves_backlog_and_only_queues_summary(self):
        self.manifest.reconciler.orphan_ask_cap = 0
        self.write_bus([row('r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead',
                            to='worker', kind='ruling', owner='worker')])
        self.assertEqual(self.reconcile_once(), 5)
        self.assertEqual(self.reconcile_once(), 5)
        self.assertEqual(self.orphan_notices(), [])
        state = json.loads((self.manifest.paths.root / 'reconcile/state.json').read_text())
        self.assertEqual(state['orphan_notified'], {})
        digest = self.manifest.paths.root / 'state/addressed/lead-digest.ndjson'
        self.assertEqual(len(digest.read_text().splitlines()), 1)
        self.write_bus([])
        self.reconcile_once()
        self.assertEqual(digest.read_text(), '')

    def test_manifest_orphan_cap_is_a_nonnegative_integer(self):
        original = self.file.read_text()
        for value in ('-1', 'true', '1.5', '"3"'):
            self.file.write_text(original + '\n[reconciler]\norphan_ask_cap = ' + value + '\n')
            with self.assertRaisesRegex(ManifestError, 'orphan_ask_cap'):
                load(self.file)

    def test_manifest_batch_lines_are_bounded_positive_integers(self):
        original = self.file.read_text()
        self.assertEqual(load(self.file).reconciler.orphan_ask_cap, 10)
        self.assertEqual(load(self.file).reconciler.orphan_batch_lines, 10)
        for value in ('0', '-1', '51', 'true', '1.5', '"3"'):
            self.file.write_text(original + '\n[reconciler]\norphan_batch_lines = ' + value + '\n')
            with self.assertRaisesRegex(ManifestError, 'orphan_batch_lines'):
                load(self.file)
        self.file.write_text(original + '\n[reconciler]\norphan_ask_cap = 0\norphan_batch_lines = 50\n')
        self.assertEqual(load(self.file).reconciler.orphan_batch_lines, 50)

    def test_reconcile_routes_an_orphan_to_fallback_once(self):
        self.manifest.bus.mirror = False
        self.manifest.obligations.coordinator = 'lead'
        self.manifest.addressed.agents = {'worker': {'fallback': 'backup'}, 'backup': {}}
        ruling = row('r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead', to='worker', kind='ruling', owner='worker')
        self.write_bus([ruling])
        for _ in range(2):
            self.reconcile_once()
        notices = self.orphan_notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['to'], 'backup')
        state = json.loads((self.manifest.paths.root / 'reconcile/state.json').read_text())
        self.assertIn('r', state['orphan_notified'])
        # An orphan is not also chased as an ordinary owner obligation.
        self.assertFalse([e for e in self.escalations() if 'has not been acknowledged' in e['args'][1]])

    def test_reconcile_orphan_target_precedence_retirement_then_fallback_then_coordinator(self):
        self.manifest.bus.mirror = False
        self.manifest.obligations.coordinator = 'coordinator'
        self.manifest.addressed.agents = {'worker': {'fallback': 'backup'}, 'backup': {},
                                          'retired': {'fallback': 'backup'}, 'replacement': {}}
        lanes = self.manifest.paths.bus_log.parent / 'state' / 'lanes.json'
        lanes.parent.mkdir(parents=True, exist_ok=True)
        lanes.write_text(json.dumps({'retired': {'owner': 'replacement'}}))
        rows = [row('retired-r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead', to='retired',
                    kind='ruling', owner='retired'),
                row('fallback-r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead', to='worker',
                    kind='ruling', owner='worker'),
                row('coordinator-r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead', to='nobody',
                    kind='ruling', owner='nobody')]
        self.write_bus(rows)
        self.reconcile_once()
        targets = {re.search(r'- \w+ (\S+):', r['text']).group(1): r['to']
                   for r in self.orphan_notices()}
        self.assertEqual(targets, {'retired-r': 'replacement', 'fallback-r': 'backup'})
        # The coordinator is digest-only, so its orphan is routed to the lead digest.
        digest = self.manifest.paths.root / 'state/addressed/lead-digest.ndjson'
        self.assertIn('coordinator-r', digest.read_text())

    def test_reconcile_retries_failed_orphan_delivery_and_dry_run_writes_nothing(self):
        from coordkit.process import ToolError
        self.manifest.bus.mirror = False
        self.manifest.obligations.coordinator = 'lead'
        self.manifest.addressed.agents = {'worker': {'fallback': 'backup'}, 'backup': {}}
        ruling = row('r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead', to='worker',
                     kind='ruling', owner='worker')
        self.write_bus([ruling])
        self.reconcile_once(dry_run=True)
        self.assertEqual(self.orphan_notices(), [])
        self.assertFalse((self.manifest.paths.root / 'reconcile/state.json').exists())
        with patch('coordkit.bus.main', side_effect=ToolError('bus', 6, 'delivery failed', 'transport')):
            self.reconcile_once()
        self.assertEqual(self.orphan_notices(), [])
        state = json.loads((self.manifest.paths.root / 'reconcile/state.json').read_text())
        self.assertNotIn('r', state.get('orphan_notified', {}))
        self.reconcile_once()
        self.assertEqual(len(self.orphan_notices()), 1)
        state = json.loads((self.manifest.paths.root / 'reconcile/state.json').read_text())
        self.assertIn('r', state['orphan_notified'])

    def test_reconcile_unroutable_orphan_does_not_abort_other_orphans(self):
        self.manifest.bus.mirror = False
        self.manifest.obligations.coordinator = 'lead'
        self.manifest.addressed.agents = {'a': {}, 'b': {}, 'c': {'fallback': 'd'}, 'd': {}}
        lanes = self.manifest.paths.bus_log.parent / 'state' / 'lanes.json'
        lanes.parent.mkdir(parents=True, exist_ok=True)
        # a and b redirect to each other; the coordinator and lead are retired too,
        # so the first orphan has no live recipient at all.
        lanes.write_text(json.dumps({'a': {'owner': 'b'}, 'b': {'owner': 'a'},
                                     'lead': {'owner': 'a'}}))
        rows = [row('a-r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead', to='a',
                    kind='ruling', owner='a'),
                row('c-r', '2026-10-31T00:00:00Z', origin='coordkit', frm='lead', to='c',
                    kind='ruling', owner='c')]
        self.write_bus(rows)
        self.reconcile_once()
        targets = {re.search(r'- \w+ (\S+):', r['text']).group(1): r['to']
                   for r in self.orphan_notices()}
        self.assertEqual(targets, {'c-r': 'd'})

    def test_reconcile_live_blocker_is_not_orphaned(self):
        self.manifest.bus.mirror = False
        self.manifest.obligations.coordinator = 'lead'
        self.manifest.addressed.agents = {'lead-b': {}, 'lead': {}}
        blocker = row('r-012', '2026-11-05T00:02:42Z', origin='coordkit', frm='worker-j', to='lead-b',
                      kind='blocker', owner='lead-b', needs='investigate')
        live = row('lead-b-live', '2026-11-10T02:00:00Z', origin='coordkit', frm='lead-b', to='lead', kind='fyi')
        self.write_bus([blocker, live])
        self.reconcile_once()
        self.assertEqual(self.orphan_notices(), [])
