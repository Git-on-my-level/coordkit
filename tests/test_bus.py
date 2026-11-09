import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from coordkit import bus
from coordkit.manifest import ManifestError
from tests.support import FakeCase


T0 = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)


class BusTests(FakeCase):
    def invoke(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = bus.main(list(args), self.manifest) or 0
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(result, code, err.getvalue() + out.getvalue())
        return out.getvalue(), err.getvalue()

    def post(self, kind='msg', *flags, code=0, to='lead', frm='worker', text='test'):
        return self.invoke('post', '--from', frm, '--to', to, '--kind', kind, *flags, text, code=code)

    def rows(self):
        return [json.loads(line) for line in self.manifest.paths.bus_log.read_text().splitlines()]

    def test_refusal_matrix(self):
        cases = [('ruling', ['--owner','all']), ('ruling', ['--owner','lead']),
                 ('ruling', ['--owner','unknown']), ('blocker', []), ('blocker', ['--owner','worker']),
                 ('blocker', ['--owner','all','--needs','tool']), ('decision', []),
                 ('decision', ['--subject','s']), ('decision', ['--subject','s','--rollback','undo','--irreversible']),
                 ('hold', []), ('unhold', []), ('hold', ['--pr','abc']), ('state', []),
                 ('state', ['--state','DONE']), ('state', ['--state','BAD','--subject','s']),
                 ('state', ['--state','DONE','--subject','bad subject']), ('signal', []),
                 ('signal', ['--signal','lowercase']),
                 ('msg', ['--state','DONE']), ('msg', ['--rollback','undo']), ('msg', ['--pr','123']),
                 ('msg', ['--needs','help']), ('msg', ['--evidence','sha']), ('msg', ['--signal','READY_NOW'])]
        for kind, flags in cases:
            with self.subTest(kind=kind, flags=flags):
                self.post(kind, *flags, code=2)
        self.post(to='unknown', code=2)
        self.assertFalse(self.manifest.paths.bus_log.exists())

    def test_ack_ownership_and_idempotency(self):
        self.post('ruling','--owner','worker')
        rid = self.rows()[0]['id']
        self.invoke('ack', rid, '--from','other', code=2)
        self.invoke('ack', rid, '--from','worker')
        self.invoke('ack', rid, '--from','worker')
        self.assertEqual(len(self.rows()),2)
        self.assertEqual(json.loads(self.invoke('open-rulings','--json')[0]), [])
        calls = [c['args'] for c in self.calls() if c['tool'] == 'anx']
        self.assertTrue(any('--next' in a for a in calls))
        self.assertTrue(any('--met' in a and '--evidence' in a for a in calls))

    def test_forced_ack(self):
        self.post('ruling','--owner','worker')
        self.invoke('ack',self.rows()[0]['id'],'--from','lead','--force')
        self.assertIn('forced', self.rows()[-1]['text'])

    def test_blocker_close_and_human_ask(self):
        self.post('blocker','--owner','lead','--needs','approve','--subject','work')
        rid = self.rows()[0]['id']
        # the lead owns the blocker on the bus; the mirror notes the card and never asks the operator directly.
        self.assertFalse([c for c in self.calls() if c['tool'] == 'anx' and 'ask' in c['args']])
        self.assertTrue(any('note' in c['args'] and any('blocked' in a and 'approve' in a for a in c['args']) for c in self.calls()))
        self.post('decision','--re',rid,'--subject','work','--rollback','revert')
        self.assertEqual(json.loads(self.invoke('open-blockers','--json')[0]), [])

    def test_handoff_move_and_warning(self):
        self.post('handoff','--subject','work')
        self.assertTrue(any('move' in c['args'] and 'review' in c['args'] for c in self.calls()))
        self.assertIn('warning', self.post('handoff','--subject','work')[1])

    def test_state_and_fyi_latch_windows(self):
        self.manifest.bus.mirror = False
        for kind, flags in [('state',['--state','RUNNING','--subject','build']),('fyi',[])]:
            self.post(kind,*flags)
            count = len(self.rows())
            self.assertIn('deduped', self.post(kind,*flags)[0])
            self.assertEqual(len(self.rows()),count)
            with patch.object(bus,'now',return_value=dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=7)):
                self.post(kind,*flags)
            self.assertEqual(len(self.rows()),count+1)
        self.post('state','--state','DONE','--subject','build')
        self.assertEqual(json.loads(self.invoke('state','--json')[0])[0]['state'],'DONE')

    def test_latest_state_wait_and_failure_exit(self):
        self.manifest.bus.mirror = False
        for state in ('DONE','RUNNING'):
            self.post('state','--state',state,'--subject','build')
        self.invoke('wait','--subject','build','--since','1h','--timeout','0',code=3)
        self.post('state','--state','FAILED','--subject','build')
        self.invoke('wait','--subject','build','--since','1h','--timeout','0',code=4)
        self.invoke('wait','--subject','build','--state','FAILED','--since','1h','--timeout','0')

    def test_idle_copy_degrades_ruling(self):
        self.post('ruling','--owner','worker',to='worker',frm='lead')
        rows = self.rows()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['kind'],'fyi')
        self.assertNotIn('owner',rows[1])
        self.assertEqual(rows[1]['orig_kind'],'ruling')
        self.assertEqual(len(json.loads(self.invoke('open-rulings','--json')[0])),1)

    def test_new_launch_is_not_idle(self):
        self.manifest.bus.live_check = True
        self.respond({'tool':'agentctl','contains':['recent'],'json':{'ok':True,'result':{'executions':[
            {'id':'exec-one','state':'running','labels':['worker.audit'], 'created_at':dt.datetime.now(dt.timezone.utc).isoformat()}]}}})
        self.post(to='worker',frm='lead')
        self.assertEqual(len(self.rows()),1)

    def test_alias_and_inbox_cursor(self):
        self.manifest.bus.aliases = {'alias':'worker'}
        self.post(to='alias',frm='lead')
        self.assertEqual(self.rows()[0]['to'],'worker')
        self.assertIn('test',self.invoke('inbox','worker','--unread','--brief')[0])
        self.assertEqual(self.invoke('inbox','worker','--unread')[0],'')

    def test_wait_cursor_and_fyi_no_wake(self):
        self.manifest.bus.mirror = False
        self.invoke('wait','--to','lead','--timeout','0',code=3)
        self.post('fyi')
        self.invoke('wait','--to','lead','--timeout','0',code=3)
        self.post('ask')
        self.assertIn('ask',self.invoke('wait','--to','lead','--timeout','0')[0])
        self.invoke('wait','--to','lead','--timeout','0',code=3)

    def test_signal_only_matches_field(self):
        self.post(text='READY_NOW')
        self.invoke('wait','--signal','READY_NOW','--since','1h','--timeout','0',code=3)
        self.post('signal','--signal','READY_NOW')
        self.assertIn('BUS_SIGNAL',self.invoke('wait','--signal','READY_NOW','--since','1h','--timeout','0')[0])

    def test_own_ask_replies_wake_regardless_of_kind_or_addressee(self):
        self.manifest.bus.mirror = False
        self.post('ask', to='worker', frm='lead')
        mine = self.rows()[-1]['id']
        self.post('ask', to='lead', frm='worker')
        other = self.rows()[-1]['id']
        self.invoke('wait', '--to', 'lead', '--own-asks', '--kinds', 'answer', '--timeout', '0', code=3)
        bus.configure(self.manifest)
        for kind in ('answer', 'ruling', 'state', 'fyi'):
            bus.append([dict(id='reply-'+kind, ts=T0.isoformat(), frm='worker', to='elsewhere',
                             kind=kind, re=mine, text='reply')])
            self.assertIn('reply-'+kind, self.invoke('wait', '--to', 'lead', '--own-asks',
                          '--kinds', 'ask', '--timeout', '0')[0])
        bus.append([dict(id='unrelated', ts=T0.isoformat(), frm='worker', to='lead',
                         kind='fyi', re=other, text='not my ask')])
        self.invoke('wait', '--to', 'lead', '--own-asks', '--kinds', 'ask', '--timeout', '0', code=3)

    def test_restarted_own_ask_seed_keeps_ids_that_overlap_the_cursor(self):
        # A cursor inside the seeded tail must not append those ask ids twice.
        # The capped deque would otherwise drop the oldest asks behind the cursor.
        self.manifest.bus.mirror = False
        bus.configure(self.manifest)
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True, exist_ok=True)
        asks = [json.dumps(dict(id=f'ask-{index:04d}', ts='2026-01-01T00:00:00+00:00', frm='lead',
                                to='worker', kind='ask', topic='general', text='a', v=1),
                           separators=(',', ':')) for index in range(4097)]
        reply = json.dumps(dict(id='late-fyi', ts='2026-01-01T00:00:01+00:00', frm='worker',
                                to='elsewhere', kind='fyi', re='ask-0001', topic='general',
                                text='late', v=1), separators=(',', ':'))
        path.write_text('\n'.join([*asks, reply]) + '\n')
        for cursor in (4096, 2048, 1):
            with self.subTest(cursor=cursor):
                bus.set_cursor('lead', 'wait', cursor)
                self.assertIn('late-fyi', self.invoke('wait', '--to', 'lead', '--own-asks',
                                                      '--kinds', 'ask', '--timeout', '0')[0])

    def test_restarted_own_ask_seed_survives_overlap_then_reply_on_a_later_poll(self):
        self.manifest.bus.mirror = False
        bus.configure(self.manifest)
        bus.append([dict(id=f'ask-{index:04d}', ts=T0.isoformat(), frm='lead', to='worker',
                         kind='ask', text='a') for index in range(4096)])
        bus.set_cursor('lead', 'wait', 2048)

        def reply(_):
            # This only wakes through own-asks: neither the kind nor recipient matches.
            bus.append([dict(id='later-reply', ts=T0.isoformat(), frm='worker', to='elsewhere',
                             kind='fyi', re='ask-0000', text='late')])

        with patch.object(bus.time, 'sleep', side_effect=reply) as sleep:
            self.assertIn('later-reply', self.invoke('wait', '--to', 'lead', '--own-asks',
                                                   '--kinds', 'ask', '--timeout', '1')[0])
        sleep.assert_called_once()
        self.assertEqual(bus.get_cursor('lead', 'wait', None), 4097)

    def test_text_spill_and_ref_refusal(self):
        self.post(text='x'*1000,to='worker')
        self.assertTrue(all(len(r['text'])<=400 for r in self.rows()))
        spills = list((self.manifest.paths.root/'state/refs').glob('*.txt'))
        self.assertEqual(spills[0].read_text().strip(),'x'*1000)
        self.post('msg','--ref',str(self.root/'missing'),code=2)

    def full_views(self):
        # Arm before posting so the wait path consumes this row too.
        self.invoke('wait', '--to', 'reader', '--timeout', '0', code=3)
        return (('inbox', 'reader', '--full'),
                ('wait', '--to', 'reader', '--full', '--timeout', '0'))

    def test_full_views_never_read_text_only_spill_markers(self):
        refs = self.manifest.paths.bus_log.parent/'state/refs'
        refs.mkdir(parents=True, exist_ok=True)
        for index, secret in enumerate((refs/'unpublished.txt', self.root/'private.txt')):
            secret.write_text('UNPUBLISHED')
            views = self.full_views()
            self.post('ask', '--force', to='reader', text=f'cut {index} … [full: {secret}]')
            for args in views:
                with self.subTest(path=secret, command=args[0]), patch.object(
                        Path, 'read_text', autospec=True, side_effect=Path.read_text) as read:
                    output, _ = self.invoke(*args)
                    self.assertNotIn(secret, [call.args[0] for call in read.call_args_list])
                    self.assertNotIn('UNPUBLISHED', output)

    def test_full_views_reject_structured_refs_outside_roots(self):
        refs = self.manifest.paths.bus_log.parent/'state/refs'
        refs.mkdir(parents=True, exist_ok=True)
        outside = self.root/'private.txt'
        outside.write_text('UNPUBLISHED')
        (refs/'escape').symlink_to(outside)
        sibling = refs.with_name('refs-sibling')
        sibling.mkdir()
        (sibling/'secret').write_text('UNPUBLISHED')
        for ref in (str(outside), 'state/refs/escape', 'state/refs/../../../private.txt', str(sibling/'secret')):
            for field in ('ref', 'spill_ref'):
                views = self.full_views()
                # Rows on shared/replicated buses may contain hostile structured refs.
                bus.append([dict(id='hostile', ts=bus.now().isoformat(), frm='poster', to='reader',
                                 kind='ask', text=f'cut … [full: {ref}]', **{field: ref})], project=False)
                for args in views:
                    with self.subTest(ref=ref, field=field, command=args[0]):
                        output, _ = self.invoke(*args)
                        self.assertNotIn('UNPUBLISHED', output)

    def test_full_views_expand_poster_structured_attachment(self):
        refs = self.manifest.paths.bus_log.parent/'state/refs'
        refs.mkdir(parents=True, exist_ok=True)
        attached = refs/'attachment.txt'
        attached.write_text('attached full body\n')
        views = self.full_views()
        self.post('ask', '--force', '--ref', str(attached), to='reader', text='brief')
        for args in views:
            with self.subTest(command=args[0]):
                self.assertIn('attached full body', self.invoke(*args)[0])

    def test_full_views_expand_relative_ref_under_configured_root(self):
        archive = self.manifest.paths.bus_log.parent/'archive'
        archive.mkdir(parents=True, exist_ok=True)
        (archive/'body.txt').write_text('archived full body\n')
        self.manifest.mcp.ref_roots = [str(archive)]
        views = self.full_views()
        bus.append([dict(id='archived', ts=bus.now().isoformat(), frm='poster', to='reader',
                         kind='ask', text='brief', ref='archive/body.txt')], project=False)
        for args in views:
            with self.subTest(command=args[0]):
                self.assertIn('archived full body', self.invoke(*args)[0])

    def test_full_views_expand_structured_spill_over_attachment_and_text(self):
        refs = self.manifest.paths.bus_log.parent/'state/refs'
        refs.mkdir(parents=True, exist_ok=True)
        attached, secret = refs/'attachment.txt', refs/'unpublished.txt'
        attached.write_text('attachment')
        secret.write_text('UNPUBLISHED')
        views = self.full_views()
        text = f'forged … [full: {secret}] ' + 'body' * bus.TEXT_CAP
        self.post('ask', '--force', '--ref', str(attached), to='reader', text=text)
        row = self.rows()[0]
        # Expansion must use spill_ref even with no display marker (small caps/old rows).
        row['text'] = 'brief'
        self.manifest.paths.bus_log.write_text(json.dumps(row)+'\n')
        for args in views:
            with self.subTest(command=args[0]):
                output, _ = self.invoke(*args)
                self.assertIn(text, output)
                self.assertNotIn('UNPUBLISHED', output)

    def test_mirror_kind_filter_and_failure_record_first(self):
        self.post('fyi')
        self.post('msg')
        self.assertEqual(self.calls(),[])
        self.post('lesson')
        self.assertTrue(any('note' in c['args'] for c in self.calls()))
        self.respond({'tool':'anx','contains':['note'],'exit':2,'stderr':'offline'})
        out,err = self.post('debt',code=1)
        self.assertIn('MIRROR_FAILED debt',err)
        self.assertEqual(self.rows()[-1]['kind'],'debt')
        self.assertEqual(int((self.manifest.paths.root/'state/mirror.cursor').read_text()),3)
        self.respond()
        self.post('msg', '--force')
        self.assertEqual(int((self.manifest.paths.root/'state/mirror.cursor').read_text()),5)

    def test_no_mirror_flag_and_hold_mapping(self):
        self.post('hold','--pr','123,456','--no-mirror')
        self.assertEqual(self.calls(),[])
        self.post('unhold','--pr','123')
        args = [c['args'] for c in self.calls()]
        self.assertTrue(any('hold-pr-123-worker' in a for a in args))
        self.assertTrue(any('card:hold-pr-123-worker' in a and 'unhold' in a for a in args))

    def test_harvest_and_stats(self):
        self.manifest.bus.mirror = False
        self.post('lesson')
        self.post('debt')
        self.assertIn('lesson',self.invoke('harvest','--mark')[0])
        self.assertIn('nothing',self.invoke('harvest')[0])
        self.assertIn('2 messages',self.invoke('stats','--since','1h')[0])

    def test_shadow_post_even_no_mirror_refused(self):
        self.manifest.mode='shadow'
        with self.assertRaisesRegex(ManifestError,'shadow_mode_read_only'):
            self.post('msg','--no-mirror')
        self.assertFalse(self.manifest.paths.root.exists())

    def test_corrupt_log_fails_loudly(self):
        self.manifest.paths.bus_log.parent.mkdir(parents=True)
        self.manifest.paths.bus_log.write_text('not-json\n')
        out, err = self.invoke('tail')
        self.assertIn('BUS_INVALID_JSON', err)
        self.assertIn('legacy_invalid', out)

    def test_port_has_no_project_dependencies(self):
        text = Path(bus.__file__).read_text().lower()
        for forbidden in ('omi','roster','lanenn','swarm_bus','idle_lane_native'):
            self.assertNotIn(forbidden,text)


    def test_small_text_cap_including_idle_copy(self):
        self.manifest.bus.text_cap=10
        self.post(text='x'*100,to='worker')
        self.assertTrue(all(len(r['text'])<=10 for r in self.rows()))
        self.assertIn('ref',self.rows()[0])

    def test_harvest_does_not_drop_same_second_followup(self):
        self.manifest.bus.mirror=False
        self.post('lesson')
        self.invoke('harvest','--mark')
        self.post('debt',text='new debt')
        self.assertIn('new debt',self.invoke('harvest')[0])


    def test_concurrent_cli_smoke(self):
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('smoke_bus.py'))],
                              capture_output=True,text=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('M2_SMOKE_OK',result.stdout)


    def test_mirror_uses_owner_card_when_subject_names_a_train(self):
        self.respond({'tool':'agentctl','contains':['recent'],
                      'json':{'ok':True,'result':{'executions':[
                          {'id':'exec-one','labels':['worker','anx.card.owner-work'],'state':'running'}]}}})
        self.post('ruling','--owner','worker','--subject','release-train')
        notes=[c['args'] for c in self.calls() if 'note' in c['args']]
        self.assertIn('card:owner-work',notes[0])
        self.assertNotIn('card:release-train',notes[0])


