"""Fault and concurrency regressions for every review-2 finding."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys

from coordkit import bus, cli, lifecycle
from coordkit.manifest import ManifestError
from tests.support import FakeCase


class Review2Tests(FakeCase):
    def quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fn(*args, **kwargs)

    def test_1_shadow_abbreviations_and_equals_never_write(self):
        self.file.write_text('mode = "shadow"\n'+self.file.read_text())
        for args in [['harvest','--ma'],['inbox','worker','--unr'],
                     ['open-rulings','--new-o',str(self.root/'seen')],
                     ['open-rulings','--new-only='+str(self.root/'seen')],
                     ['wait','--to=worker','--timeout=0'], ['harvest','--mark']]:
            try:
                result=self.quiet(cli.main,['--manifest',str(self.file),'bus',*args])
            except SystemExit as exc:
                result=exc.code
            self.assertEqual(result,2,args)
            self.assertFalse(self.manifest.paths.root.exists())
            self.assertFalse((self.root/'seen').exists())
        self.assertEqual(self.calls(),[])

    def test_2_legacy_baseline_and_new_dead_letter(self):
        self.manifest.launchd.path=[str(Path.home()/'.local/bin'),'/usr/bin','/bin']
        def status(count, rows):
            return {'tool':'agentctl','contains':['anx','status'],
                    'json':{'ok':True,'result':{'dead_letter_count':count,'dead_letters':rows}}}
        good=[{'tool':'anx','contains':['orient'],'json':{'ok':True,'result':{'principal':{'id':'lead'}}}},
              {'tool':'anx','contains':['boards','get'],'json':{'ok':True,'result':{'board':{'id':'board'}}}},
              {'tool':'agentctl','contains':['config','doctor'],'json':{'ok':True,'result':{'valid':True}}}]
        old={'execution_id':'legacy','reason':'card_unresolved'}
        self.respond(status(256,[old]),*good)
        self.quiet(lifecycle.record_dead_letter_baseline,self.manifest)
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),0)
        self.respond(status(256,[dict(old,attempts=99)]),*good)
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),0)
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest,True),1)
        self.respond(status(257,[old,{'execution_id':'coordkit-new','labels':['ck-sample-worker'],'reason':'card_unresolved'}]),*good)
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),1)
        # Replacement at an unchanged total is still a new failure.
        self.respond(status(256,[{'execution_id':'coordkit-new','reason':'card_unresolved'}]),*good)
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),1)
        # The spool is fleet-wide: another project's new letter at a capped total stays green.
        foreign={'execution_id':'exec-other','reason':'card_unresolved'}
        self.respond(status(256,[foreign]),{'tool':'agentctl','contains':['status','exec-other'],
                     'json':{'ok':True,'result':{'delegation':{'request_key':'titlestall-01'}}}},*good)
        self.assertEqual(self.quiet(lifecycle.doctor,self.manifest),0)
        with self.assertRaisesRegex(ManifestError,'baseline_exists'):
            lifecycle.record_dead_letter_baseline(self.manifest)


    def test_5_concurrent_waiters_deliver_row_once(self):
        bus.configure(self.manifest)
        bus.set_cursor('worker','wait',0)
        self.quiet(bus.main,['post','--from','lead','--to','worker','--kind','ask','--force','unique-work'],self.manifest)
        command=[sys.executable,'-m','coordkit.cli','--manifest',str(self.file),'bus','wait','--to','worker','--timeout','0','--poll','0.01']
        processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(2)]
        try:
            outputs=[p.communicate(timeout=10) for p in processes]
            self.assertEqual(sorted(p.returncode for p in processes),[0,3])
            self.assertEqual(sum('unique-work' in out for out,err in outputs),1)
        finally:
            for p in processes:
                if p.poll() is None: p.kill(); p.wait(timeout=5)

    def test_6_identical_id_retry_and_conflicting_payload(self):
        args=['post','--from','lead','--to','worker','--kind','ask','--id','stable','--force','original']
        self.quiet(bus.main,args,self.manifest)
        self.quiet(bus.main,args,self.manifest)
        for changed in [args[:-1]+['different'],args[:4]+['lead']+args[5:],
                        [a if a!='ask' else 'fyi' for a in args]]:
            with self.assertRaisesRegex(ManifestError,'publication_id_conflict'):
                self.quiet(bus.main,changed,self.manifest)
        self.assertEqual(len([r for r in bus.read() if r['id']=='stable']),1)
        # Idle-copy prefixes can cause a spill even below the configured text cap.
        self.manifest.bus.text_cap=50
        long_args=[a if a!='stable' else 'spill-id' for a in args[:-1]]+['x'*45]
        self.quiet(bus.main,long_args,self.manifest)
        self.quiet(bus.main,long_args,self.manifest)
        self.assertEqual(len([r for r in bus.read() if r['id']=='spill-id']),1)
