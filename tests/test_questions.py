"""Question capture, authority escalation, bounded hooks and answer delivery."""
import contextlib
import io
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from coordkit import addressed, hooks, questions
from coordkit.anxcli import AnxClient
REAL_ASK = AnxClient.ask  # captured before any test patches it
# An authored decision brief: tests of the ANX path file their question with one.
BRIEF = {'title': 'Rerun the lifecycle gate on the staging fixtures?',
         'situation': 'The gate failed once on a flaky fixture. A rerun takes ten minutes.',
         'impact': 'The lane waits for the gate before it can merge.',
         'options': [{'action': 'Rerun it', 'consequence': 'The lane learns in ten minutes whether it can merge.', 'reversible': True},
                     {'action': 'Skip it', 'consequence': 'The lane merges without the gate.', 'reversible': False}],
         'recommendation': 'Rerun it', 'why': 'The failure looks like a flaky fixture.',
         'if_no_answer': 'Nothing happens: the lane keeps waiting.'}
from coordkit.manifest import load
from tests.test_addressed import AddressedCase, ts

QUESTIONS = '''
[questions]
human_actor = "operator"
fallback_role = "coordinator"
config_dirs = ["@CLAUDE2@"]
delivery_cli = "@DELIVERY_CLI@"
'''

MODAL = {'questions': [{'question': 'May I re-run the fenced repro gate on the staging fixtures?', 'header': 'Repro',
                        'multiSelect': False, 'options': [{'label': 'Re-run it (Recommended)', 'description': 'x'},
                                                          {'label': 'Skip', 'description': 'y'}]}]}


