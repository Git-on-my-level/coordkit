import contextlib
import functools
import importlib
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from coordkit.manifest import load
from coordkit.process import call_json, run as REAL_RUN

FAKE_SOURCES = Path(__file__).parent / 'fakes'
FAKE_CODE = compile((FAKE_SOURCES/'fake_cli.py').read_text(),str(FAKE_SOURCES/'fake_cli.py'),'exec')
LAUNCHCTL_CODE = compile((FAKE_SOURCES/'launchctl').read_text(),str(FAKE_SOURCES/'launchctl'),'exec')

BASE = '''[project]
name = "sample"
git_repo = "owner/repo"
[services]
manager = "launchd"
[anx]
workspace = "anx"
board = "ck-sample"
[agentctl.pools]
implement = "pool:implement"
plan = "pool:plan"
[escalation]
card = "card:task"
[obligations]
coordinator = "coordinator"
[bus]
live_check = false
[[agents]]
label = "worker"
actor = "ck-sample-worker"
'''


class FakeCase(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".coordkit"
        scratch.mkdir(exist_ok=True)
        wake_patch = patch('coordkit.wake.wake_adapters', return_value=[])
        wake_patch.start(); self.addCleanup(wake_patch.stop)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / "coordkit.toml"
        self.file.write_text(BASE)
        self.manifest = load(self.file)
        self.responses = self.root / "responses.json"
        self.responses.write_text("[]")
        self.log = self.root / "calls.jsonl"
        # Use the test runner's interpreter directly. env python3 can select a
        # different, slow interpreter for every fake invocation; -S skips site
        # initialization for these stdlib-only executables. Keep real subprocess
        # boundaries and the same fake argv/response assertions.
        fakes = self.root / 'fakes'
        fakes.mkdir()
        sources = Path(__file__).parent / 'fakes'
        fake_cli = (sources / 'fake_cli.py').read_text().replace(
            'name = os.environ["CK_FAKE_NAME"]', 'name = Path(sys.argv[0]).name')
        for source in sources.iterdir():
            if source.is_file():
                target = fakes / source.name
                # Inline the shared fake implementation instead of importing
                # runpy and starting a second script on every fake call.
                content = fake_cli if source.name in ('anx','agentctl','gh') else source.read_text()
                if content.startswith('#!'):
                    content = '#!'+sys.executable+' -S\n'+content.partition('\n')[2]
                target.write_text(content)
                target.chmod(source.stat().st_mode)
        env = {key: str(fakes / key.lower()) for key in ("ANX", "AGENTCTL", "GH", "LAUNCHCTL")}
        env.update(FAKE_LOG=str(self.log), FAKE_RESPONSES=str(self.responses), COORDKIT_STATE_HOME=str(self.root/'host-state'))
        self.environment = patch.dict(os.environ, env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.retry_delays = []
        if os.environ.get('COORDKIT_TEST_SUBPROCESSES') != '1':
            binaries = {env[key]:key.lower() for key in ('ANX','AGENTCTL','GH','LAUNCHCTL')}
            def fake_run(argv, *, timeout=60, stdin=None, cwd=None):
                name = binaries.get(str(argv[0]))
                if name is None:
                    return REAL_RUN(argv,timeout=timeout,stdin=stdin,cwd=cwd)
                stdout, stderr = io.StringIO(), io.StringIO()
                code = LAUNCHCTL_CODE if name == 'launchctl' else FAKE_CODE
                rc = 0
                with patch.object(sys,'argv',list(map(str,argv))), \
                     patch.object(sys,'stdin',io.StringIO(stdin or '')), \
                     patch.dict(os.environ,CK_FAKE_NAME=name), \
                     contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                    try:
                        exec(code,{'__name__':'__main__'})
                    except SystemExit as exc:
                        rc = exc.code or 0
                return subprocess.CompletedProcess(argv,rc,stdout.getvalue(),stderr.getvalue())
            # Inject only the external tool transport. Domain logic, JSON parsing,
            # error mapping, durable receipts and every existing assertion still run.
            # Real CLI/watcher children use the executable fakes above; all other
            # executables (including process timeout tests) use the real runner.
            for name in ('process','actl','wait','loop','lifecycle','merge'):
                module = importlib.import_module('coordkit.'+name)
                patched = patch.object(module,'run',side_effect=fake_run)
                patched.start(); self.addCleanup(patched.stop)
            for name in ('anxcli','actl'):
                patched = patch('coordkit.'+name+'.call_json',functools.partial(call_json,sleep=self.retry_delays.append))
                patched.start(); self.addCleanup(patched.stop)

    def respond(self, *responses):
        self.responses.write_text(json.dumps(responses))

    def calls(self):
        """Tool calls, plus each coordkit machine finding routed onto the bus as a
        `bus-escalation` pseudo-call with args ['ask', text], so 'an escalation was raised' stays assertable."""
        calls = [json.loads(s) for s in self.log.read_text().splitlines()] if self.log.exists() else []
        return calls + self.escalations()

    def escalations(self):
        bus_log = self.manifest.paths.bus_log
        rows = [json.loads(s) for s in bus_log.read_text().splitlines()] if bus_log.exists() else []
        def full(text):  # a long post spills to a ref file: "... [full: PATH]"
            import re as _re
            m = _re.search(r'\[full: (\S+)\]$', text or '')
            return Path(m.group(1)).read_text().rstrip('\n') if m and Path(m.group(1)).exists() else text
        return [{'tool': 'bus-escalation', 'args': ['ask', full(r.get('text', ''))], 'to': r.get('to')}
                for r in rows if isinstance(r, dict) and r.get('frm') == 'coordkit' and r.get('kind') == 'ask'
                and str(r.get('text', '')).startswith('coordkit ')]
