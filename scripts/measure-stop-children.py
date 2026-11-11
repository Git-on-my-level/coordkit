#!/usr/bin/env python3
"""Measure a read-only Stop handler; optionally launch/collect one synthetic child.

Run from a checkout with PYTHONPATH=src. The binary is never installed. The
--exercise-child option explicitly writes one synthetic execution to the binary's
default journal and collects only that execution. No model calls or bus posts.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

from coordkit import deliver, process, stop_agentctl
from coordkit.adapters.claude_hook import load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agentctl', required=True)
    parser.add_argument('--scratch-dir')
    parser.add_argument('--runs', type=int, default=20)
    parser.add_argument('--exercise-child', action='store_true')
    parser.add_argument('--full-cli', action='store_true', help='also measure 20 complete CLI invocations, including startup')
    args = parser.parse_args()
    if not 1 <= args.runs <= 100:
        parser.error('--runs must be between 1 and 100')
    executable = str(Path(args.agentctl).resolve())
    report = dict(measured_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  binary_sha256=hashlib.sha256(Path(executable).read_bytes()).hexdigest(),
                  binary_version=process.call_json([executable, '--version'], timeout=2, retries=0)['version'])
    calls, samples = [], []
    original = process.call_json

    def observed(argv, **kwargs):
        started = time.perf_counter()
        entry = {'command': argv[1]}
        try:
            value = original(argv, **kwargs)
            entry.update(outcome='ok', complete=value.get('complete'), count=value.get('count'))
            return value
        except Exception as exc:
            entry['outcome'] = getattr(exc, 'category', type(exc).__name__)
            raise
        finally:
            entry['ms'] = (time.perf_counter() - started) * 1000
            calls.append(entry)

    with tempfile.TemporaryDirectory(dir=args.scratch_dir) as temporary:
        root = Path(temporary)
        file = root/'coordkit.toml'
        file.write_text('[project]\nname="stop-benchmark"\ngit_repo="owner/repo"\n'
                        '[deliver]\nstop_requires_waiter=false\n'
                        '[addressed.agents.benchmark]\nruntime="claude-code"\n')
        manifest = load(file)
        manifest.paths.bus_log.parent.mkdir(parents=True)
        manifest.paths.bus_log.touch()
        session = 'stop-validation-' + uuid.uuid4().hex
        payload = json.dumps(dict(hook_event_name='Stop', session_id=session))
        os.environ.update(AGENTCTL=executable, COORDKIT_AS='benchmark')
        try:
            process.call_json = observed
            for _ in range(args.runs):
                out = io.StringIO()
                started = time.perf_counter()
                assert deliver.run_stop_hook(manifest, payload, out) == 0
                samples.append((time.perf_counter() - started) * 1000)
                assert not out.getvalue(), 'synthetic empty caller unexpectedly blocked'
        finally:
            process.call_json = original
        queries = [item for item in calls if item['command'] == 'children']
        assert len(queries) == args.runs and all(q['outcome'] == 'ok' and q['complete'] is True for q in queries)
        report.update(runs=args.runs, method='in-process run_stop_hook; first capability probe included; no Python startup/manifest loading',
                      hook_ms=samples, p50_ms=statistics.median(samples),
                      p95_ms=sorted(samples)[math.ceil(.95 * len(samples)) - 1],
                      percentile_method='nearest rank', calls=calls)
        if args.full_cli:
            cli_samples = []
            for _ in range(args.runs):
                started = time.perf_counter()
                result = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--manifest', str(file),
                                         'deliver', '--stop-hook'], input=payload, env=os.environ,
                                        capture_output=True, text=True, timeout=7)
                cli_samples.append((time.perf_counter() - started) * 1000)
                assert result.returncode == 0 and not result.stdout and not result.stderr, 'full CLI failed'
            report['full_cli'] = dict(runs=args.runs, ms=cli_samples,
                p50_ms=statistics.median(cli_samples), p95_ms=sorted(cli_samples)[math.ceil(.95 * len(cli_samples)) - 1],
                method='CLI subprocess including Python startup/manifest load; capability cache already warm; empty synthetic caller; quiet exit 0 for every run',
                exit_errors=0)
        if args.exercise_child:
            env = dict(os.environ, AGENTCTL_UPDATE_MODE='off', AGENTCTL_CALLER_PROVIDER='claude',
                       AGENTCTL_CALLER_NATIVE_SESSION_ID=session)
            child = None
            acknowledged = False
            def command(argv):
                return subprocess.run([executable, '--journal-busy-budget', '2s', *argv],
                                      env=env, cwd=root, text=True, capture_output=True, timeout=15)
            def stop():
                result = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--manifest', str(file),
                                         'deliver', '--stop-hook'], input=payload, env=env, text=True,
                                        capture_output=True, timeout=7)
                assert result.returncode == 0 and not result.stderr, 'hook CLI failed'
                return json.loads(result.stdout) if result.stdout else None
            try:
                fixture = 'import json; print(json.dumps(dict(type="result", status="completed", result="synthetic child collected")))'
                launched = command(['run', '--adapter', 'generic-process', '--idempotency-key', session,
                                    '--', sys.executable, '-c', fixture])
                assert launched.returncode == 0, 'synthetic launch failed'
                admission = json.loads(launched.stdout)['result']
                child = admission['id']
                assert admission['state'] == 'completed' and admission['caller_identity']['provider'] == 'claude'
                blocked = stop()
                assert blocked and blocked['decision'] == 'block' and child in blocked['reason']
                collected = command(['result', child, '--content'])
                assert collected.returncode == 0 and collected.stdout == 'synthetic child collected'
                acknowledged = True
                assert stop() is None, 'collection did not clear Stop guard'
                report['blocking_case'] = dict(execution_collected=True, state=admission['state'],
                                               attribution='launch-time caller_identity from explicit unmanaged Claude declaration; no labels',
                                               before_collection='block', after_collection='allow',
                                               collection_output=collected.stdout,
                                               hook='real CLI subprocess against default journal')
            finally:
                # Recover the uniquely scoped fixture even if admission returned no receipt.
                if child is None:
                    discovered = command(['children', '--provider', 'claude', '--native-session-id', session])
                    if discovered.returncode == 0:
                        rows = json.loads(discovered.stdout)['result']['executions']
                        for row in rows:
                            command(['await', row['id'], '--content', '--timeout', '5s'])
                elif not acknowledged:
                    command(['await', child, '--content', '--timeout', '5s'])
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
