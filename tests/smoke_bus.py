"""Local transport smoke: independent CLI processes, temporary state, no external writes."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time


def main():
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='bus-smoke-',dir=repo/'.coordkit') as temp:
        root = Path(temp)
        manifest = root/'coordkit.toml'
        manifest.write_text('''[project]
name = "smoke"
git_repo = "owner/repo"
[anx]
workspace = "anx"
board = "unused"
[agentctl.pools]
implement = "pool:implement"
[bus]
live_check = false
mirror = false
[[agents]]
label = "worker"
actor = "ck-smoke-worker"
''')
        env = dict(os.environ,PYTHONPATH=str(repo/'src'))
        command = [sys.executable,'-m','coordkit.cli','--manifest',str(manifest),'bus']
        # Arm a cursor before posting, then a separate process consumes it.
        first = subprocess.run([*command,'wait','--to','worker','--timeout','0'],env=env,capture_output=True,text=True)
        assert first.returncode == 3, first.stderr
        waiter = subprocess.Popen([*command,'wait','--to','worker','--timeout','0.2','--poll','0.05'],env=env,
                                  stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            posted = subprocess.run([*command,'post','--from','lead','--to','worker','--kind','ruling',
                                     '--owner','worker','smoke ruling'],env=env,capture_output=True,text=True,check=True)
            out,err = waiter.communicate(timeout=15)
            assert waiter.returncode == 0 and 'ruling' in out,(out,err)
            row = json.loads((root/'.coordkit/bus.jsonl').read_text().splitlines()[0])
            ack = subprocess.run([*command,'ack',row['id'],'--from','worker'],env=env,capture_output=True,text=True,check=True)
            opened = subprocess.run([*command,'open-rulings','--json'],env=env,capture_output=True,text=True,check=True)
            assert json.loads(opened.stdout)==[],opened.stdout
            print('M2_SMOKE_OK post/wait/ack; no open rulings; temporary state only')
        finally:
            if waiter.poll() is None:
                waiter.terminate()
                waiter.communicate(timeout=5)


if __name__ == '__main__':
    main()
