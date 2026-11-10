import contextlib
import io
import json
from unittest.mock import patch
from coordkit import bus, reconcile
from tests.support import FakeCase


class ReconcileTests(FakeCase):
    def run_reconcile(self,dry=False,at=10000):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return reconcile.reconcile(self.manifest,dry,at)

    def cards(self,*cards):
        return {'tool':'anx','contains':['cards','list'],'json':{'ok':True,'result':{'cards':list(cards)}}}

    def test_dead_owner_grace_handoff_and_live_reset(self):
        self.respond(self.cards({'handle':'task','column_key':'in_progress'}),
                     {'tool':'anx','contains':['lease','show'],'json':{'ok':True,'result':{'lease':{'owner':'actor:worker','epoch':2}}}})
        self.assertEqual(self.run_reconcile(at=0),0)
        self.assertFalse(any('release' in c['args'] for c in self.calls()))
        self.assertEqual(self.run_reconcile(at=601),5)
        release=next(c['args'] for c in self.calls() if 'release' in c['args'])
        self.assertIn('actor:ck-sample-lead',release)
        self.assertEqual(self.run_reconcile(at=1200),0)

    def test_loop_wake_once_then_ask_at_threshold(self):
        self.manifest.loop.loops={'audit':{}}
        self.respond({'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'loops':[
            {'ref':'card:loop-audit','state':'past_promise','owner':'worker','promise':{'at':'1970-01-01'}}]}}})
        for _ in range(4): self.run_reconcile()
        rows=[json.loads(s) for s in self.manifest.paths.bus_log.read_text().splitlines()]
        self.assertEqual(len([r for r in rows if r['kind']=='handoff' and not r.get('copy_of')]),1)
        asks=self.escalations()  # a machine finding goes to an agent on the bus, never to the operator
        self.assertEqual(len(asks),1)
        self.assertEqual(asks[0]['to'],'lead')
        for part in ('Restart the loop-audit card, which stopped reporting?','Options:','Recommended:','[card:loop-audit]'):
            self.assertIn(part,asks[0]['args'][1])

    def test_machine_ask_is_a_plain_brief(self):
        # Machine findings must be phrased as a plain decision brief.
        from coordkit import briefs
        self.manifest.loop.loops={'audit':{}}
        self.respond({'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'loops':[
            {'ref':'card:loop-audit','state':'past_promise','owner':'worker','promise':{'at':'1970-01-01'}}]}}})
        for _ in range(4): self.run_reconcile()
        self.assertFalse([c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']])
        text=self.escalations()[0]['args'][1]
        self.assertTrue(text.startswith('coordkit loops: Restart the loop-audit card, which stopped reporting? Recommended:'))
        self.assertIn('[card:loop-audit] card:loop-audit has missed its receipt',text)  # agents get the exact ids
        self.assertIn('Recommended: Restart it now. Options: Restart it now / Pause the loop / Leave it for now.',text)
        self.assertIn('Pause the loop: No more cycles',text)  # each option with its consequence
        self.assertIn('Next step: Inspect the loop runner',text)

    def test_reconcile_hands_a_question_answer_to_the_asker(self):
        # reconcile's answer read may consume a question's answer; it must be relayed, not dropped.
        from coordkit import questions
        from unittest.mock import patch as _patch
        rec={'qid':'q-aaaaaa','state':'escalated','asker':'worker','anx_ask_id':'event:ask-1','question':'Ship?'}
        relayed=[]
        self.respond({'tool':'anx','contains':['await'],'json':{'ok':True,'result':{'answers':[
            {'ask_id':'event:ask-1','subject_ref':'card:loop-audit','answer':{'text':'yes'}}]}}})
        with _patch.object(questions,'load_all',lambda m: {'q-aaaaaa':rec}), \
                _patch.object(questions,'relay_anx_answer',lambda m,r,a: relayed.append((r['qid'],a))):
            self.run_reconcile()
        self.assertEqual(relayed,[('q-aaaaaa',{'text':'yes'})])

    def test_repeated_reconciliation_never_reposts_the_delivered_ask(self):
        self.manifest.loop.loops={'audit':{}}
        self.respond({'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'loops':[
            {'ref':'card:loop-audit','state':'past_promise','owner':'worker','promise':{'at':'1970-01-01'}}]}}})
        for _ in range(6): self.run_reconcile()
        self.assertEqual(len(self.escalations()),1)

    def test_answer_to_a_mirrored_blocker_correlates_by_its_brief_title(self):
        from coordkit import briefs
        row={'id':'b1','frm':'worker','owner':'lead','kind':'blocker','subject':'card:task','text':'blocked','needs':'Approve the cap'}
        self.assertEqual(briefs.blocker_title(row),'Unblock worker: Approve the cap?')

    def test_dead_letters_to_fyi_once(self):
        self.respond({'tool':'agentctl','contains':['anx','status'],'json':{'ok':True,'result':{'dead_letters':[
            {'execution_id':'exec-dead','reason':'card_unresolved','phase':'start'}]}}})
        self.run_reconcile(); self.run_reconcile()
        rows=[json.loads(s) for s in self.manifest.paths.bus_log.read_text().splitlines()]
        rows=[r for r in rows if not (r['frm']=='coordkit' and r['kind']=='ask')]  # routed stock findings are separate
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['kind'],'fyi')

    def test_foreign_dead_letters_ignored_and_cached(self):
        def status(execution,key):
            return {'tool':'agentctl','contains':['status',execution],'json':{'ok':True,'result':{'delegation':{'request_key':key}}}}
        self.respond({'tool':'agentctl','contains':['anx','status'],'json':{'ok':True,'result':{'dead_letters':[
            {'execution_id':'exec-other','reason':'card_unresolved','phase':'start'},
            {'execution_id':'exec-ours','reason':'card_unresolved','phase':'start'}]}}},
            status('exec-other','external-run'),status('exec-ours','ck-sample-worker'))
        self.run_reconcile(); self.run_reconcile()
        rows=[json.loads(s) for s in self.manifest.paths.bus_log.read_text().splitlines()]
        rows=[r for r in rows if not (r['frm']=='coordkit' and r['kind']=='ask')]  # routed stock findings are separate
        self.assertEqual([r['text'] for r in rows],['agentctl card_unresolved dead letter: exec-ours phase=start'])
        lookups=[c for c in self.calls() if c['tool']=='agentctl' and 'status' in c['args'] and 'anx' not in c['args']]
        self.assertEqual(len(lookups),2)

    def test_merged_review_and_old_ruling_ask(self):
        self.respond(self.cards({'handle':'task','column_key':'review','prs':[123]}),
                     {'tool':'gh','stdout':json.dumps({'state':'MERGED','mergeCommit':{'oid':'a'*40}})})
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'kind':'ruling','id':'r1','origin':'coordkit','owner':'worker','frm':'lead','to':'worker','topic':'t',
                                'text':'act','subject':'card:task','ts':'1970-01-01T00:00:00Z'})+'\n')
        self.assertEqual(self.run_reconcile(),5)
        self.assertEqual(len([c for c in self.calls() if 'ask' in c['args']]),2)

    def test_mirror_lag_replays_and_dry_run_writes_nothing(self):
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True)
        row={'kind':'handoff','id':'h1','frm':'worker','to':'lead','subject':'card:task','text':'done','ts':'2026-01-01T00:00:00Z'}
        p.write_text(json.dumps(row)+'\n')
        before={str(x):x.read_bytes() for x in self.root.rglob('*') if x.is_file()}
        self.assertEqual(self.run_reconcile(True),5)
        after={str(x):x.read_bytes() for x in self.root.rglob('*') if x.is_file() and x != self.log}
        self.assertEqual(before,after)
        self.assertFalse(any('move' in c['args'] or 'note' in c['args'] for c in self.calls()))
        self.assertEqual(self.run_reconcile(),5)
        self.assertEqual((self.manifest.paths.root/'state/mirror.cursor').read_text().strip(),'1')

    def test_live_owner_never_swept_and_unknown_preserved(self):
        self.respond(self.cards({'handle':'task','column_key':'in_progress'}),
                     {'tool':'anx','contains':['lease','show'],'json':{'ok':True,'result':{'lease':{'owner':'actor:worker','epoch':1}}}},
                     {'tool':'agentctl','contains':['recent'],'json':{'ok':True,'result':{'executions':[{'labels':['worker'],'liveness':'alive'}]}}})
        self.run_reconcile(at=0); self.run_reconcile(at=10000)
        self.assertFalse(any('release' in c['args'] for c in self.calls()))

    def test_unrelated_workspace_loop_is_never_woken(self):
        self.manifest.loop.loops={'audit':{}}
        self.respond({'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'loops':[
            {'ref':'card:loop-other-project','state':'past_promise','owner':'worker'}]}}})
        self.assertEqual(self.run_reconcile(),0)
        self.assertFalse(self.manifest.paths.bus_log.exists())
        self.assertFalse(any('ask' in c['args'] for c in self.calls()))

    def test_answer_projects_decision_and_closes_blocker(self):
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'kind':'blocker','id':'b1','origin':'coordkit','frm':'worker','owner':'lead','to':'lead',
                                'subject':'card:task','topic':'t','needs':'decide','text':'question','ts':'2026-01-01T00:00:00Z'})+'\n')
        (self.manifest.paths.root/'state').mkdir()
        (self.manifest.paths.root/'state/mirror.cursor').write_text('1')
        self.respond({'tool':'anx','contains':['await'],'json':{'ok':True,'result':{'answers':[
            {'ask_id':'a1','subject_ref':'card:task','answer':{'text':'proceed'}}]}}})
        # Keep the historical replay's wall clock equal to reconcile(at=10000),
        # including answer routing's read-only open-blocker lookup.
        with patch('coordkit.closure.time.time',return_value=10000):
            self.assertEqual(self.run_reconcile(),5)
            bus.configure(self.manifest)
            self.assertEqual(bus.open_blockers(bus.read()),[])
            self.run_reconcile()
            self.assertEqual(len([r for r in bus.read() if r['kind']=='decision' and not r.get('copy_of')]),1)

    def test_orphaned_lead_blocker_answer_still_closes(self):
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'kind':'blocker','id':'b1','origin':'coordkit','frm':'worker','owner':'lead','to':'lead',
                                'subject':'card:task','topic':'t','needs':'decide','text':'question','ts':'2026-01-01T00:00:00Z'})+'\n')
        (self.manifest.paths.root/'state').mkdir()
        (self.manifest.paths.root/'state/mirror.cursor').write_text('1')
        bus.configure(self.manifest)
        self.assertEqual(bus.open_blockers(bus.read()), [])
        self.assertEqual([r['id'] for r in bus.open_blockers(bus.read(), orphaned=True)], ['b1'])
        self.respond({'tool':'anx','contains':['await'],'json':{'ok':True,'result':{'answers':[
            {'ask_id':'a1','subject_ref':'card:task','answer':{'text':'proceed'}}]}}})
        self.run_reconcile()
        decisions=[r for r in bus.read() if r['kind']=='decision' and not r.get('copy_of')]
        self.assertEqual([(r['to'], r.get('re')) for r in decisions], [('worker', 'b1')])
        self.assertEqual(bus.open_blockers(bus.read(), orphaned=True), [])

    def test_lead_principal_id_lease_is_the_lead(self):
        self.respond(self.cards({'handle':'loop-audit','column_key':'in_progress'}),
                     {'tool':'anx','contains':['lease','show'],'json':{'ok':True,'result':{'lease':{'owner':'actor:agent_lead','epoch':1}}}},
                     {'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'principal':{'id':'agent_lead'}}}})
        out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):
            reconcile.reconcile(self.manifest,False,10000)
        self.assertNotIn('unknown_owner',out.getvalue())

    def test_shared_repo_core_worktree_asks(self):
        import subprocess
        repo=self.root/'shared'; subprocess.run(['/usr/bin/git','init','-q',str(repo)],check=True)
        self.manifest.reconciler.shared_git_dirs=[str(repo/'.git')]
        self.run_reconcile(at=10000)
        self.assertFalse([c for c in self.calls() if 'ask' in c['args'] and any('core.worktree' in a for a in c['args'])])
        subprocess.run(['/usr/bin/git','config','--file',str(repo/'.git/config'),'core.worktree','/elsewhere'],check=True)
        self.run_reconcile(at=10300)
        asks=[c['args'] for c in self.calls() if 'ask' in c['args'] and any('core.worktree=/elsewhere' in a for a in c['args'])]
        self.assertEqual(len(asks),1)

    def test_reconcile_never_posts_per_item_obligation_reminders(self):
        ask = {'ts':'1970-01-01T00:00:00Z', 'id':'q1', 'frm':'worker', 'to':'lead',
               'topic':'general', 'kind':'ask', 'text':'ship it?'}
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(json.dumps(ask)+'\n')
        with patch('coordkit.obligations.gh_states', return_value={}):
            self.run_reconcile(at=1000)
            self.run_reconcile(at=1000+8*86400)
        self.assertFalse([r for r in bus.read() if r['id'].startswith(('wake-ob-', 'alert-ob-'))])


    def test_incumbent_history_before_adoption_raises_no_asks(self):
        # Adopting an existing log must not escalate its historical obligations.
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(''.join(json.dumps(r)+'\n' for r in [
            {'kind':'ruling','id':'old','owner':'worker','frm':'lead','to':'worker','topic':'t','text':'act','ts':'1970-01-01T00:00:00Z'},
            {'kind':'blocker','id':'oldb','owner':'lead','needs':'x','frm':'worker','to':'lead','topic':'t','text':'stuck','ts':'1970-01-01T00:00:00Z'}]))
        self.run_reconcile(at=10000)
        self.assertFalse([c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']])
        # A ruling the incumbent posts after adoption is still escalated.
        with p.open('a') as f:
            f.write(json.dumps({'kind':'ruling','id':'new','owner':'worker','frm':'lead','to':'worker','topic':'t',
                                'text':'act','subject':'card:task','ts':'1970-01-01T03:00:00Z'})+'\n')
        self.run_reconcile(at=10800+3600)
        self.assertTrue([c for c in self.escalations() if 'rulings' in c['args'][1]])


class LegacyBusRowsTests(FakeCase):
    """Legacy input can omit IDs, owners or kinds; normalization must keep valid rows usable."""
    LEGACY = [
        {'ts':'2026-01-01T11:15:01+00:00','frm':'lead','to':'all','topic':'process','kind':'ruling','text':'old ruling, no id or owner'},
        {'ts':'2026-01-01T11:16:00+00:00','frm':'worker-a','to':'lead','topic':'t','kind':'ask','text':'old ask, no id'},
        {'ts':'2026-01-01T11:17:00+00:00','frm':'lead','to':'worker-a','topic':'t','kind':'answer','text':'old answer','re':'nothing'},
        {'ts':'2026-01-01T11:18:00+00:00','frm':'lead','to':'worker-a','text':'no kind'},
        [],
        {'id':'badts','ts':'not a time','frm':'x','to':'y','kind':'msg','text':'t'},
        {'ts':'2026-01-01T11:19:00+00:00','frm':'lead','to':'worker-a','kind':'hold','pr':'123','text':'hold #123'},
    ]

    def write_bus(self):
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(''.join(json.dumps(r)+'\n' for r in self.LEGACY))

    def test_rows_keep_positions_and_get_synthetic_ids(self):
        self.write_bus(); bus.configure(self.manifest)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rows=bus.read()
            count,tail=bus.read_from(5)
        self.assertEqual(len(rows),len(self.LEGACY)); self.assertEqual(count,len(self.LEGACY))
        self.assertEqual([r['id'] for r in rows],[f'legacy-L{i}' for i in range(5)]+['badts','legacy-L6'])
        self.assertEqual([r['kind'] for r in rows][3:6],['','legacy_invalid','msg'])  # content kept, gaps filled
        self.assertEqual([r['ts'] for r in rows[4:6]],[bus.EPOCH_TS]*2)
        self.assertEqual(rows[5]['id'],'badts'); self.assertEqual(rows[5]['legacy'],'ts')
        self.assertEqual(tail,[(5,rows[5]),(6,rows[6])])  # every reader sees the same rows at the same lines
        self.assertEqual(rows[0]['text'],'old ruling, no id or owner')  # content kept for real rows
        self.assertNotIn('legacy',self.manifest.paths.bus_log.read_text())  # the log is never rewritten

    def test_stock_reconcile_mirror_sweep_and_merge_survive_legacy_rows(self):
        from coordkit import addressed, merge, stock
        self.write_bus(); bus.configure(self.manifest)
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            stock.stock(self.manifest,10**10)
            self.assertIn(reconcile.reconcile(self.manifest,True,10**10),(0,5))
            self.assertIn(reconcile.reconcile(self.manifest,False,10**10),(0,5))
            addressed.sweep(self.manifest,at=10**10,dry_run=True,out=lambda *a,**k:None)
        self.assertTrue(merge.holds_on('123', bus.read()))
        self.assertFalse([c for c in self.calls() if 'ask' in c['args']])  # history raises no obligations

    def test_inbox_stock_counts_only_unacked_obligations(self):
        from coordkit import stock
        p=self.manifest.paths.bus_log; p.parent.mkdir(parents=True, exist_ok=True)
        rows=[{'id':'f1','origin':'coordkit','ts':'2026-01-01T11:00:00+00:00','frm':'worker','to':'lead','kind':'fyi','text':'fyi'},
              {'id':'a1','origin':'coordkit','ts':'2026-01-01T11:00:00+00:00','frm':'worker','to':'lead','kind':'ask','text':'open ask'},
              {'id':'a2','origin':'coordkit','ts':'2026-01-01T11:00:00+00:00','frm':'worker','to':'lead','kind':'ask','text':'acked ask'},
              {'id':'k2','origin':'coordkit','ts':'2026-01-01T11:01:00+00:00','frm':'lead','to':'worker','kind':'answer','re':'a2','text':'received'}]
        p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(stock.stock(self.manifest, 10**10)['inbox']['count'], 1)

    def test_stock_counts_only_rows_since_adoption(self):
        from coordkit import stock
        self.write_bus()
        with self.manifest.paths.bus_log.open('a') as f:
            f.write(json.dumps({'id':'new','origin':'coordkit','ts':'2026-01-01T11:00:00+00:00','frm':'lead','to':'worker',
                                'kind':'ask','text':'ours'})+'\n')
        with contextlib.redirect_stderr(io.StringIO()):
            everything=stock.stock(self.manifest,10**10)
            adopted=stock.stock(self.manifest,10**10,since=10**10-1)
        self.assertGreater(everything['asks']['count'],1)
        self.assertEqual(adopted['asks']['count'],1)  # coordkit's own ask always counts; history never does
        self.assertEqual(adopted['rulings']['count'],0); self.assertEqual(adopted['holds']['count'],0)
        with contextlib.redirect_stdout(io.StringIO()) as out,contextlib.redirect_stderr(io.StringIO()):
            reconcile.reconcile(self.manifest,True,10**10)
        self.assertIn('RECONCILE stock asks count=1 ',out.getvalue())  # reconcile passes its baseline
