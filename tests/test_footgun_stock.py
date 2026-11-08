import contextlib
import io
from coordkit import bus, stock
from coordkit.escalation import validate
from coordkit.manifest import load, ManifestError
from tests.support import FakeCase


class StockTests(FakeCase):
    def test_p1_3_status_stock_and_thresholds_read_only(self):
        self.manifest.bus.mirror = False
        bus.main(['post', '--from', 'worker', '--to', 'lead', '--kind', 'blocker', '--owner', 'worker',
                  '--needs', 'repair', '--subject', 'card:task', '--force', 'stuck'], self.manifest)
        before = {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p != self.log}
        result = stock.stock(self.manifest)
        self.assertEqual(result['blockers']['count'], 1)
        self.assertEqual(result['inbox']['count'], 2)
        self.assertEqual(set(result), set(vars(self.manifest.limits)))
        self.assertTrue(all(x['threshold'] > 0 for x in result.values()))
        self.assertEqual(before, {p:p.read_bytes() for p in self.root.rglob('*') if p.is_file() and p != self.log})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stock.report(self.manifest)
        self.assertIn('STATUS stock blockers count=1', out.getvalue())

    def test_schema_defaults_overrides_and_missing_alarm_path(self):
        self.file.write_text(self.file.read_text()+'\n[limits]\nblockers = 2\n')
        self.assertEqual(load(self.file).limits.blockers, 2)
        self.manifest.escalation.ask_to = ''
        with self.assertRaises(ManifestError): validate(self.manifest)
        self.manifest.escalation.ask_to = 'lead'
        self.manifest.limits.holds = 0
        with self.assertRaises(ManifestError): validate(self.manifest)

    def test_remote_open_asks_are_counted_and_scoped(self):
        self.respond({'tool':'anx','contains':['inbox','list'],'json':{'ok':True,'result':{'items':[
            {'id':'a1','subject_ref':'card:task','created_at':'1970-01-01T00:00:00Z'},
            {'id':'a2','subject_ref':'card:unrelated','created_at':'1970-01-01T00:00:00Z'}]}}})
        result=stock.stock(self.manifest,10000)
        self.assertEqual(result['asks']['count'],1)
        self.assertEqual(result['asks']['oldest'],10000)

    def test_threshold_type_and_unset_alarm_card(self):
        self.file.write_text(self.file.read_text()+'\n[limits]\nmirror = true\n')
        with self.assertRaises(ManifestError): load(self.file)
        self.manifest.escalation.card = ''
        with self.assertRaises(ManifestError): validate(self.manifest)

    def test_dead_letter_stock_counts_only_this_projects_letters(self):
        self.respond({'tool':'agentctl','contains':['anx','status'],'json':{'ok':True,'result':{'dead_letter_count':2,'dead_letters':[
                         {'execution_id':'exec-other','reason':'card_unresolved','created_at':'2026-01-01T00:00:00Z'},
                         {'execution_id':'exec-ours','reason':'card_unresolved','created_at':'2026-01-01T00:00:00Z'}]}}},
                     {'tool':'agentctl','contains':['status','exec-other'],'json':{'ok':True,'result':{'delegation':{'request_key':'other-1'}}}},
                     {'tool':'agentctl','contains':['status','exec-ours'],'json':{'ok':True,'result':{'delegation':{'request_key':'ck-sample-w'}}}})
        self.assertEqual(stock.stock(self.manifest)['dead_letters']['count'], 1)

    def test_report_diffs_against_last_snapshot_with_verified_at(self):
        from unittest.mock import patch
        sample = {'asks': {'count': 1, 'oldest': 60, 'threshold': 10, 'age_unknown': False}}
        def report(at):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = stock.report(self.manifest, at=at)
            return result, out.getvalue()
        with patch.object(stock, 'stock', return_value=sample):
            result, text = report(10000)
            self.assertEqual(result, sample)
            self.assertIn('verified_at=1970-01-01T02:46:40+00:00', text)
            self.assertEqual(report(10060)[1], '')
            sample['asks']['count'] = 0
            self.assertIn('asks count=0', report(10120)[1])
        import json
        snapshot = json.loads((self.manifest.paths.root/'state/stock-last.json').read_text())
        self.assertEqual(snapshot, {'asks': {'count': 0, 'oldest': 60}})
        self.assertEqual(list((self.manifest.paths.root/'state').glob('*.tmp')), [])

    def test_report_full_flag_reproduces_legacy_table(self):
        from unittest.mock import patch
        sample = {'asks': {'count': 1, 'oldest': 60, 'threshold': 10, 'age_unknown': False},
                  'holds': {'count': 2, 'oldest': 90, 'threshold': 3, 'age_unknown': True}}
        with patch.object(stock, 'stock', return_value=sample), contextlib.redirect_stdout(io.StringIO()) as out:
            stock.report(self.manifest, at=10000)
            out.seek(0); out.truncate()
            self.assertEqual(stock.main(['--full'], self.manifest), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([line.split(' verified_at=')[0] for line in lines],
                         ['STATUS stock asks count=1 oldest=60s threshold=10',
                          'STATUS stock holds count=2 oldest=90s threshold=3 age=unknown'])

    def test_machine_lines_echoed_into_status_and_digest(self):
        from unittest.mock import patch
        from coordkit import addressed, machine
        path = self.manifest.paths.root/'state/machine'
        path.mkdir(parents=True)
        local = path/'flags.lines'
        local.write_text('FLAG_OVERDUE_COUNT n=3\nFLAG_DUE_SOON n=1\nFLAG_DARK_OVER_14D n=2\n'
                         'APPROVED_NOT_OWNED a1 31m from=worker\nIGNORE this\nFLAG_DUE_SOONISH no\n')
        external = self.root/'producer-state'
        (external/'lane-watch/machine').mkdir(parents=True)
        (external/'lane-watch/machine/open-counts.lines').write_text('OPEN_COUNTS lead=2 worker=1\n')
        (external/'quiet-prs.lines').write_text('QUIET_PRS_OVER_14D n=1 ids=#123 drafts=0\n')
        self.file.write_text(self.file.read_text() + '\n[paths]\nmachine_state = "producer-state"\n')
        self.manifest = load(self.file)
        expected = machine.lines(self.manifest)
        self.assertEqual(len(expected), 6)
        before = {p: p.read_bytes() for p in [local, *external.rglob('*.lines')]}
        for mode in ('live', 'shadow'):
            self.manifest.mode = mode
            with self.subTest(mode=mode), patch.object(stock, 'stock', return_value={}), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                stock.report(self.manifest, at=10000)
            self.assertEqual(out.getvalue().splitlines(), expected)
            with patch.object(bus, 'main') as post, patch.object(addressed, 'to_relay_file') as relay:
                state = {}
                addressed.flush_lead_digest(self.manifest, state, 10000, out=lambda *_: None)
            text = post.call_args.args[0][-1] if mode == 'live' else relay.call_args.args[1]
            self.assertTrue(all(line in text.splitlines() for line in expected))
            self.assertEqual(state['lead_digest_at'], 10000)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        local.write_text(''.join(f'FLAG_DARK_OVER_14D n={i}\n' for i in range(30)))
        self.assertEqual(len(machine.lines(self.manifest)), 20)
        self.assertNotIn('FLAG_DARK_OVER_14D n=0', machine.lines(self.manifest))