class QuestionCase(AddressedCase):
    def setUp(self):
        super().setUp()
        self.claude2 = self.root/'claude-2'
        (self.claude2/'projects'/'-work').mkdir(parents=True)
        self.claude2_settings = self.claude2/'settings.json'
        self.claude2_settings.write_text(json.dumps({'hooks': {
            'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'agent-heartbeat touch'}]}],
            'PostToolUse': [{'hooks': [{'type': 'command', 'command': 'agent-heartbeat touch'}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': 'agent-heartbeat touch'}]}]},
            'statusLine': {'type': 'command', 'command': 'fleet-status'}}, indent=1))
        self.delivery_cli_log = self.root/'delivery_cli.jsonl'
        delivery_cli = self.root/'delivery_cli'
        delivery_cli.write_text(f'#!/usr/bin/env python3\nimport json,sys\nopen({str(self.delivery_cli_log)!r},"a").write(json.dumps(sys.argv[1:])+"\\n")\n')
        delivery_cli.chmod(0o755)
        text = self.file.read_text().replace('[addressed.agents.worker-ui]\nruntime = "codex"\nadapter = "tmux"\nfallback = "lead-a"',
                                             '[addressed.agents.worker-ui]\nruntime = "claude-code"\nfallback = "lead-a"\nremote = "worker-statemachines"')
        self.file.write_text(text + QUESTIONS.replace('@CLAUDE2@', str(self.claude2)).replace('@DELIVERY_CLI@', str(delivery_cli)))
        self.manifest = load(self.file)
        self.anx_calls = []
        ask = patch.object(AnxClient, 'ask', lambda client, card, brief: self.anx_calls.append(('ask', card, brief)) or {'ask_id': 'ask-7'})
        call = patch.object(AnxClient, 'call', lambda client, *args, **kw: self.anx_calls.append(('call',) + args) or {})
        ask.start(); call.start(); self.addCleanup(ask.stop); self.addCleanup(call.stop)

    def transcript(self, name, rows):
        path = self.claude2/'projects'/'-work'/f'{name}.jsonl'
        path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        return path

    def user(self, text, at=None):
        return {'type': 'user', 'timestamp': ts(at or self.now - 3600), 'message': {'role': 'user', 'content': text}}

    def assistant_text(self, text, at=None):
        return {'type': 'assistant', 'timestamp': ts(at or self.now), 'message': {'role': 'assistant',
                'content': [{'type': 'text', 'text': text}]}}

    def hook(self, event, payload):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            questions.run_hook(self.manifest, event, str(self.claude2), stdin=json.dumps(payload))
        return json.loads(out.getvalue()) if out.getvalue().strip() else None

    def records(self):
        return questions.load_all(self.manifest)


class QuestionTests(QuestionCase):
    # -- the six required tests ----------------------------------------------------------------------
    def test_q1_nonlead_modal_becomes_bus_ask_and_denied_on_claude2(self):
        self.lease('lead-a')
        path = self.transcript('sess-1', [self.user('Your bus name is **worker-ui**. Read the brief.')])
        decision = self.hook('pretooluse', {'session_id': 'sess-1', 'transcript_path': str(path), 'cwd': '/work',
                                            'tool_name': 'AskUserQuestion', 'tool_input': MODAL})
        out = decision['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        record = next(iter(self.records().values()))
        self.assertIn(record['qid'], out['permissionDecisionReason'])
        self.assertEqual((record['asker'], record['tier'], record['target'], record['recommendation']),
                         ('worker-ui', 1, 'lead-a', 'Re-run it (Recommended)'))
        ask = next(r for r in self.rows() if r['id'] == record['qid'])
        self.assertEqual((ask['kind'], ask['frm'], ask['to']), ('ask', 'worker-ui', 'lead-a'))
        # A non-AskUserQuestion tool and a broken payload are untouched (fail open).
        self.assertIsNone(self.hook('pretooluse', {'tool_name': 'Bash', 'tool_input': {}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            questions.run_hook(self.manifest, 'pretooluse', str(self.claude2), stdin='{not json')
        self.assertEqual(out.getvalue(), '')
        # Hooks install into the claude-2 config dir (heartbeat-only today) and roll back exactly.
        original = json.loads(self.claude2_settings.read_text())
        self.assertEqual(hooks.pending_question_installs(self.manifest), [str(self.claude2)])
        backup = self.root/'settings-backup.json'
        backup.write_bytes(self.claude2_settings.read_bytes())
        hooks.install_questions(self.manifest, str(self.claude2), backup)
        self.assertEqual(hooks.question_drift(self.manifest), [])
        installed = json.loads(self.claude2_settings.read_text())
        self.assertEqual(installed['hooks']['PreToolUse'][0]['matcher'], 'AskUserQuestion')
        self.assertEqual(installed['statusLine'], original['statusLine'])
        hooks.restore_questions(self.manifest)
        self.assertEqual(json.loads(self.claude2_settings.read_text()), original)

    def test_q2_stop_prose_question_without_waiter_posts_ask_and_flags_it(self):
        self.lease('lead-a')
        path = self.transcript('sess-2', [self.user('Your bus name is worker-ui.'), self.assistant_text(
            'Built the probe and the report.\n\nDecision for you: should I go ahead with the backfill once lead-a approves?')])
        self.hook('stop', {'session_id': 'sess-2', 'transcript_path': str(path), 'stop_hook_active': False})
        record = next(iter(self.records().values()))
        self.assertEqual((record['capture'], record['asker'], record['tier'], record['target']), ('stop', 'worker-ui', 1, 'lead-a'))
        self.assertFalse((addressed.home(self.manifest)/'events.ndjson').exists())

    def test_q2b_stop_hook_ignores_listening_agents_reports_and_continuations(self):
        self.lease('lead-a'); self.lease('worker-ui')
        path = self.transcript('sess-3', [self.user('Your bus name is worker-ui.'),
                                          self.assistant_text('Done.\n\nShould I go ahead with the backfill?')])
        self.hook('stop', {'session_id': 'sess-3', 'transcript_path': str(path)})  # waiter armed: it will hear an answer
        report = self.transcript('sess-4', [self.user('Your bus name is worker-remote.'),
                                            self.assistant_text('Merged #12. Next I will rebase #13.')])
        self.hook('stop', {'session_id': 'sess-4', 'transcript_path': str(report)})
        self.hook('stop', {'session_id': 'sess-5', 'transcript_path': str(path), 'stop_hook_active': True})
        self.assertEqual(self.records(), {})

    def test_q3_agent_ask_unanswered_past_deadline_escalates_to_anx_with_recommendation(self):
        self.lease('lead-a', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-ui', 'Approve rerunning the lifecycle gate?',
                                     recommendation='Rerun it', brief=BRIEF, session_id='s', at=self.now)
        self.assertEqual(record['tier'], 1)
        self.assertEqual(questions.ladder(self.manifest, at=self.now + 29*60), [])
        lines = questions.ladder(self.manifest, at=self.now + 31*60)
        self.assertTrue(lines[0].startswith(f'QUESTION_ESCALATED {record["qid"]} tier=2'))
        kind, card, kw = self.anx_calls[0]
        self.assertEqual((kind, card, kw['recommendation']), ('ask', 'card:task', 'Rerun it'))
        self.assertIn(record['qid'], kw['details'])  # ids live in the collapsed details, never the body
        self.assertNotIn(record['qid'], kw['situation'] + kw['title'])
        after = self.records()[record['qid']]
        self.assertEqual((after['tier'], after['anx_ask_id']), (2, 'ask-7'))
        hop = [r for r in self.rows() if r.get('escalation') == 'question_escalated tier=2']
        self.assertEqual((hop[0]['to'], hop[0]['re']), ('worker-ui', record['qid']))
        self.assertTrue(any('QUESTION tier=2' in l and 'anx=ask-7' in l for l in self.relay_lines()))

    def test_q4_host_change_question_skips_tier1_to_anx(self):
        self.lease('lead-a')
        record, _ = questions.create(self.manifest, 'service-agent',
                                     "May I rehearse the host configuration now? It is a host change (launchd loads on the pilot).",
                                     recommendation='Yes, rehearse on the pilot', brief=BRIEF, session_id='s')
        self.assertEqual((record['tier'], record['guarded']), (2, True))
        self.assertFalse([r for r in self.rows() if r['to'] == 'lead-a' and r['kind'] == 'ask'])
        self.assertEqual(self.anx_calls[0][0], 'ask')
        self.assertIn('a lead cannot approve it', record['tier2_why'])

    def test_q5_sweep_finds_a_pending_modal_in_a_transcript_without_hooks(self):
        self.lease('lead-a', heartbeat_ago=-7200)
        use = {'type': 'assistant', 'sessionId': 'sess-6', 'timestamp': ts(self.now - 11*60), 'message': {'role': 'assistant',
               'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'AskUserQuestion', 'input': MODAL}]}}
        self.transcript('sess-6', [self.user('Your bus name is worker-ui.'), use])
        found = questions.scan_transcripts(self.manifest, at=self.now)
        self.assertEqual(len(found), 1)
        record = next(iter(self.records().values()))
        self.assertEqual((record['capture'], record['asker'], record['tier']), ('sweep', 'worker-ui', 1))
        self.assertEqual(questions.scan_transcripts(self.manifest, at=self.now + 60), [])  # no duplicate
        answered = {'type': 'user', 'timestamp': ts(self.now), 'message': {'role': 'user', 'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_2', 'content': 'answered'}]}}
        use2 = dict(use, sessionId='sess-7', message={'role': 'assistant', 'content': [dict(use['message']['content'][0], id='toolu_2')]})
        self.transcript('sess-7', [use2, answered])
        self.assertEqual(questions.scan_transcripts(self.manifest, at=self.now + 120), [])
        fresh = dict(use, sessionId='sess-8', timestamp=ts(self.now))  # younger than modal_min_age
        self.transcript('sess-8', [fresh])
        self.assertEqual(questions.scan_transcripts(self.manifest, at=self.now + 120), [])

    def test_q6_answer_reaches_a_deaf_asker_and_closes_bus_and_anx(self):
        self.lease('lead-a', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-ui', 'Approve rerunning the lifecycle gate?',
                                     recommendation='Rerun it', brief=BRIEF, session_id='s', at=self.now)
        questions.ladder(self.manifest, at=self.now + 31*60)  # now also open in anx
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            questions.answer(self.manifest, record['qid'], 'lead-a', 'PROCEED: rerun the gate | rollback: none needed')
        closed = self.records()[record['qid']]
        self.assertEqual((closed['state'], closed['answered_by'], closed['delivered_via']), ('closed', 'lead-a', 'delivery_cli'))
        decision = [r for r in self.rows() if r['kind'] == 'decision' and r.get('re') == record['qid']]
        self.assertEqual((decision[0]['frm'], decision[0]['to']), ('lead-a', 'worker-ui'))
        sent = json.loads(self.delivery_cli_log.read_text().splitlines()[0])
        self.assertEqual(sent[:2], ['send', 'worker-statemachines'])
        self.assertIn(('call', 'ask', 'withdraw', 'event:ask-7', '--reason', 'answered on the bus by lead-a'), self.anx_calls)
        with contextlib.redirect_stdout(io.StringIO()) as again:
            questions.answer(self.manifest, record['qid'], 'coordinator', 'too late')
        self.assertIn('already closed by lead-a', again.getvalue())

    # -- supporting behaviour ------------------------------------------------------------------------
    def test_human_answers_in_anx_and_the_asker_hears_it(self):
        self.lease('lead-a', heartbeat_ago=-7200); self.lease('worker-ui', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-ui', 'Delete the old staging bucket?', recommendation='Yes', brief=BRIEF, session_id='s')
        self.assertEqual(record['tier'], 2)
        questions.ladder(self.manifest, at=self.now + 60, anx_answers={'ask-7': {'text': 'yes, delete it'}})
        closed = self.records()[record['qid']]
        self.assertEqual((closed['state'], closed['answered_by'], closed['delivered_via']), ('closed', 'operator', 'waiter'))
        relayed = [r for r in self.rows() if r['kind'] == 'decision' and r['frm'] == 'coordkit']
        self.assertIn('yes, delete it', relayed[0]['text'])
        self.assertFalse([c for c in self.anx_calls if 'withdraw' in c])

    def test_bus_reply_from_the_target_closes_the_question(self):
        self.lease('lead-a', heartbeat_ago=-7200); self.lease('worker-ui', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-ui', 'Rerun the lifecycle gate?', recommendation='Yes', brief=BRIEF, session_id='s')
        self.incumbent_row('r1', 'lead-a', 'worker-ui', kind='answer', text='yes', ago=0, re=record['qid'])
        self.assertEqual(questions.ladder(self.manifest)[0], f'QUESTION_ANSWERED {record["qid"]} by lead-a')


    def test_kill_switch_shadow_mode_and_dedupe(self):
        self.lease('lead-a')
        payload = {'session_id': 'sess-9', 'tool_name': 'AskUserQuestion', 'tool_input': MODAL}
        identity = patch.dict(os.environ, COORDKIT_AS='worker-ui'); identity.start(); self.addCleanup(identity.stop)
        with patch.dict(os.environ, COORDKIT_QUESTION_GUARD='off'):
            self.assertIsNone(self.hook('pretooluse', payload))
        self.assertTrue(self.hook('pretooluse', payload))
        self.assertTrue(self.hook('pretooluse', payload))
        self.assertEqual(len(self.records()), 1, 'same session and question: one record')
        self.file.write_text(self.file.read_text().replace('[project]', 'mode = "shadow"\n[project]', 1))
        self.manifest = load(self.file)
        self.assertIsNone(self.hook('pretooluse', dict(payload, session_id='sess-10')))

    def test_status_line_counts_open_questions_over_30_minutes(self):
        self.lease('lead-a')
        questions.create(self.manifest, 'worker-ui', 'Rerun?', recommendation='Yes', brief=BRIEF, session_id='s', at=self.now - 40*60)
        questions.create(self.manifest, 'worker-ui', 'Rebase?', recommendation='Yes', brief=BRIEF, session_id='s', at=self.now)
        self.assertEqual(questions.status_line(self.manifest, at=self.now), 'QUESTIONS open=2 over30m=1 oldest=40m')

    def test_prose_detector(self):
        for text, want in (('Done.\n\nDecision for you: merge now?', True), ('Should I rebase both?', True),
                           ('Done. Want me to page you when it lands?', True), ('How does argparse parse this? It splits on spaces.', False),
                           ('Merged #12; next I rebase #13.', False), ('Does the cache work? Yes: 3 hits.', False)):
            with self.subTest(text=text):
                self.assertEqual(questions.prose_question(text), want)

    def test_configured_human_prose_is_case_insensitive_and_regex_escaped(self):
        actor = 'alex'
        for preposition in ('on', 'for'):
            self.assertTrue(questions.prose_question(f"I'm waiting {preposition} {actor.title()} to approve this", actor))
        self.assertTrue(questions.prose_question(f'Waiting for {actor.title()}.', actor))
        self.assertFalse(questions.prose_question(f'Waiting for {actor}.other to approve', actor))
        self.assertFalse(questions.prose_question('Waiting for somebody else to approve', actor))
        self.assertFalse(questions.prose_question('Waiting for operator-extra to approve', 'operator'))
        self.assertTrue(questions.prose_question('Waiting for human.a+ to approve', 'human.a+'))
        self.assertFalse(questions.prose_question('Waiting for humanXa to approve', 'human.a+'))
        self.assertFalse(questions.prose_question('Waiting for anyone to approve', '.*'))

    def test_stop_hook_captures_waiting_for_the_configured_human(self):
        self.lease('lead-a')
        actor = 'alex'
        self.manifest.questions.human_actor = actor
        path = self.transcript('human-wait', [self.user('Your bus name is worker-ui.'),
            self.assistant_text(f"Report complete.\n\nI'm waiting for {actor.title()} to approve this")])
        self.hook('stop', {'session_id':'human-wait', 'transcript_path':str(path)})
        record = next(iter(self.records().values()))
        self.assertEqual((record['capture'], record['asker']), ('stop', 'worker-ui'))

    def test_identity_binding_order(self):
        path = self.transcript('sess-11', [self.user('Your bus name is **worker-search** (not worker-old).')])
        self.assertEqual(questions.asker_for(self.manifest, 'sess-11', str(path), env={}), ('worker-search', 'transcript'))
        self.assertEqual(questions.asker_for(self.manifest, 'sess-11', str(path), env={'COORDKIT_AS': 'x'}), ('x', 'env'))
        self.assertEqual(questions.asker_for(self.manifest, 'abcdefgh123', None, env={}), ('unknown-abcdefgh', 'unknown'))


class QuestionReviewTests(QuestionCase):
    """Question capture and delivery regressions."""
    def test_r1_hook_bootstrap_failure_never_blocks(self):
        from coordkit import cli
        for manifest in ('/nonexistent/coordkit.toml', str(self.root/'bad.toml')):
            (self.root/'bad.toml').write_text('not = [toml')
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), \
                    patch('sys.stdin', io.StringIO(json.dumps({'tool_name': 'AskUserQuestion', 'tool_input': MODAL}))):
                self.assertEqual(cli.main(['--manifest', manifest, 'question', 'hook', 'pretooluse']), 0)
            self.assertEqual(out.getvalue(), '')

    def test_r2_anx_outage_keeps_the_modal_and_retries(self):
        def down(client, card, brief):
            raise RuntimeError('anx workspace_unavailable')
        payload = {'session_id': 's1', 'tool_name': 'AskUserQuestion', 'tool_input': {'questions': [
            {'question': 'May I deploy #123 to prod?', 'options': [{'label': 'Deploy (Recommended)'}]}]}}
        identity = patch.dict(os.environ, COORDKIT_AS='worker-ui'); identity.start(); self.addCleanup(identity.stop)
        self.assertIsNone(self.hook('pretooluse', payload), 'no live tier 1, no brief: no durable post, the modal stays')
        # A briefed guarded question while anx is down: stays at its tier, retries on the next sweep.
        with patch.object(AnxClient, 'ask', down):
            record, _ = questions.create(self.manifest, 'worker-ui', 'May I deploy #123 to prod?', brief=BRIEF,
                                         session_id='s2')
        record = self.records()[record['qid']]
        self.assertEqual((record['tier'], record['anx_ask_id']), (0, None))
        self.assertTrue(record['anx_failed_at'])
        lines = questions.ladder(self.manifest, at=time.time() + 301)
        self.assertIn('tier=2', lines[0])
        self.assertEqual(self.records()[record['qid']]['anx_ask_id'], 'ask-7')

    def test_r3_question_drift_checks_matcher_and_exact_command(self):
        self.assertEqual(hooks.pending_question_installs(self.manifest), [str(self.claude2)])
        self.assertEqual(hooks.question_drift(self.manifest), [], 'nothing installed and coordkit not up yet')
        backup = self.root/'b.json'; backup.write_bytes(self.claude2_settings.read_bytes())
        hooks.install_questions(self.manifest, str(self.claude2), backup)
        self.assertEqual((hooks.pending_question_installs(self.manifest), hooks.question_drift(self.manifest)), ([], []))
        doc = json.loads(self.claude2_settings.read_text())
        doc['hooks']['PreToolUse'][0]['matcher'] = 'Bash'
        self.claude2_settings.write_text(json.dumps(doc))
        self.assertTrue(any('PreToolUse matcher=AskUserQuestion' in p for p in hooks.question_drift(self.manifest)))

    def test_r4_a_receipt_is_not_an_answer(self):
        self.lease('lead-a', heartbeat_ago=-7200); self.lease('worker-ui', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-ui', 'Rerun the lifecycle gate?', recommendation='Yes', brief=BRIEF, session_id='s')
        self.incumbent_row('k1', 'lead-a', 'worker-ui', kind='answer', text='received', ago=0, re=record['qid'])
        self.incumbent_row('k2', 'lead-a', 'worker-ui', kind='answer', text='received', ago=0, re=record['qid'], ack='received')
        self.assertEqual(questions.ladder(self.manifest), [])
        self.incumbent_row('k3', 'lead-a', 'worker-ui', kind='answer', text='done: yes, rerun it', ago=0, re=record['qid'], ack='done')
        self.assertEqual(questions.ladder(self.manifest), [f'QUESTION_ANSWERED {record["qid"]} by lead-a'])

    def test_r5_unknown_asker_goes_to_the_relay_and_undelivered_answers_stay_pending(self):
        self.lease('relay', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'unknown-abcd1234', 'Rerun the gate?', session_id='abcd1234')
        self.assertEqual(record['target'], 'relay')
        self.file.write_text(self.file.read_text().replace(f'delivery_cli = "{self.root/"delivery_cli"}"', 'delivery_cli = ""'))
        self.manifest = load(self.file)
        with contextlib.redirect_stdout(io.StringIO()):
            questions.answer(self.manifest, record['qid'], 'relay', 'yes')
        self.assertEqual(self.records()[record['qid']]['state'], 'answered', 'deaf asker, no adapter: not closed')
        self.assertTrue(any('QUESTION_ANSWER_UNDELIVERED' in l for l in self.relay_lines()))
        self.lease('unknown-abcd1234', heartbeat_ago=-7200)  # the asker re-arms its waiter
        self.assertEqual(questions.ladder(self.manifest), [f'QUESTION_DELIVERED {record["qid"]} via waiter'])
        self.assertEqual(self.records()[record['qid']]['state'], 'closed')

    def test_r6_shadow_sweeps_never_deliver_or_page(self):
        record, _ = questions.create(self.manifest, 'worker-ui', 'Ship #1 now?', recommendation='Yes', brief=BRIEF, session_id='s', urgent=True)
        self.file.write_text(self.file.read_text().replace('[project]', 'mode = "shadow"\n[project]', 1))
        self.manifest = load(self.file)
        how = questions.close(self.manifest, questions.load_all(self.manifest)[record['qid']], 'lead-a', 'yes')
        self.assertEqual(how, 'shadow')
        self.assertTrue(any('QUESTION_ANSWER (shadow)' in l for l in self.relay_lines()))
        questions.ladder(self.manifest, at=time.time() + 3*3600)
        self.assertFalse(self.delivery_cli_log.exists())
        self.assertEqual(self.records()[record['qid']]['state'], 'answered')

    def test_r7_only_permission_notifications_count(self):
        # Idle is not a question; an idle lane on its waiter, or the operator's own chat, sends idle_prompt.
        self.hook('notification', {'session_id': 'n1', 'notification_type': 'auth_success', 'message': 'Signed in'})
        self.hook('notification', {'session_id': 'n2', 'notification_type': 'idle_prompt', 'message': 'Claude is waiting for your input'})
        self.hook('notification', {'session_id': 'n3', 'notification_type': 'permission_prompt', 'message': 'Claude needs your permission to use Bash'})
        self.assertEqual(sorted(p.stem for p in (questions.home(self.manifest)/'notifications').glob('*.json')), ['n3'])

    def test_r8_guarded_is_about_acting_on_a_guarded_object(self):
        for text, want in (('Should I switch the active host to host-a?', True), ("What's our fleet status?", False),
                           ('May I load the launchd job on the pilot?', True), ('Can I merge #123 now?', True),
                           ('How many PRs merged today?', False), ('Should I rerun the unit tests?', False),
                           ('Report: deploy finished. [human-only] confirm the price change?', True)):
            with self.subTest(text=text):
                self.assertEqual(questions.guarded(self.manifest, text)[0], want)

    def test_r9_routed_ask_carries_options_and_recommendation(self):
        self.lease('lead-a')
        with patch.dict(os.environ, COORDKIT_AS='worker-ui'):
            self.hook('pretooluse', {'session_id': 's9', 'tool_name': 'AskUserQuestion', 'tool_input': MODAL})
        ask = next(r for r in self.rows() if r['kind'] == 'ask')
        self.assertIn('rec: Re-run it (Recommended)', ask['text'])
        self.assertIn('options: Re-run it (Recommended) / Skip', ask['text'])

    def test_r10_concurrent_sweeps_escalate_once(self):
        self.lease('lead-a', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-ui', 'Rerun?', recommendation='Yes', brief=BRIEF, session_id='s', at=self.now)
        at = self.now + 31*60
        self.assertTrue(questions.claim(self.manifest, record['qid'], 'tier2', at, lambda r: True))  # another sweep holds it
        self.assertEqual(questions.ladder(self.manifest, at=at), [])
        self.assertEqual([c for c in self.anx_calls if c[0] == 'ask'], [])

    def test_r2_1_up_upgrades_an_existing_install(self):
        from coordkit import lifecycle
        (self.manifest.paths.root/'lifecycle.json').parent.mkdir(parents=True, exist_ok=True)
        (self.manifest.paths.root/'lifecycle.json').write_text(json.dumps({'jobs': [{'label': 'x', 'file': '/nonexistent'}]}))
        self.assertTrue(hooks.question_drift(self.manifest), 'up and missing: doctor reports drift')
        self.assertEqual(hooks.question_drift(self.manifest, include_pending=False), [], 'up --apply is not blocked')
        self.assertEqual(lifecycle.installation_drift.__defaults__, (True,))

    def test_r2_2_persistent_anx_outage_reaches_a_live_agent(self):
        self.file.write_text(self.file.read_text().replace('relay_file = "pending-alerts.ndjson"\n', ''))
        self.manifest = load(self.file)
        self.lease('lead-a', heartbeat_ago=-7200)
        def down(client, card, brief):
            raise RuntimeError('anx down')
        with patch.object(AnxClient, 'ask', down):
            record, _ = questions.create(self.manifest, 'worker-ui', 'May I deploy #123 to prod?', recommendation='Yes', brief=BRIEF, session_id='s')
            questions.ladder(self.manifest, at=time.time() + 301)  # retry also fails: no second alert
        after = self.records()[record['qid']]
        self.assertEqual(after['outage_reached'], ['lead-a'])
        self.assertEqual(len([r for r in self.rows() if r.get('to') == 'lead-a' and 'anx is down' in r['text']]), 1)

    def test_r2_3_same_prompt_with_other_options_is_a_new_question(self):
        self.lease('lead-a')
        identity = patch.dict(os.environ, COORDKIT_AS='worker-ui'); identity.start(); self.addCleanup(identity.stop)
        self.hook('pretooluse', {'session_id': 's', 'tool_name': 'AskUserQuestion', 'tool_input': MODAL})
        other = {'questions': [dict(MODAL['questions'][0], options=[{'label': 'Only the subset (Recommended)'}, {'label': 'Skip'}])]}
        decision = self.hook('pretooluse', {'session_id': 's', 'tool_name': 'AskUserQuestion', 'tool_input': other})
        self.assertEqual(len(self.records()), 2)
        qid = [q for q, r in self.records().items() if 'subset' in r['recommendation']][0]
        self.assertIn(qid, decision['hookSpecificOutput']['permissionDecisionReason'])

    def test_r2_4_undelivered_answer_stays_visible(self):
        self.file.write_text(self.file.read_text().replace('relay_file = "pending-alerts.ndjson"\n', '')
                             .replace(f'delivery_cli = "{self.root/"delivery_cli"}"', 'delivery_cli = ""'))
        self.manifest = load(self.file)
        self.lease('lead-a', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-remote', 'Rerun?', recommendation='Yes', brief=BRIEF, session_id='s')
        with contextlib.redirect_stdout(io.StringIO()):
            questions.answer(self.manifest, record['qid'], 'lead-a', 'yes')
        self.assertEqual(self.records()[record['qid']]['state'], 'answered')
        self.assertIn(record['qid'], [r['qid'] for r in questions.open_questions(self.manifest)])
        self.assertIn('open=1', questions.status_line(self.manifest))

    def test_r2_5_host_moves_are_guarded_and_read_only_checks_are_not(self):
        for text, want in (('May I move the worker from host-a to host-b?', True), ('May I check our fleet status?', False),
                           ('Should I migrate the bus waiter to another machine?', True), ('Can I look at the launchd logs?', False),
                           ('May I review #123 and then merge it?', True)):
            with self.subTest(text=text):
                self.assertEqual(questions.guarded(self.manifest, text)[0], want)


    def test_r2_8_hook_deadline_interrupts_a_slow_tool_call(self):
        import subprocess, sys as _sys
        self.assertLess(questions.HOOK_DEADLINE, 5)
        child = self.root/'slow.py'
        child.write_text('import time\ntime.sleep(30)\n')
        code = (f'import signal,sys; sys.path.insert(0, {str(Path(questions.__file__).parents[1])!r});'
                f'from coordkit.process import run\n'
                f'def overrun(s,f): raise TimeoutError()\n'
                f'signal.signal(signal.SIGALRM, overrun); signal.alarm(1)\n'
                f'try:\n    run([sys.executable, {str(child)!r}], timeout=60)\nexcept TimeoutError:\n    print("interrupted")\n')
        out = subprocess.run([_sys.executable, '-c', code], capture_output=True, text=True, timeout=20)
        self.assertIn('interrupted', out.stdout)
        ps = subprocess.run(['ps', '-axo', 'args='], capture_output=True, text=True).stdout
        self.assertNotIn(str(child), ps, 'the child process group was killed')


class QuestionReview3Tests(QuestionCase):
    """Permission and guard classification regressions."""
    def test_r3_1_one_guarded_clause_guards_the_modal(self):
        modal = {'questions': [{'question': 'May I deploy #123 to prod?', 'options': []},
                               {'question': 'Can I just check the logs?', 'options': []}]}
        question, _, _ = questions.from_tool_input(modal)
        self.assertTrue(questions.guarded(self.manifest, question)[0])
        self.assertTrue(questions.guarded(self.manifest, 'Can I check the logs? Then may I restart the host host-a?')[0])

    def test_r3_2_outage_fallback_needs_a_live_listener(self):
        self.file.write_text(self.file.read_text().replace('relay_file = "pending-alerts.ndjson"\n', ''))
        self.manifest = load(self.file)
        def down(client, card, brief):
            raise RuntimeError('anx down')
        with patch.object(AnxClient, 'ask', down):
            record, _ = questions.create(self.manifest, 'worker-ui', 'May I deploy #1?', recommendation='Yes', brief=BRIEF, session_id='s')
        after = self.records()[record['qid']]
        self.assertFalse(after.get('outage_alerted'), 'nobody live, no relay file, no pager: keep retrying')
        self.assertFalse([r for r in self.rows() if 'anx is down' in r.get('text', '')])

    def test_r3_4_pending_answer_is_not_recaptured(self):
        self.lease('lead-a', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-remote', 'Rerun?', recommendation='Yes', brief=BRIEF, session_id='s')
        questions.mark(self.manifest, record['qid'], state='answered')
        again, fresh = questions.create(self.manifest, 'worker-remote', 'Rerun?', recommendation='Yes', brief=BRIEF, session_id='s')
        self.assertEqual((again['qid'], fresh), (record['qid'], False))

    def test_r3_5_relay_notice_written_once(self):
        self.file.write_text(self.file.read_text().replace(f'delivery_cli = "{self.root/"delivery_cli"}"', 'delivery_cli = ""'))
        self.manifest = load(self.file)
        self.lease('lead-a', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-remote', 'Rerun?', recommendation='Yes', brief=BRIEF, session_id='s')
        with contextlib.redirect_stdout(io.StringIO()):
            questions.answer(self.manifest, record['qid'], 'lead-a', 'yes')
        for _ in range(3):
            questions.ladder(self.manifest)
        self.assertEqual(len([l for l in self.relay_lines() if 'QUESTION_ANSWER_UNDELIVERED' in l]), 1)

    def test_r3_6_real_hook_fails_open_within_its_deadline_and_kills_children(self):
        import subprocess, sys as _sys
        slow = self.root/'slow-anx'
        marker = self.root/'slow-anx.pid'
        slow.write_text(f'#!{_sys.executable}\nimport os,time\nopen({str(marker)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
        slow.chmod(0o755)
        payload = json.dumps({'session_id': 'sx', 'tool_name': 'AskUserQuestion', 'tool_input': {'questions': [
            {'question': 'May I deploy #123 to prod?', 'options': [{'label': 'Deploy (Recommended)'}, {'label': 'Wait'}]}]}})
        env = dict(os.environ, ANX=str(slow), PYTHONPATH=str(Path(questions.__file__).parents[1]), COORDKIT_AS='worker-ui')
        started = time.time()
        out = subprocess.run([_sys.executable, '-m', 'coordkit.cli', '--manifest', str(self.file), 'question', 'hook',
                              'pretooluse', '--config-dir', str(self.claude2)], input=payload, capture_output=True,
                             text=True, env=env, timeout=30)
        self.assertLess(time.time() - started, questions.HOOK_DEADLINE + 3)
        self.assertEqual((out.returncode, out.stdout), (0, ''), 'fails open: no deny without a durable post')
        # a capture is never filed to anx, so a hook cannot block on a slow anx at all.
        self.assertFalse(marker.exists(), 'a hook capture never calls anx')


class QuestionReview4Tests(QuestionCase):
    """Bounded hook and delivery regressions."""
    def test_r4_1_same_sentence_read_only_and_guarded_action(self):
        for text, want in (('Can I check the API key and rotate it?', True), ('Can I check the API key?', False),
                           ('Can I just read the API key and rotate the application log files?', False),
                           ('May I rehearse the host configuration now? It is a host change.', True),
                           ('May I look at the logs, then restart the host host-a?', True),
                           ('Should I review #123 and then merge it?', True)):
            with self.subTest(text=text):
                self.assertEqual(questions.guarded(self.manifest, text)[0], want)

    def test_r4_2_shadow_relay_notice_once(self):
        record, _ = questions.create(self.manifest, 'worker-ui', 'Rerun?', recommendation='Yes', brief=BRIEF, session_id='s')
        self.file.write_text(self.file.read_text().replace('[project]', 'mode = "shadow"\n[project]', 1))
        self.manifest = load(self.file)
        for _ in range(3):
            questions.close(self.manifest, questions.load_all(self.manifest)[record['qid']], 'lead-a', 'yes')
            questions.ladder(self.manifest)
        self.assertEqual(len([l for l in self.relay_lines() if 'QUESTION_ANSWER (shadow)' in l]), 1)

    def test_r4_3_stale_tier1_target_is_not_an_outage_route(self):
        self.file.write_text(self.file.read_text().replace('relay_file = "pending-alerts.ndjson"\n', ''))
        self.manifest = load(self.file)
        self.lease('lead-a')
        record, _ = questions.create(self.manifest, 'worker-ui', 'Rerun the lifecycle gate?', recommendation='Yes', brief=BRIEF, session_id='s', at=self.now)
        self.assertEqual(record['target'], 'lead-a')
        self.lease('lead-a', heartbeat_ago=3600)  # lead-a went quiet before the deadline
        def down(client, card, brief):
            raise RuntimeError('anx down')
        with patch.object(AnxClient, 'ask', down):
            questions.ladder(self.manifest, at=time.time() + 31*60)
        after = self.records()[record['qid']]
        self.assertFalse(after.get('outage_alerted'))
        self.assertEqual(after.get('outage_reached'), [])
        self.assertFalse([r for r in self.rows() if 'anx is down' in r.get('text', '')])


class BriefGateTests(QuestionCase):
    """Only an authored human decision brief reaches the operator."""

    def anx_asks(self):
        return [c for c in self.anx_calls if c[0] == 'ask']

    def test_unknown_asker_never_reaches_anx(self):
        record, _ = questions.create(self.manifest, 'unknown-session-without-question', 'Approve skipping make check?',
                                     recommendation='Skip it', session_id='session-without-question')  # No tier-one question exists for this session.
        self.assertEqual(self.anx_asks(), [])
        after = self.records()[record['qid']]
        self.assertEqual(after['tier'], 1)
        self.assertIn('unknown asker (session not bound to a bus name)', after['brief_refused'])
        self.assertTrue(any('QUESTION needs_brief' in l for l in self.relay_lines()))

    def test_ask_without_options_or_recommendation_routes_to_tier1_not_anx(self):
        self.lease('lead-a')
        raw = '**Decision for you:** approve a one-time skip of the OSS check, or let me retry later.'
        record, _ = questions.create(self.manifest, 'worker-ui', raw, session_id='s', urgent=True, at=self.now)
        questions.ladder(self.manifest, at=self.now + 31*60)  # past the deadline: would have gone to tier 2
        self.assertEqual(self.anx_asks(), [])
        briefs = [r for r in self.rows() if r['to'] == 'lead-a' and 'needs a decision brief' in r.get('text', '')]
        self.assertEqual(len(briefs), 1)
        self.assertLessEqual({'no decision with at least two options', 'no recommendation'}, set(self.records()[record['qid']]['brief_refused']))

    def test_refused_ask_is_not_retried_or_reposted(self):
        self.lease('lead-a')
        record, _ = questions.create(self.manifest, 'worker-ui', 'Thoughts on the backlog plan', session_id='s', at=self.now)
        for minutes in (31, 62, 93, 124):
            questions.ladder(self.manifest, at=self.now + minutes*60)
        self.assertEqual(self.anx_asks(), [])
        self.assertEqual(len([r for r in self.rows() if 'needs a decision brief' in r.get('text', '')]), 1)
        self.assertEqual(len([l for l in self.relay_lines() if 'needs_brief' in l]), 1)

    def test_valid_brief_still_reaches_anx(self):
        record, _ = questions.create(self.manifest, 'worker-ui', 'Ship #123 to prod now?', recommendation='Ship it', brief=dict(BRIEF, recommendation='Ship it'),
                                     session_id='s', urgent=True)
        self.assertEqual(record['tier'], 2)
        self.assertEqual(self.anx_asks()[0][2]['recommendation'], 'Ship it')

    def test_valid_brief_renders_every_field_in_the_anx_ask(self):
        from coordkit import briefs
        calls = []
        with patch.object(AnxClient, 'ask', REAL_ASK), \
                patch.object(AnxClient, 'call', lambda client, *args, **kw: calls.append(args) or {'ask_id': 'ask-9'}):
            questions.create(self.manifest, 'worker-ui', 'May I deploy #123 to prod?', recommendation='Deploy it',
                             session_id='s', urgent=True, brief=dict(BRIEF, title='May I deploy #123 to prod?', evidence=[
                                 {'label': 'PR #123', 'url': 'https://github.com/example/project/pull/123'}]))
        args = calls[0]
        self.assertEqual((args[0], args[1]), ('ask', 'May I deploy #123 to prod?'))  # the decision, not "Coordkit: ..."
        for flag in ('--subject-ref', '--situation', '--impact', '--recommend', '--why', '--if-no-answer', '--alternative',
                     '--evidence', '--details'):
            self.assertIn(flag, args)
        self.assertEqual(sum(1 for a in args if a == '--alternative'), 2)
        self.assertIn('PR #123=https://github.com/', ' '.join(args))
        body = ' '.join(args[args.index(f) + 1] for f in ('--situation', '--impact', '--recommend', '--why', '--if-no-answer'))
        self.assertIsNone(briefs.JARGON.search(body), body)

    def test_excluded_and_human_sessions_are_never_captured(self):
        self.file.write_text(self.file.read_text().replace('[questions]', '[questions]\nexclude_sessions = ["operator-chat"]\nhuman_sessions = ["worker-b"]', 1))
        self.manifest = load(self.file)
        self.assertIsNone(self.hook('pretooluse', {'session_id': 'operator-chat', 'tool_name': 'AskUserQuestion', 'tool_input': MODAL}))
        with patch.dict(os.environ, COORDKIT_AS='worker-b'):
            self.assertIsNone(self.hook('pretooluse', {'session_id': 'other', 'tool_name': 'AskUserQuestion', 'tool_input': MODAL}))
        self.assertEqual(self.records(), {})

    def test_parked_idle_notice_never_becomes_a_question_and_permission_goes_to_tier1(self):
        self.lease('lead-a')
        from coordkit.state import write_json
        home = questions.home(self.manifest)/'notifications'; home.mkdir(parents=True, exist_ok=True)
        old = time.time() - 3600
        write_json(home/'idle1.json', {'message': 'Claude is waiting for your input', 'type': 'idle_prompt', 'at': old})
        write_json(home/'perm1.json', {'message': 'Claude needs your permission to use Bash', 'type': 'permission_prompt', 'at': old})
        with contextlib.redirect_stdout(io.StringIO()):
            questions.main(['bind', '--session', 'perm1', '--as', 'worker-ui'], self.manifest)  # a bound lane (fallback lead-a)
        questions.scan_notifications(self.manifest)
        records = list(self.records().values())
        self.assertEqual(len(records), 1)
        self.assertIn('blocked on a permission prompt', records[0]['question'])
        self.assertEqual((records[0]['tier'], records[0]['target']), (1, 'lead-a'))
        self.assertEqual(self.anx_asks(), [])
        self.assertFalse(list(home.glob('*.json')))

    def test_tier1_authors_a_brief_that_then_reaches_human(self):
        self.lease('lead-a')
        record, _ = questions.create(self.manifest, 'worker-ui', 'Thoughts on the backlog plan', session_id='s', at=self.now)
        brief = self.root/'brief.json'
        brief.write_text(json.dumps({'title': 'Skip the OSS check for this release?', 'situation': 'The release is ready but the OSS check timed out under load. Main CI is green on the same code.',
                                     'impact': 'The release waits until someone decides.', 'options': [
                                         {'action': 'Skip the check this once', 'consequence': 'Ships now on the green main CI.', 'reversible': True},
                                         {'action': 'Retry later', 'consequence': 'Ships in about an hour.', 'reversible': True}],
                                     'recommendation': 'Skip the check this once', 'why': 'Main CI already passed on identical code.',
                                     'if_no_answer': 'Nothing ships; the lane retries at 14:00Z.'}))
        bad = self.root/'bad.json'; bad.write_text(json.dumps({'title': 'q-abc123 check?'}))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(questions.main(['brief', record['qid'], '--as', 'lead-a', '--from-file', str(bad)], self.manifest), 2)
        self.assertIn('title contains ids or paths', out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(questions.main(['brief', record['qid'], '--as', 'lead-a', '--from-file', str(brief)], self.manifest), 0)
        sent = self.anx_asks()[0][2]
        self.assertEqual(sent['title'], 'Skip the OSS check for this release?')
        self.assertIn(record['qid'], sent['details'])
        self.assertEqual(self.records()[record['qid']]['tier'], 2)


class AuthoredBriefTests(QuestionCase):
    """A capture is evidence, never the authored ask; the asker is immutable."""
    def test_well_formed_capture_still_goes_to_tier1_not_anx(self):
        self.lease('lead-a')
        payload = {'session_id': 'sess-9', 'tool_name': 'AskUserQuestion', 'tool_input': MODAL}
        with patch.dict(os.environ, COORDKIT_AS='worker-ui'):
            self.hook('pretooluse', payload)
        record = next(iter(self.records().values()))
        questions.ladder(self.manifest, at=time.time() + 3600)  # long past the tier-1 deadline
        self.assertEqual([c for c in self.anx_calls if c[0] == 'ask'], [])
        self.assertIn('no decision brief authored yet', self.records()[record['qid']]['brief_refused'])

    def test_a_brief_cannot_change_an_unbound_asker(self):
        record, _ = questions.create(self.manifest, 'unknown-abcd1234', 'Rerun the gate?', session_id='abcd1234')
        brief = self.root/'brief.json'; brief.write_text(json.dumps(dict(BRIEF, asker='lead-a')))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = questions.main(['brief', record['qid'], '--as', 'lead-a', '--from-file', str(brief)], self.manifest)
        self.assertEqual(rc, 2)
        self.assertIn('unknown asker (session not bound to a bus name)', out.getvalue())  # the brief's asker is ignored
        self.assertEqual([c for c in self.anx_calls if c[0] == 'ask'], [])
        self.assertNotIn('brief', self.records()[record['qid']])

    def test_validator_refuses_relative_paths_code_and_unstated_reversibility(self):
        from coordkit import briefs
        self.assertIn('title contains ids or paths', briefs.validate(dict(BRIEF, asker='x', title='Should I patch src/coordkit/briefs.py?')))
        self.assertIn('title contains ids or paths', briefs.validate(dict(BRIEF, asker='x', title='Run `make ship` now?')))
        for path in ('src/worker/main.rs', 'cmd/server/main', 'infra/rollout.cfg', 'src/main', 'app/lib',
                     '../README.md', './README.md', '../lib', 'README.md', 'main.py', 'main.c', 'main.cpp',
                     'header.h', 'build.gradle', 'Dockerfile.prod', 'github.com', 'main.C', 'Main.CPP', 'LICENSE.TXT',
                     '.env', '.gitignore', 'README.markdown', 'config.properties', '/tmp', 'src/', '../', '~/',
                     'C:\\repo\\src', 'src\\main', '--dry-run', '$HOME', 'validate()', 'foo(arg)', 'foo::bar',
                     'core_worktree', '0xdeadbeef', 'id:abcdef', 'DEADBEEF12', 'ABC123', 'Makefile'):
            self.assertIn('situation contains ids or paths (move them to details)',
                          briefs.validate(dict(BRIEF, asker='x', situation=f'The change is in {path}.')), path)
        for plain in ('TCP/IP', 'and/or', '24/7', 'v1.2', 'yes/no', 'I/O', '50/50', '3.2 cadences', 'e.g. a rerun',
                      'about 1.5 hours', 'a follow-up (if needed)', 'costs $1.50', 'in 2024', 'the make step',
                      'about 30 minutes, then a rerun', 'ship #123 today'):
            self.assertEqual(briefs.validate(dict(BRIEF, asker='x', situation=f'It uses {plain} today.')), [], plain)
        opts = [dict(o, reversible=None) for o in BRIEF['options']]
        self.assertIn('each option must say whether it can be undone (reversible: true/false)',
                      briefs.validate(dict(BRIEF, asker='x', options=opts)))
        self.assertEqual(briefs.validate(dict(BRIEF, asker='x')), [])


class RefusalDeliveryTests(QuestionCase):
    def test_a_refusal_delivered_nowhere_stays_retryable_then_lands(self):
        # no live tier 1 and no relay file used to mark the refusal done, losing the obligation.
        from coordkit import addressed
        with patch.object(addressed, 'to_relay_file', lambda *a, **k: False):
            record, _ = questions.create(self.manifest, 'unknown-abcd1234', 'Rerun the gate?', session_id='abcd1234')
            after = self.records()[record['qid']]
            self.assertIsNone(after.get('brief_refused_at'))
            self.assertTrue(after['refusal_undelivered_at'])
        lines = questions.ladder(self.manifest, at=after['deadline_at'] + 1)  # relay works again on a later sweep
        self.assertTrue(self.records()[record['qid']].get('brief_refused_at'), lines)
        self.assertTrue(any('needs_brief' in l for l in self.relay_lines()))
        self.assertEqual([c for c in self.anx_calls if c[0] == 'ask'], [])


    def test_shadow_refusal_with_a_live_tier1_but_no_relay_stays_retryable(self):
        # in shadow mode post() only relays, so a live target is not delivery by itself.
        from coordkit import addressed
        self.lease('lead-a')
        self.file.write_text(self.file.read_text().replace('[project]', 'mode = "shadow"\n[project]', 1))
        self.manifest = load(self.file)
        with patch.object(addressed, 'to_relay_file', lambda *a, **k: False):
            record, _ = questions.create(self.manifest, 'worker-ui', 'Thoughts on the plan', session_id='s', at=self.now)
            questions.ladder(self.manifest, at=self.now + 31*60)  # the tier-1 deadline passes: tier 2 refuses it
        after = self.records()[record['qid']]
        self.assertIsNone(after.get('brief_refused_at'))
        self.assertTrue(after.get('refusal_undelivered_at'), after)


    def test_relayed_refusal_completes_even_when_the_bus_post_fails(self):
        # a failing bus post left a relayed refusal retryable, re-relaying it every sweep.
        self.lease('lead-a')
        record, _ = questions.create(self.manifest, 'worker-ui', 'Thoughts on the plan', session_id='s', at=self.now)
        with patch.object(questions, 'post', side_effect=SystemExit(1)):
            questions.ladder(self.manifest, at=self.now + 31*60)
        self.assertTrue(self.records()[record['qid']].get('brief_refused_at'))
        for minutes in (62, 93): questions.ladder(self.manifest, at=self.now + minutes*60)
        self.assertEqual(len([l for l in self.relay_lines() if 'needs_brief' in l]), 1)


class AnxInboxReconcileTests(QuestionCase):
    """M15b: answers and withdrawals are read from the anx inbox listing, not only the consuming await stream."""
    def escalated(self):
        record, _ = questions.create(self.manifest, 'worker-ui', 'May I deploy #123 to prod?', brief=BRIEF,
                                     session_id='s', at=self.now)
        self.assertEqual(self.records()[record['qid']]['anx_ask_id'], 'ask-7')
        return record

    def test_an_answer_another_reader_consumed_is_still_relayed(self):
        record = self.escalated()
        lines = questions.ladder(self.manifest, at=self.now + 60, anx_answers={'ask-7': {'text': 'yes, ship it'}},
                                 anx_known={'ask-7'})
        self.assertIn(f'QUESTION_ANSWERED {record["qid"]} by operator (anx)', lines)

    def test_a_withdrawn_ask_closes_after_the_grace_window_only_on_a_complete_listing(self):
        record = self.escalated()
        self.assertEqual(questions.ladder(self.manifest, at=self.now + 60, anx_known=set()), [])  # just filed
        self.assertEqual(questions.ladder(self.manifest, at=self.now + 3600, anx_known=None), [])  # listing unknown
        lines = questions.ladder(self.manifest, at=self.now + 3600, anx_known=set())
        self.assertIn(f'QUESTION_CLOSED {record["qid"]} withdrawn in anx', lines)
        self.assertFalse([c for c in self.anx_calls if 'withdraw' in c])  # already gone in anx: no second withdraw

    def test_anx_inbox_refuses_a_paginated_listing(self):
        with patch.object(AnxClient, 'call', lambda client, *a, **k: {'items': [{'ask_id': 'x', 'status': 'open'}],
                                                                       'has_more': True}):
            self.assertEqual(questions.anx_inbox(self.manifest), ({}, None))
        with patch.object(AnxClient, 'call', lambda client, *a, **k: {'items': [
                {'ask_id': 'a', 'status': 'open'}, {'ask_id': 'b', 'status': 'answered', 'answer': {'text': 'no'}}]}):
            self.assertEqual(questions.anx_inbox(self.manifest), ({'b': {'text': 'no'}}, {'a', 'b'}))


class RefusedUnboundCloseTests(QuestionCase):
    def test_a_relayed_refusal_from_an_unbound_session_closes_after_the_window(self):
        record, _ = questions.create(self.manifest, 'unknown-728d2938', 'I am waiting on the operator about the lane change.',
                                     session_id='728d2938', at=self.now)
        after = self.records()[record['qid']]
        self.assertTrue(after.get('brief_refused_at')); self.assertIsNone(after.get('target'))
        self.assertEqual(questions.ladder(self.manifest, at=self.now + 60), [])
        lines = questions.ladder(self.manifest, at=self.now + questions.UNBOUND_DELIVERY_WINDOW + 60)
        self.assertIn(f'QUESTION_CLOSED {record["qid"]} relayed (unbound asker, refused capture)', lines)
        self.assertNotIn(record['qid'], [r['qid'] for r in questions.open_questions(self.manifest)])


class UnboundDeliveryTests(QuestionCase):
    def test_answer_to_an_unbound_asker_closes_after_the_delivery_window(self):
        self.lease('relay', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'unknown-abcd1234', 'Rerun the gate?', session_id='abcd1234')
        self.file.write_text(self.file.read_text().replace(f'delivery_cli = "{self.root/"delivery_cli"}"', 'delivery_cli = ""'))
        self.manifest = load(self.file)
        with contextlib.redirect_stdout(io.StringIO()):
            questions.answer(self.manifest, record['qid'], 'relay', 'yes')
        self.assertEqual(questions.ladder(self.manifest, at=time.time() + 60), [])
        self.assertEqual(self.records()[record['qid']]['state'], 'answered')
        lines = questions.ladder(self.manifest, at=time.time() + 3601)
        self.assertEqual(lines, [f'QUESTION_CLOSED {record["qid"]} relayed (unbound asker)'])
        self.assertEqual(self.records()[record['qid']]['state'], 'closed')


class DefaultAndDedupeTests(QuestionCase):
    def pending(self, default='', **kwargs):
        self.lease('lead-a', heartbeat_ago=-7200)
        record, _ = questions.create(self.manifest, 'worker-ui', 'Rerun the lifecycle gate?',
                                     recommendation='Rerun it', brief=BRIEF, session_id='defaults',
                                     default=default, at=self.now, **kwargs)
        return record

    def test_q7_second_unanswered_cycle_applies_default_and_records_decision(self):
        record = self.pending('Keep waiting', bus_id='original-ask')
        first = questions.ladder(self.manifest, at=self.now + 31*60)
        self.assertIn('QUESTION_ESCALATED', first[0])
        self.assertEqual(self.records()[record['qid']]['cycles'], 1)
        self.assertEqual(questions.ladder(self.manifest, at=self.now + 32*60), [])
        with patch.object(questions, 'deliver', return_value='relay') as deliver:
            lines = questions.ladder(self.manifest, at=self.now + 62*60)
        self.assertEqual(lines, [f'QUESTION_DEFAULTED {record["qid"]} default applied after 2 cycles'])
        stored = self.records()[record['qid']]
        self.assertEqual((stored['state'], stored['answered_by'], stored['applied_default']),
                         ('answered', 'default', True))
        decisions = [r for r in self.rows() if r['kind'] == 'decision']
        self.assertEqual(len(decisions), 1)
        self.assertEqual((decisions[0]['re'], decisions[0]['to'], decisions[0]['text']),
                         ('original-ask', 'worker-ui', 'Keep waiting'))
        self.assertEqual(decisions[0]['rollback'], questions.DEFAULT_ROLLBACK)
        deliver.assert_called_once()
        questions.ladder(self.manifest, at=self.now + 63*60)
        self.assertEqual(len([r for r in self.rows() if r['kind'] == 'decision']), 1)

    def test_defaulted_question_reports_in_status_and_no_default_still_waits(self):
        record = self.pending('Keep waiting')
        ask = next(r for r in self.rows() if r['id'] == record['qid'])
        self.assertEqual(ask['default'], 'Keep waiting')
        questions.capture_bus_asks(self.manifest, self.rows(), self.now)
        self.assertEqual(len(self.records()), 1)
        questions.ladder(self.manifest, at=self.now + 31*60)
        questions.ladder(self.manifest, at=self.now + 62*60)
        self.assertIn('defaulted=1', questions.status_line(self.manifest, self.now + 62*60))
        self.assertIn('default: Keep waiting', questions.summary(record))
        other, _ = questions.create(self.manifest, 'worker-ui', 'Retry the gate?', recommendation='Retry',
                                    brief=BRIEF, session_id='no-default', default='  ', at=self.now)
        questions.ladder(self.manifest, at=self.now + 31*60)
        questions.ladder(self.manifest, at=self.now + 62*60)
        stored = self.records()[other['qid']]
        self.assertEqual((stored['state'], stored['tier'], stored['cycles']), ('escalated', 2, 2))
        self.assertFalse(stored.get('applied_default'))

    def test_reworded_reask_same_key_joins_existing_open_question(self):
        self.lease('lead-a')
        first, fresh = questions.create(self.manifest, 'worker-ui', 'Retry at 12:30Z q-abcdef attempt 1?',
                                       session_id='dedupe', at=self.now)
        with patch.object(questions, 'route') as route:
            second, fresh = questions.create(self.manifest, 'worker-ui', 'RETRY   at 13:45Z q-123abc attempt 22?',
                                            session_id='dedupe', at=self.now + 60)
        self.assertFalse(fresh)
        route.assert_not_called()
        self.assertEqual(first['qid'], second['qid'])
        self.assertEqual((second['created_at'], second['deadline_at']), (first['created_at'], first['deadline_at']))
        self.assertEqual((second['last_seen'], second['reopens']), (self.now + 60, 1))
        self.assertIn('re-asked 1x', questions.summary(second))
        self.assertEqual(len(self.records()), 1)

    def test_reask_fingerprint_preserves_pr_numbers_and_flag_keys(self):
        texts = ['Ship #123 with FEATURE_GATE_V1?', 'Ship #124 with FEATURE_GATE_V1?',
                 'Ship #123 with FEATURE_GATE_V2?', 'Ship #123 with OTHER_GATE_V1?']
        keys = {questions.fingerprint('s', text) for text in texts}
        self.assertEqual(len(keys), len(texts))
        for text in texts:
            questions.create(self.manifest, 'worker-ui', text, session_id='pr-flags', at=self.now)
        self.assertEqual(len(self.records()), len(texts))

    def test_default_needs_two_real_windows_and_answer_wins_at_deadline(self):
        record = self.pending('Keep waiting')
        questions.ladder(self.manifest, at=self.now + 31*60)
        # Receipts are not answers, but a real answer at the deadline takes precedence over the default.
        rows = [{'id': 'answer-1', 'frm': 'lead-a', 're': record['qid'], 'kind': 'answer', 'text': 'Retry'}]
        lines = questions.ladder(self.manifest, at=self.now + 62*60, rows=rows)
        self.assertIn('QUESTION_ANSWERED', lines[0])
        self.assertFalse(self.records()[record['qid']].get('applied_default'))

    def test_anx_outage_retries_do_not_count_as_unanswered_cycles(self):
        record = self.pending('Keep waiting')
        with patch.object(AnxClient, 'ask', side_effect=RuntimeError('offline')):
            for minutes in (31, 37, 43, 80):
                questions.ladder(self.manifest, at=self.now + minutes*60)
        stored = self.records()[record['qid']]
        self.assertEqual(stored['cycles'], 1)
        self.assertFalse(stored.get('applied_default'))

    def test_bus_ask_default_capture_is_opt_in_and_reasks_captured_once(self):
        from coordkit import bus
        self.lease('lead-a')
        bus.main(['post', '--from', 'worker-ui', '--to', 'lead-a', '--kind', 'ask', '--default', 'Wait',
                  '--force', 'Retry at 12:30Z attempt 1?'], self.manifest)
        questions.capture_bus_asks(self.manifest, self.rows(), self.now)
        record = next(iter(self.records().values()))
        original_id = record['bus_id']
        self.assertEqual((record['default'], record['target'], record['capture']), ('Wait', 'lead-a', 'bus'))
        bus.main(['post', '--from', 'worker-ui', '--to', 'lead-a', '--kind', 'ask', '--default', 'Wait',
                  '--force', 'RETRY at 13:30Z attempt 2?'], self.manifest)
        for _ in range(3):
            questions.capture_bus_asks(self.manifest, self.rows(), self.now)
        self.assertEqual(len(self.records()), 1)
        record = next(iter(self.records().values()))
        self.assertEqual((record['bus_id'], record['reopens']), (original_id, 1))

    def test_existing_fingerprint_migrates_and_different_choices_do_not_join(self):
        self.lease('lead-a')
        record, _ = questions.create(self.manifest, 'worker-ui', 'Retry at 12:30Z attempt 1?',
                                     session_id='old-record', at=self.now)
        questions.mark(self.manifest, record['qid'], fingerprint='legacy-fingerprint')
        joined, fresh = questions.create(self.manifest, 'worker-ui', 'RETRY at 14:20z attempt 2?',
                                         session_id='old-record', at=self.now + 60)
        self.assertFalse(fresh)
        self.assertEqual(record['qid'], joined['qid'])
        for kwargs in ({'options': ['Wait', 'Retry']}, {'default': 'Wait'}, {'recommendation': 'Retry'}):
            with self.subTest(kwargs=kwargs):
                _, fresh = questions.create(self.manifest, 'worker-ui', 'RETRY at 14:20z attempt 2?',
                                             session_id='old-record', at=self.now + 60, **kwargs)
                self.assertTrue(fresh)

    def test_direct_tier2_waits_for_two_full_windows(self):
        record, _ = questions.create(self.manifest, 'worker-ui', '[human-only] Retry the gate?',
                                     recommendation='Retry', brief=BRIEF, default='Wait', at=self.now)
        self.assertEqual(record['tier'], 2)
        self.assertEqual(questions.ladder(self.manifest, at=self.now + 31*60), [])
        self.assertEqual(self.records()[record['qid']]['cycles'], 1)
        lines = questions.ladder(self.manifest, at=self.now + 62*60)
        self.assertIn('QUESTION_DEFAULTED', lines[0])

    def test_default_decision_survives_a_failed_completion_without_duplicate_post(self):
        record = self.pending('Keep waiting')
        questions.ladder(self.manifest, at=self.now + 31*60)
        with patch.object(questions, 'close', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                questions.ladder(self.manifest, at=self.now + 62*60)
        self.assertEqual(self.records()[record['qid']]['state'], 'escalated')
        lines = questions.ladder(self.manifest, at=self.now + 68*60)
        self.assertIn('QUESTION_DEFAULTED', lines[0])
        decisions = [r for r in self.rows() if r['kind'] == 'decision']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['rollback'], questions.DEFAULT_ROLLBACK)
