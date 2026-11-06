import contextlib
import datetime as dt
import io
import json
from unittest.mock import patch

from coordkit import auditor, obligations
from coordkit.manifest import load
from tests.support import FakeCase

T0 = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc).isoformat()


def stamp(minutes):
    return (dt.datetime.fromisoformat(T0) + dt.timedelta(minutes=minutes)).isoformat()


def row(id, ts=T0, **fields):
    return {'id':id, 'ts':ts, 'topic':'general', **fields}


class ObligationTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest = load(self.file)
        self.at = obligations.parse_ts(T0).timestamp() + 3*3600

    def open(self, rows, states=None):
        return {i['id']:i for i in obligations.open_obligations(self.manifest, rows, self.at, (states or {}).get)}

    def test_handoff_stays_open_until_picked_up(self):
        handoff = row('handoff-1', frm='worker-ui', to='lead', kind='handoff', text='PR #123 ready')
        found = self.open([handoff])
        self.assertEqual(found['handoff-1']['owner'], 'lead')
        self.assertEqual(found['handoff-1']['owner'], 'lead')
        self.assertEqual(obligations.stage(self.manifest, found['handoff-1']), ('alert', 0))
        self.assertIn('owner=lead age=3.0h', obligations.line(found['handoff-1']))

    def test_status_updates_and_broadcasts_are_not_obligations(self):
        rows = [row('a', frm='x', to='lead', kind='handoff', text='DONE, nothing to merge'),
                row('b', frm='x', to='all', kind='ask', text='anyone?'),
                row('c', frm='x', to='lead', kind='fyi', text='#123'),
                row('wake-1', frm='lead', to='x', kind='ask', text='#123')]
        self.assertEqual(self.open(rows), {})

    def test_each_closure_rule(self):
        ask = row('q', frm='x', to='lead', kind='ask', text='ship it?')
        pr = row('h', frm='x', to='lead', kind='handoff', text='#100 ready')
        later = stamp(20)
        self.assertEqual(set(self.open([ask, pr])), {'q', 'h'})
        # A reply from anyone but the sender closes; the sender's own follow-up and coordkit
        # reminders do not.
        self.assertEqual(set(self.open([ask, row('r', later, frm='x', re='q', kind='msg')])), {'q'})
        self.assertEqual(set(self.open([ask, row('wake-ob-1', later, frm='lead', re='q', kind='fyi')])), {'q'})
        self.assertEqual(set(self.open([ask, row('r', later, frm='lead', re='q', kind='msg')])), set())
        # The owner later naming the PR, a superseding handoff, or a merged PR closes.
        self.assertEqual(set(self.open([pr, row('m', later, frm='lead', to='y', kind='msg', text='merging #100')])), set())
        self.assertEqual(set(self.open([pr, row('n', later, frm='x', to='lead', kind='handoff', text='#100 rebased')])), {'n'})
        self.assertEqual(set(self.open([pr], {'100':'MERGED'})), set())
        self.assertEqual(set(self.open([pr], {'100':None})), {'h'})
        # Any named PR still open keeps the work open.
        two = row('h2', frm='x', to='lead', kind='handoff', text='#100 merged, #101 ready')
        self.assertEqual(set(self.open([two], {'100':'MERGED', '101':'OPEN'})), {'h2'})
        self.assertEqual(set(self.open([two], {'100':'MERGED', '101':'CLOSED'})), set())

    def test_reassignment_restarts_clock_for_new_owner(self):
        later = stamp(140)
        rows = [row('h', frm='x', to='lead', kind='handoff', text='#100 ready'),
                row('k', later, frm='lead', re='h', kind='ack', owner='alex')]
        found = self.open(rows)['h']
        self.assertEqual(found['owner'], 'alex')
        self.assertEqual(found['age'], 40*60)
        self.assertEqual(obligations.stage(self.manifest, found), 'wake')
        rows.append(row('z', stamp(150), frm='alex', re='h', kind='msg', text='on it'))
        self.assertEqual(self.open(rows), {})

    def test_auditor_flags_close_only_when_each_is_dispositioned(self):
        items = [{'title':'Main red has no owner', 'severity':'high'}, {'title':'Preview stuck', 'severity':'med'}]
        one, two = (obligations.flag_id(i['title']) for i in items)
        self.assertEqual(one, obligations.flag_id('  main RED has  no owner '))
        report = row('r', frm='auditor', to='coordinator', kind='handoff', topic='missed-work',
                     text=auditor.message(items, 'findings.md'))
        self.assertEqual(self.open([report])['r']['flags'], sorted([one, two]))
        later = lambda id, minute, frm, text: row(id, stamp(20 + minute), frm=frm, re='r', kind='msg', text=text)
        # Only the flag owner dispositions; a bare "closed" or "dismissed" without a reason does nothing.
        self.assertEqual(len(self.open([report, later('x', 1, 'lead', f'{one} {two} dismissed: no')])['r']['flags']), 2)
        self.assertEqual(len(self.open([report, later('y', 1, 'coordinator', 'closed, dismissed')])['r']['flags']), 2)
        partial = later('d1', 2, 'coordinator', f'{one} owner=lead')
        self.assertEqual(self.open([report, partial])['r']['flags'], [two])
        rest = later('d2', 3, 'coordinator', f'{two} dismissed: stale')
        self.assertEqual(self.open([report, partial, rest]), {})
        self.assertEqual(self.open([report, later('d3', 4, 'coordinator', 'dismissed: all stale')]), {})
        # A newer report carries a recurring flag forward instead of running two ladders.
        newer = row('r2', stamp(80), frm='auditor', to='coordinator', kind='handoff',
                    topic='missed-work', text=auditor.message(items[:1], 'f2.md'))
        found = self.open([report, newer])
        self.assertEqual((found['r']['flags'], found['r2']['flags']), ([two], [one]))
        # Incumbent reports without ids close on any reply.
        legacy = row('l', frm='auditor', to='lead', kind='handoff', topic='missed-work', text='5 items, f.md: [high] x')
        self.assertIn('l', self.open([legacy]))
        self.assertEqual(self.open([legacy, row('d4', frm='coordinator', re='l', kind='msg', text='owner=lead')]), {})

    def test_flag_ids_survive_the_text_cap(self):
        items = [{'title':f'Long finding {n} ' + 'x'*120, 'severity':'high'} for n in range(5)]
        text = auditor.message(items, '/very/long/path/' + 'y'*200)
        self.assertLessEqual(len(text), auditor.BUS_CAP)
        self.assertEqual(sorted(obligations.FLAG.findall(text)), sorted(obligations.flag_id(i['title']) for i in items))

    def test_ladder_steps_and_realert_buckets(self):
        item = {'age':0}
        steps = []
        for minutes in (10, 30, 119, 120, 359, 480, 840):
            item['age'] = minutes*60
            steps.append(obligations.stage(self.manifest, item))
        self.assertEqual(steps, [None, 'wake', 'wake', ('alert', 0), ('alert', 0), ('alert', 1), ('alert', 2)])


    def test_report_window_and_baseline(self):
        old = row('o', stamp(-48*60), frm='x', to='lead', kind='ask', text='old?')
        new = row('n', frm='x', to='lead', kind='ask', text='new?')
        with patch.object(obligations, 'gh_states', return_value={}):
            got = obligations.report(self.manifest, [old, new], self.at, window_hours=48)
            self.assertEqual([i['id'] for i in got], ['n'])
            got = obligations.report(self.manifest, [old, new], self.at, since=self.at)
            self.assertEqual(got, [])

    def test_bus_ack_owner_reassigns_through_the_cli(self):
        from coordkit import bus
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            bus.main(['post','--from','worker','--to','lead','--kind','ask','--force','--id','q1','ship #100?'],self.manifest)
            bus.main(['ack','q1','--from','lead','--owner','alex'],self.manifest)
        rows = bus.read()
        self.assertEqual((rows[-1]['kind'], rows[-1]['re'], rows[-1]['owner']), ('ack', 'q1', 'alex'))
        found = obligations.open_obligations(self.manifest, rows, self.at+10**9, lambda n: None)
        self.assertEqual([(i['id'], i['owner']) for i in found], [('q1', 'alex')])

    def test_addressed_ask_alerts_at_its_own_deadline(self):
        # Short addressed deadlines must not inherit the longer handoff deadline.
        ask = row('q', frm='coordinator', to='lead-a', kind='ask', text='go/no-go?')
        urgent = row('u', frm='x', to='lead-a', kind='ask', text='URGENT: worker roll pinned to the wrong SHA')
        flagged = row('p', frm='x', to='lead-a', kind='ask', priority='urgent', text='roll?')
        handoff = row('h', frm='x', to='lead-a', kind='handoff', text='#100 ready')
        at = obligations.parse_ts(T0).timestamp()
        due = lambda minutes: {i['id']: obligations.stage(self.manifest, i) for i in
                               obligations.open_obligations(self.manifest, [ask, urgent, flagged, handoff], at+minutes*60)}
        self.assertEqual(due(4), {'q': None, 'u': None, 'p': None, 'h': None})
        self.assertEqual(due(5), {'q': None, 'u': ('alert', 0), 'p': ('alert', 0), 'h': None})
        self.assertEqual(due(15), {'q': ('alert', 0), 'u': ('alert', 0), 'p': ('alert', 0), 'h': None})
        self.assertEqual(due(30)['h'], 'wake')
        found = {i['id']: i for i in obligations.open_obligations(self.manifest, [urgent], at+300)}
        self.assertTrue(obligations.line(found['u']).startswith('URGENT ask u from x owner=lead-a'))

    def test_report_due_lists_only_obligations_past_their_deadline(self):
        ask = row('q', frm='coordinator', to='lead-a', kind='ask', text='go/no-go?')
        handoff = row('h', frm='x', to='lead-a', kind='handoff', text='#100 ready')
        at = obligations.parse_ts(T0).timestamp() + 16*60
        out = io.StringIO()
        with patch.object(obligations, 'gh_states', return_value={}), \
             patch.object(obligations, 'report', lambda m, window_hours: obligations.open_obligations(m, [ask, handoff], at)), \
             contextlib.redirect_stdout(out):
            rc = obligations.main(['report', '--due', '--json'], self.manifest)
        self.assertEqual((rc, [i['id'] for i in json.loads(out.getvalue())]), (5, ['q']))
        out = io.StringIO()
        with patch.object(obligations, 'report', lambda m, window_hours: obligations.open_obligations(m, [ask, handoff], at)), \
             contextlib.redirect_stdout(out):
            # A fixed minimum age can exclude an ask that is already past its own deadline.
            obligations.main(['report', '--min-age', '2h', '--json'], self.manifest)
        self.assertEqual(json.loads(out.getvalue()), [])

    def test_urgent_handoff_keeps_the_pickup_ladder(self):
        handoff = row('h', frm='x', to='lead-a', kind='handoff', text='URGENT #100 ready')
        at = obligations.parse_ts(T0).timestamp()
        item = obligations.open_obligations(self.manifest, [handoff], at + 600)[0]
        self.assertEqual((item['priority'], item['due_after']), ('urgent', 7200))
        self.assertIsNone(obligations.stage(self.manifest, item))


    def test_lead_copy_of_an_alert_is_not_a_reply(self):
        ask = row('q', frm='x', to='lead', kind='ask', text='ship it?')
        alert = row('alert-ob-1-0', stamp(20), frm='lead', to='coordinator', kind='fyi', re='q')
        copy = row('alert-copy', stamp(20), frm='lead', to='lead', kind='fyi', re='q', copy_of='alert-ob-1-0')
        self.assertEqual(set(self.open([ask, alert, copy])), {'q'})

    def test_lead_copy_naming_the_pr_does_not_close_implicitly(self):
        handoff = row('h', frm='x', to='lead', kind='handoff', text='#100 ready')
        copy = row('c1', stamp(20), frm='lead', to='lead', kind='fyi', copy_of='alert-ob-1-0',
                   text='[copy: coordinator silent >3h] UNPICKED handoff h from x owner=lead #100')
        self.assertEqual(set(self.open([handoff, copy])), {'h'})
        self.assertEqual(set(self.open([handoff, dict(copy, copy_of=None, id='m1')])), set())