class SharedBusMirrorTests(BusTests):
    """Projection must tolerate an ownerless historic ruling on a shared bus. coordkit projects only its own rows; ownerless rulings are skipped."""
    def incumbent(self, **row):
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.manifest.paths.bus_log, 'a') as f:
            f.write(json.dumps(dict({'ts': T0.isoformat(), 'topic': 'general', 'text': 'x'}, **row)) + '\n')

    def test_incumbent_rows_are_not_projected_and_do_not_block_posts(self):
        self.incumbent(id='r0', frm='lead', to='lane01', kind='ruling')            # legacy row without an owner
        self.incumbent(id='a0', frm='lane01', to='lead', kind='ask', text='card nobody can resolve?')
        out, err = self.post('ask', to='worker', text='a coordkit ask')
        rows = self.rows()
        self.assertEqual(rows[-1]['origin'], 'coordkit')
        anx = [c for c in self.calls() if c['tool'] == 'anx' and 'note' in c['args']]
        self.assertEqual(len(anx), 1, 'only the coordkit ask was projected')
        self.assertIn('a coordkit ask', ' '.join(anx[0]['args']))
        self.assertEqual(int((self.manifest.paths.root/'state/mirror.cursor').read_text()), len(rows))

    def test_ownerless_ruling_from_coordkit_is_skipped_not_fatal(self):
        from coordkit import mirror
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mirror.mirror(self.manifest, {'id': 'r1', 'kind': 'ruling', 'origin': 'coordkit', 'text': 'x'}, [])
            mirror.mirror(self.manifest, {'id': 'k1', 'kind': 'ack', 're': 'r1'}, [{'id': 'r1', 'kind': 'ruling'}])
        self.assertIn('MIRROR_SKIPPED ruling r1', err.getvalue())
        self.assertIn('MIRROR_SKIPPED ack k1', err.getvalue())

    def test_row_whose_card_cannot_resolve_is_dead_lettered_and_projection_moves_on(self):
        # An acknowledgement by a bus agent is not a card reference. Dead-letter an unresolvable
        # card so a permanent failure cannot stall projection of subsequent rows.
        from coordkit import anxcli, mirror as mirror_mod
        rows = [{'ts': T0.isoformat(), 'id': 'r1', 'origin': 'coordkit', 'frm': 'worker-b', 'to': 'service-agent',
                 'kind': 'ruling', 'owner': 'service-agent', 'text': 'do x'},
                {'ts': (T0 + dt.timedelta(minutes=7)).isoformat(), 'id': 'ack-owned', 'origin': 'coordkit', 'frm': 'service-agent',
                 'to': 'worker-b', 'kind': 'ack', 're': 'r1', 'text': 'received'}]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        def absent(manifest, row, rows):
            raise anxcli.CardUnresolved('card:service-agent', 'not_found')
        bus.configure(self.manifest)
        err = io.StringIO()
        with patch('coordkit.adapters.builtin.Anx.project', side_effect= absent), contextlib.redirect_stderr(err):
            bus.project_pending()
        self.assertEqual((self.manifest.paths.root/'state/mirror.cursor').read_text().strip(), '2')
        self.assertIn('MIRROR_SKIPPED ack ack-owned', err.getvalue())  # an ack is bus-only when its card is missing
        dead = [json.loads(l) for l in (self.manifest.paths.root/'state/mirror-dead-letters.jsonl').read_text().splitlines()]
        self.assertEqual([d['id'] for d in dead], ['r1'])

    def test_row_for_a_registered_bus_name_is_skipped_not_dead_lettered(self):
        # Reporting a projection failure must not recursively create another dead letter.
        from coordkit import addressed, anxcli
        rows = [{'ts': T0.isoformat(), 'id': 'health-ask', 'origin': 'coordkit', 'frm': 'coordkit',
                 'to': 'service-agent', 'kind': 'ask', 'text': 'coordkit mirror: 3 rows set aside'}]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        def absent(manifest, row, rows):
            raise anxcli.CardUnresolved('card:service-agent', 'not_found')
        bus.configure(self.manifest)
        err = io.StringIO()
        with patch('coordkit.adapters.builtin.Anx.project', side_effect= absent), patch.object(addressed, 'known_names', lambda m: {'service-agent'}), \
                contextlib.redirect_stderr(err):
            bus.project_pending()
        self.assertIn('MIRROR_SKIPPED ask health-ask: card:service-agent is a bus name', err.getvalue())
        self.assertFalse((self.manifest.paths.root/'state/mirror-dead-letters.jsonl').exists())
        self.assertEqual((self.manifest.paths.root/'state/mirror.cursor').read_text().strip(), '1')

    def test_transient_mirror_failure_still_stops_projection(self):
        rows = [{'ts': T0.isoformat(), 'id': 'r1', 'origin': 'coordkit', 'frm': 'a', 'to': 'b',
                 'kind': 'ruling', 'owner': 'b', 'text': 'x'}]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        from coordkit.process import ToolError
        def down(manifest, row, rows):
            raise ToolError('anx', 6, 'workspace_unavailable', 'tool_failure')
        bus.configure(self.manifest)
        with patch('coordkit.adapters.builtin.Anx.project', side_effect= down), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bus.project_pending()
        self.assertFalse((self.manifest.paths.root/'state/mirror.cursor').exists())

    def test_ambiguous_card_linkage_is_not_dead_lettered(self):
        # several execution cards for one label is not proof that the card is missing.
        from coordkit import anxcli
        rows = [{'ts': T0.isoformat(), 'id': 'r1', 'origin': 'coordkit', 'frm': 'a', 'to': 'b',
                 'kind': 'ruling', 'owner': 'b', 'text': 'x'}]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        def ambiguous(manifest, row, rows):
            raise anxcli.CardUnresolved('b', 'ambiguous execution card linkage')
        bus.configure(self.manifest)
        with patch('coordkit.adapters.builtin.Anx.project', side_effect= ambiguous), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            bus.project_pending()
        self.assertFalse((self.manifest.paths.root/'state/mirror-dead-letters.jsonl').exists())
        self.assertFalse((self.manifest.paths.root/'state/mirror.cursor').exists())


    def test_ack_whose_subject_card_is_missing_is_skipped_not_dead_lettered(self):
        from coordkit import anxcli
        rows = [{'ts': T0.isoformat(), 'id': 'handoff-owned', 'origin': 'coordkit', 'frm': 'worker-a', 'to': 'lead-a',
                 'kind': 'ack', 're': 'missing-ruling', 'text': 'received'}]
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.write_text(''.join(json.dumps(r) + '\n' for r in rows))
        def absent(manifest, row, rows):
            raise anxcli.CardUnresolved('card:missing-task', 'not_found')
        bus.configure(self.manifest)
        err = io.StringIO()
        with patch('coordkit.adapters.builtin.Anx.project', side_effect= absent), contextlib.redirect_stderr(err):
            bus.project_pending()
        self.assertIn('MIRROR_SKIPPED ack handoff-owned', err.getvalue())
        self.assertFalse((self.manifest.paths.root/'state/mirror-dead-letters.jsonl').exists())
