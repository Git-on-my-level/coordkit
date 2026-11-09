import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
from unittest.mock import patch
from coordkit import auditor, loop
from coordkit.extract import clean, commit, extract
from coordkit.manifest import ManifestError
from tests.support import FakeCase


class LoopTests(FakeCase):
    def invoke(self, name='audit'):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = loop.run_cycle(self.manifest,name)
        return code,out.getvalue(),err.getvalue()

    @property
    def state(self):
        return self.manifest.paths.root/'loops/audit'

    def test_success_findings_handoff_and_receipt(self):
        code,out,err = self.invoke()
        self.assertEqual(code,0,err)
        findings = list(self.state.glob('runs/*/findings.md'))
        self.assertEqual(len(findings),1)
        self.assertIn('Verify receipt',findings[0].read_text())
        rows = [json.loads(s) for s in self.manifest.paths.bus_log.read_text().splitlines()]
        self.assertEqual(rows[0]['kind'],'handoff')
        self.assertEqual(rows[0]['evidence'],str(findings[0]))
        self.assertLessEqual(len(rows[0]['text']),380)
        notes = [c['args'] for c in self.calls() if 'note' in c['args']]
        self.assertTrue(any('--met' in a and '--evidence' in a and 'card:loop-audit' in a for a in notes))
        self.assertFalse(any('move' in c['args'] for c in self.calls()))
        self.assertEqual((self.state/'fails').read_text().strip(),'0')
        calls = [c for c in self.calls() if c['tool']=='agentctl' and 'delegate' in c['args']]
        self.assertEqual(len(calls),2)
        self.assertIn('--plan',calls[0]['args'])
        self.assertEqual(calls[0]['request'],calls[1]['request'])
        self.assertEqual(calls[1]['request']['selector'],{'pool':'plan','settings':{'access':'read_only'}})

    def test_external_state_root_nonempty_cycle_recovers_pending_handoff(self):
        # State storage is not necessarily inside the manifest or any checkout.
        self.manifest.paths.root = self.root.parent / (self.root.name + '-state')
        self.manifest.bus.mirror = False
        self.addCleanup(shutil.rmtree, self.manifest.paths.root)
        self.manifest.handoffs.repos = {'workspace': str(self.root / 'checkout')}
        self.respond({'tool': 'anx', 'contains': ['note'], 'exit': 2, 'stderr': 'receipt refused'})
        self.assertEqual(self.invoke()[0], 1)
        self.assertTrue((self.state / 'pending.json').exists())
        self.respond()
        code, out, err = self.invoke()
        self.assertEqual(code, 0, err)
        self.assertIn('CYCLE_OK audit', out)
        self.assertEqual((self.state / 'fails').read_text(), '0\n')
        self.assertFalse((self.state / 'pending.json').exists())
        rows = [json.loads(s) for s in self.manifest.paths.bus_log.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertTrue(Path(rows[0]['evidence']).is_relative_to(self.state / 'runs'))
        self.assertNotIn('handoff_degraded', rows[0])

    def test_empty_findings_still_receipt(self):
        self.respond({'tool':'agentctl','contains':['delegate','--wait'], 'stdout':'MISSED_JSON_START {"items":[]} MISSED_JSON_END'})
        code,out,err = self.invoke()
        self.assertEqual(code,0,err)
        self.assertIn('Nothing raised',next(self.state.glob('runs/*/findings.md')).read_text())
        self.assertFalse(self.manifest.paths.bus_log.exists())
        self.assertTrue(any('--met' in c['args'] for c in self.calls()))

    def test_failures_escalate_to_decide_on_third_then_recovery_resets(self):
        self.respond({'tool':'agentctl','contains':['delegate','--wait'], 'exit':11,'stderr':'failed auditor'})
        for count in range(1,5):
            code,out,err = self.invoke()
            self.assertEqual(code,1)
            self.assertEqual(int((self.state/'fails').read_text()),count)
        asks = [c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']]
        self.assertEqual(len(asks),1)
        args = asks[0]['args']
        self.assertIn('Restore the loop-audit card, which keeps failing?', args)
        self.assertEqual(args[args.index('--subject-ref') + 1], 'card:loop-audit')
        self.assertIn('--again', args)
        self.assertEqual(self.escalations(), [])
        self.respond()
        self.assertEqual(self.invoke()[0],0)
        self.assertEqual(int((self.state/'fails').read_text()),0)

    def test_snapshot_failure_no_delegate(self):
        self.respond({'tool':'gh','exit':1,'stderr':'not authenticated'})
        code,out,err = self.invoke()
        self.assertEqual(code,1)
        self.assertIn('not authenticated',err)
        self.assertFalse(any('delegate' in c['args'] for c in self.calls()))

    def test_bad_block_no_receipt(self):
        self.respond({'tool':'agentctl','contains':['delegate','--wait'],'stdout':'no block'})
        code,out,err = self.invoke()
        self.assertEqual(code,1)
        self.assertIn('POST_NO_BLOCK',err)
        self.assertFalse(any('--met' in c['args'] for c in self.calls()))

    def test_failed_receipt_no_success(self):
        self.respond({'tool':'agentctl','contains':['delegate','--wait'],'stdout':'MISSED_JSON_START {"items":[]} MISSED_JSON_END'},
                     {'tool':'anx','contains':['note'],'exit':2,'stderr':'receipt refused'})
        self.assertEqual(self.invoke()[0],1)
        self.assertEqual(int((self.state/'fails').read_text()),1)

    def test_stop_and_shadow(self):
        self.state.mkdir(parents=True)
        (self.state/'STOP').touch()
        self.assertEqual(self.invoke()[0],0)
        self.assertEqual(self.calls(),[])
        self.manifest.mode='shadow'
        with self.assertRaisesRegex(ManifestError,'shadow_mode_read_only'):
            self.invoke()

    def test_plist_explicit_absolute_path(self):
        self.manifest.launchd.path=[str(Path.home()/'.local/bin'),'/usr/bin','/bin']
        with contextlib.redirect_stdout(io.StringIO()):
            loop.install(self.manifest,'audit')
        target = next((self.manifest.paths.root/'launchd').glob('*.plist'))
        template = plistlib.loads(target.read_bytes())
        self.assertEqual(template['StartInterval'],1800)
        self.assertEqual(template['EnvironmentVariables']['PATH'],':'.join(self.manifest.launchd.path))
        self.assertEqual(template['ProgramArguments'][-3:],['loop','run','audit'])
        self.assertEqual(template['Label'],'com.coordkit.sample.loop.audit')
        self.assertTrue(Path(template['ProgramArguments'][0]).is_absolute())

    def test_overrides_and_template(self):
        template = self.root/'prompt.md'
        template.write_text('Audit @PROJECT@ @SNAPSHOT@ @PRIOR@ @EXTRACT@')
        self.manifest.loop.loops={'audit':{'default_cadence':'1h','prompt_template':'prompt.md'}}
        code,out,err=self.invoke()
        self.assertEqual(code,0,err)
        prompt=next(self.state.glob('runs/*/prompt.md')).read_text()
        self.assertIn('Audit sample',prompt)
        self.assertNotIn('@SNAPSHOT@',prompt)
        self.assertIn('snapshot-data',prompt)
        self.assertEqual(loop.config(self.manifest,'audit')[0].default_cadence,'1h')

    def test_failed_cycle_preserves_transcript_checkpoint(self):
        transcript = self.root/'transcript.jsonl'
        transcript.write_text(json.dumps({'type':'user','message':{'content':'hello'}})+'\n')
        self.manifest.loop.transcript='transcript.jsonl'
        self.respond({'tool':'agentctl','contains':['delegate','--wait'],'stdout':'missing block'})
        self.assertEqual(self.invoke()[0],1)
        self.assertEqual(list((self.state/'offsets').glob('*.json')),[])
        self.respond()
        self.assertEqual(self.invoke()[0],0)
        self.assertEqual(len(list((self.state/'offsets').glob('*.json'))),1)

    def test_bad_plan_permissions_prevent_launch(self):
        self.respond({'tool':'agentctl','contains':['--plan'],
                      'json':{'ok':True,'result':{'native':{'permissions':'full'}}}})
        code,out,err=self.invoke()
        self.assertEqual(code,1)
        self.assertIn('plan_refused',err)
        self.assertFalse(any('--wait' in c['args'] for c in self.calls()))

    def test_no_lead_transcript_heuristic(self):
        source=Path(loop.__file__).read_text()
        self.assertNotIn('lead_transcript',source)
        self.assertNotIn('grep',source)
        with self.assertRaises(ManifestError):
            loop.config(self.manifest,'../escape')


class AuditorTests(FakeCase):
    def test_last_block_and_five_items(self):
        first='MISSED_JSON_START {"items":[{"title":"old"}]} MISSED_JSON_END'
        last='MISSED_JSON_START '+json.dumps({'items':[{'title':str(i)} for i in range(8)]})+' MISSED_JSON_END'
        self.assertEqual([i['title'] for i in auditor.find_items(first+last)],list('01234'))
        envelope=json.dumps({'content':[{'text':first},{'text':last}]})
        self.assertEqual(len(auditor.find_items(envelope)),5)

    def test_bad_items_and_block(self):
        for raw in ('nothing','MISSED_JSON_START {"items":{}} MISSED_JSON_END',
                    'MISSED_JSON_START {broken} MISSED_JSON_END'):
            with self.subTest(raw=raw),self.assertRaises(ManifestError):
                auditor.find_items(raw)

    def test_message_budget_long_path(self):
        message=auditor.message([{'title':'x'*1000,'severity':'high'}],Path('/'+'x'*1000))
        self.assertLessEqual(len(message),380)

    def test_redaction_and_tool_results(self):
        text='person@example.com sk-abcdefghijklmno <system-reminder>hidden</system-reminder>'
        self.assertEqual(clean(text),'[email] [secret]')
        self.transcript=self.root/'t.jsonl'
        self.transcript.write_text(json.dumps({'type':'assistant','message':{'content':[
            {'type':'text','text':text},{'type':'tool_result','content':'sensitive-result'},
            {'type':'tool_use','input':{'description':'check status'}}]}})+'\n')
        result=extract(self.transcript,self.root/'offset')
        self.assertNotIn('sensitive-result',result.text)
        self.assertIn('check status',result.text)
        self.assertNotIn('person@',result.text)

    def test_forward_offset_no_skipped_line_partial_and_truncation(self):
        transcript=self.root/'t.jsonl'
        checkpoint=self.root/'offset'
        def line(text):
            return json.dumps({'type':'user','message':{'content':text}})+'\n'
        transcript.write_text(line('first'))
        commit(checkpoint,extract(transcript,checkpoint))
        with transcript.open('a') as stream:
            stream.write(line('second')+line('third')[:-1])
        result=extract(transcript,checkpoint)
        self.assertIn('second',result.text)
        self.assertNotIn('third',result.text)
        commit(checkpoint,result)
        with transcript.open('a') as stream:
            stream.write('\n')
        result=extract(transcript,checkpoint)
        self.assertIn('third',result.text)
        self.assertNotIn('second',result.text)
        commit(checkpoint,result)
        transcript.write_text(line('short'))
        self.assertIn('short',extract(transcript,checkpoint).text)


class LoopScopeTests(FakeCase):
    def test_snapshot_keeps_only_this_projects_executions(self):
        req=self.manifest.paths.out_dir/'req'; req.mkdir(parents=True)
        (req/'w.plan.json').write_text(json.dumps({'label':'w1'}))
        self.respond({'tool':'agentctl','contains':['recent'],'json':{'ok':True,'result':{'executions':[
                         {'id':'exec-other-lane','labels':['worker-other']},{'id':'exec-ours','labels':['w1']},
                         {'id':'exec-card','labels':['anx.card.task']}]}}},
                     {'tool':'anx','contains':['cards','list'],'json':{'ok':True,'result':{'cards':[{'handle':'task'}]}}},
                     {'tool':'gh','stdout':'[]'})
        text=loop.snapshot(self.manifest)
        self.assertIn('exec-ours',text); self.assertIn('exec-card',text); self.assertNotIn('exec-other-lane',text)


class AuditNoiseTests(FakeCase):
    invoke = LoopTests.invoke
    state = LoopTests.state

    def test_unchanged_cycle_writes_findings_and_receipt_without_bus_row(self):
        self.assertEqual(self.invoke()[0], 0)
        before = self.manifest.paths.bus_log.read_text()
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.manifest.paths.bus_log.read_text(), before)
        reports = list(self.state.glob('runs/*/findings.md'))
        self.assertEqual(len(reports), 2)
        self.assertTrue(any('Nothing raised' in p.read_text() for p in reports))

    def test_multiple_changed_findings_publish_one_summary_with_evidence_link(self):
        items = [{'title': 'a', 'severity': 'high', 'kind': 'risk', 'evidence': 'ref1', 'next_action': 'act'},
                 {'title': 'b', 'severity': 'med', 'kind': 'followup', 'evidence': 'ref2', 'next_action': 'check'}]
        self.respond({'tool': 'agentctl', 'contains': ['delegate', '--wait'],
                      'stdout': 'MISSED_JSON_START ' + json.dumps({'items': items}) + ' MISSED_JSON_END'})
        self.assertEqual(self.invoke()[0], 0)
        rows = [json.loads(s) for s in self.manifest.paths.bus_log.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertIn('2 new finding(s)', rows[0]['text'])
        self.assertIn('findings.md', rows[0]['text'])
        self.assertIn(rows[0]['evidence'], rows[0]['text'])
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(len(self.manifest.paths.bus_log.read_text().splitlines()), 1)
        items[0]['evidence'] = 'new-ref'
        self.respond({'tool': 'agentctl', 'contains': ['delegate', '--wait'],
                      'stdout': 'MISSED_JSON_START ' + json.dumps({'items': items}) + ' MISSED_JSON_END'})
        self.assertEqual(self.invoke()[0], 0)
        rows = [json.loads(s) for s in self.manifest.paths.bus_log.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertIn('1 new finding(s)', rows[-1]['text'])

    def test_legacy_prior_lines_are_a_valid_baseline(self):
        item = {'title': 'Same finding', 'severity': 'med', 'kind': 'risk', 'next_action': 'Fix it', 'evidence': 'ref'}
        self.assertEqual(auditor.new_items([item], auditor.prior_line(item)), [])
        self.assertEqual(auditor.new_items([dict(item, next_action='New action')], auditor.prior_line(item)),
                         [dict(item, next_action='New action')])

    def test_failed_receipt_retry_keeps_original_summary_and_comparison(self):
        self.respond({'tool': 'anx', 'contains': ['note'], 'exit': 2, 'stderr': 'receipt refused'})
        self.assertEqual(self.invoke()[0], 1)
        pending = json.loads((self.state/'pending.json').read_text())
        self.assertTrue(pending['compared_prior'])
        self.respond()
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(len(self.manifest.paths.bus_log.read_text().splitlines()), 1)
