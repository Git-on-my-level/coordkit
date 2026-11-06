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
