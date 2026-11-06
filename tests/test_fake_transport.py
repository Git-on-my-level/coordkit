"""Keep injected unit tools equivalent to the executable integration fakes."""
import os
from coordkit import process
from tests.support import FakeCase, REAL_RUN


class FakeTransportTests(FakeCase):
    def test_injected_and_subprocess_fakes_have_identical_outputs_and_logs(self):
        self.respond({'tool':'anx','contains':['error'],'exit':6,'stdout':'failure','stderr':'unavailable'})
        for key,args,stdin in [('ANX',['cards','get','card:loop-audit'],None),
                               ('ANX',['error'],None),
                               ('AGENTCTL',['delegate','--plan','--prompt-stdin'],'read only prompt'),
                               ('GH',['pr','list'],None),('LAUNCHCTL',['list'],None)]:
            with self.subTest(key=key,args=args):
                argv = [os.environ[key],*args]
                injected = process.run(argv,stdin=stdin)
                actual = REAL_RUN(argv,stdin=stdin)
                self.assertEqual((injected.returncode,injected.stdout,injected.stderr),
                                 (actual.returncode,actual.stdout,actual.stderr))
                if key != 'LAUNCHCTL':
                    self.assertEqual(self.calls()[-2],self.calls()[-1])
