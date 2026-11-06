import contextlib
import fcntl
import io
import json
from unittest.mock import patch
from coordkit import bus, reconcile
from coordkit.state import read_json, write_json
from tests import test_reconcile
from tests.support import FakeCase


class FootgunReconcileTests(FakeCase):
    run_reconcile = test_reconcile.ReconcileTests.run_reconcile
    cards = test_reconcile.ReconcileTests.cards
    def rows(self, *rows):
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        # origin='coordkit': these model coordkit's own rows, which the adoption baseline never filters.
        path.write_text(''.join(json.dumps(dict(dict(ts='1970-01-01T00:00:00Z', origin='coordkit', frm='worker', to='lead',
                                              subject='card:task', text='question'), **row))+'\n' for row in rows))
        self.manifest.bus.mirror = False

    def asks(self):
        return [c['args'] for c in self.calls() if 'ask' in c['args']]

    def test_p1_1_blocker_wakes_once_asks_then_clears(self):
        self.rows(dict(kind='blocker', id='b1', owner='worker', needs='repair'))
        for at in (2000,2300,2600,2900): self.run_reconcile(at=at)
        self.assertEqual(len(self.asks()),1)
        bus.configure(self.manifest)
        self.assertEqual(len([r for r in bus.read() if r['kind']=='handoff' and not r.get('copy_of')]),1)
        bus.main(['post','--from','lead','--to','worker','--kind','decision','--re','b1',
                  '--rollback','revisit','--force','abandoned with reason'],self.manifest)
        self.run_reconcile(at=3200)
        self.assertEqual(read_json(self.manifest.paths.root/'reconcile/state.json')['blockers'],{})

    def test_p1_2_hold_age_escalates_and_normal_unhold_clears(self):
        self.rows(dict(kind='hold', id='h1', prs=['142']))
        for at in (8000,8300,8600): self.run_reconcile(at=at)
        self.assertIn('unhold', ' '.join(self.asks()[0]))
        self.manifest.bus.mirror = True
        with contextlib.redirect_stdout(io.StringIO()):
            bus.main(['post','--from','worker','--to','lead','--kind','unhold','--pr','142','--force','released'],self.manifest)
        self.assertTrue(any('card:hold-pr-142-worker' in c['args'] and 'resolve' in c['args'] for c in self.calls()))
        self.run_reconcile(at=8900)
        self.assertEqual(read_json(self.manifest.paths.root/'reconcile/state.json')['holds'],{})

    def test_p2_4_mirror_failures_ask_and_recovery_clears_counter(self):
        self.rows(dict(kind='lesson', id='m1'))
        self.manifest.bus.mirror = True
        with patch.object(bus,'append',side_effect=SystemExit(1)):
            for at in (0,300,600,900): self.run_reconcile(at=at)
        state=read_json(self.manifest.paths.root/'reconcile/state.json')
        self.assertEqual(state['mirror_failures'],4)
        # The bus itself is down, so the finding cannot be posted and there is no relay file: it stays unfired and is
        # retried, never marked sent.
        self.assertNotIn('mirror-failure',state['fired'])
        self.run_reconcile(at=1200)
        state=read_json(self.manifest.paths.root/'reconcile/state.json')
        self.assertEqual(state['mirror_failures'],0)
        self.assertNotIn('mirror-failure',state['fired'])

    def test_p2_7_ambiguous_answer_ages_asks_and_disposition_prunes(self):
        self.rows(dict(kind='blocker',id='b1',owner='lead',needs='choose'),
                  dict(kind='blocker',id='b2',owner='lead',needs='choose'))
        self.respond({'tool':'anx','contains':['await'],'json':{'ok':True,'result':{'answers':[
            {'ask_id':'a1','subject_ref':'card:task','answer':{'text':'pick one'}}]}}})
        self.run_reconcile(at=0); self.run_reconcile(at=3601)
        self.assertEqual(len(self.asks()),1)
        self.assertIn('pick one', ' '.join(self.asks()[0]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reconcile.main(['abandon-answer','a1','--reason','obsolete question'],self.manifest),0)
        self.assertEqual(read_json(self.manifest.paths.root/'reconcile/answers.json'),[])

    def test_p3_10_reconcile_skip_is_nonblocking_receipt(self):
        directory=self.manifest.paths.root/'reconcile'; directory.mkdir(parents=True)
        with (directory/'run.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reconcile.main([],self.manifest),4)
        self.assertEqual(read_json(directory/'skip.json')['reason'],'run_lock_held')
        self.assertEqual(self.calls(),[])

    def test_p3_11_gc_and_release_clear_dead_clock(self):
        path=self.manifest.paths.root/'reconcile/state.json'
        write_json(path,dict(fired={'ancient':0},dead={'ancient':0},loops={}))
        self.run_reconcile(at=700000)
        state=read_json(path)
        self.assertEqual(state['dead'],{}); self.assertNotIn('ancient',state['fired'])
        self.respond(self.cards({'handle':'task','column_key':'in_progress'}),
                     {'tool':'anx','contains':['lease','show'],'json':{'ok':True,'result':{'lease':{'owner':'worker','epoch':1}}}})
        self.run_reconcile(at=700001); self.run_reconcile(at=700602)
        self.assertEqual(read_json(path)['dead'],{})

    def test_p3_12_asks_after_missed_cadences_not_polls(self):
        self.manifest.loop.loops={'audit':{'default_cadence':'1h','fail_report_after':2}}
        self.respond({'tool':'anx','contains':['cards','get','card:loop-audit'],
                      'json':{'ok':True,'result':{'card':{'handle':'loop-audit','board_ref':'board:ck-sample',
                          'cadence':{'every_seconds':3600,'progress_budget_seconds':1800,'stop_rule':'STOP file present'}}}}},
                     {'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'loops':[
            {'ref':'card:loop-audit','state':'past_promise','owner':'worker','promise':{'at':'1970-01-01T00:00:00Z'}}]}}})
        for at in (0,300,600,7199): self.run_reconcile(at=at)
        self.assertEqual(self.asks(),[])
        self.run_reconcile(at=7200)
        self.assertEqual(len(self.asks()),1)

    def test_p3_13_hold_request_keys_include_poster(self):
        from coordkit.mirror import mirror
        rows=[]
        for poster in ('alice','bob'):
            row=dict(kind='hold',id=poster,frm=poster,prs=['142'],text='check evidence')
            rows.append(row)
            mirror(self.manifest,row,rows)
        creates=[c['args'] for c in self.calls() if 'create' in c['args']]
        keys=[args[args.index('--request-key')+1] for args in creates]
        self.assertEqual(keys,['ck-sample-hold-142-alice','ck-sample-hold-142-bob'])
        mirror(self.manifest,dict(kind='unhold',frm='lead',prs=['142']),rows)
        resolved=[c['args'] for c in self.calls() if 'resolve' in c['args']]
        self.assertEqual(len(resolved),2)

    def test_lead_text_release_clears_mirrored_hold_card(self):
        self.rows(dict(kind='hold',id='h1',prs=['142']),dict(kind='fyi',id='release',text='unhold PR #142',frm='lead'))
        self.respond(self.cards({'handle':'hold-pr-142-worker','column_key':'blocked'}))
        self.run_reconcile()
        self.assertTrue(any('resolve' in c['args'] and 'card:hold-pr-142-worker' in c['args'] for c in self.calls()))

    def test_owner_wake_no_mirror_does_not_disable_replay(self):
        self.rows(dict(kind='blocker',id='b1',owner='worker',needs='repair'))
        self.manifest.bus.mirror=True
        self.run_reconcile(at=2000)
        bus.configure(self.manifest)
        self.assertEqual(int((self.manifest.paths.root/'state/mirror.cursor').read_text()),len(bus.read()))
        self.assertTrue(any('note' in c['args'] for c in self.calls()))
