#!/usr/bin/env python3
"""Private worker for probe-session-start.py; never run against a live profile.

Invoked during native SessionStart after manually sourcing that hook's env file.
All agentctl writes use a disposable profile/journal; no provider request occurs.
Only sanitized booleans and timings enter the public report. Exact execution IDs
remain in the separate private admission/collection ledger.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from coordkit import deliver
from coordkit.adapters.claude_hook import load


def main():
    receipt, manifest_file, binary, output = map(Path, sys.argv[1:])
    private = json.loads(receipt.read_text())
    session = private['session']
    assert os.environ.get('AGENTCTL_CALLER_PROVIDER') == 'claude'
    assert os.environ.get('AGENTCTL_CALLER_NATIVE_SESSION_ID') == session
    root = receipt.parent
    config, journal = root/'agentctl.json', root/'journal.json'
    config.write_text(json.dumps({'schema_version': 1, 'default_profile': 'probe',
                                 'profiles': {'probe': {}},
                                 'bootstrap': {'instruction_pointers': 'off'},
                                 'anx': {'run_links': {'default': 'off'}}}))
    config.chmod(0o600)
    env = dict(os.environ, AGENTCTL_UPDATE_MODE='off', AGENTCTL_STATE_HOME=str(root/'agentctl-state'),
               AGENTCTL_CONFIG=str(config), COORDKIT_AS='probe')
    agent = [str(binary), '--config', str(config), '--profile', 'probe', '--journal', str(journal),
             '--journal-busy-budget', '1s']
    wrapper = root/'agentctl-isolated'
    wrapper.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> ' + shlex.quote(str(root/'real.calls'))
                       + '\nexec ' + shlex.join(agent) + ' "$@"\n')
    wrapper.chmod(0o700)
    env['AGENTCTL'] = str(wrapper)
    manifest = load(manifest_file)
    manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
    manifest.paths.bus_log.touch()
    # Bus is an empty local fixture: none of these probes posts a row.
    original_manifest = manifest_file.read_text()
    manifest_file.write_text(original_manifest.replace('[launchd]', '[deliver]\nstop_requires_waiter=false\n[launchd]'))
    counter = deliver.stop_counter_path(manifest, 'probe', session)
    payload = json.dumps({'hook_event_name': 'Stop', 'session_id': session})
    report, child, collected = {}, None, False
    ledger = Path(os.environ['PROBE_EXECUTION_LEDGER'])

    def record(execution, phase):
        fd = os.open(ledger, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps({'execution_id': execution, 'phase': phase}) + '\n')

    def native_active():
        os.kill(int(Path(os.environ['PROBE_NATIVE_PID_FILE']).read_text()), 0)
        return True

    def command(args):
        return subprocess.run([*agent, *args], env=env, cwd=root/'cwd',
                              capture_output=True, text=True, timeout=10)

    def stop(*, executable=None, fresh=False):
        if fresh:
            counter.unlink(missing_ok=True)
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-m', 'coordkit.cli', '--manifest', str(manifest_file),
                                 'deliver', '--stop-hook'], input=payload,
                                env=dict(env, AGENTCTL=executable or str(wrapper)), cwd=root/'cwd',
                                capture_output=True, text=True, timeout=7)
        assert result.returncode == 0 and not result.stderr, 'stop_cli_failure'
        value = json.loads(result.stdout) if result.stdout else None
        return value, time.monotonic() - started

    def stub(name, mode):
        target = root/name
        def emit(value):
            return 'printf "%s\\n" ' + shlex.quote(json.dumps(value)) + '\n'
        target.write_text('#!/bin/sh\nif [ "$1 $2" = "help children" ]; then\n'
                          'printf "help\\n" >> "$0.calls"\n' +
                          (emit({'ok': False, 'schema_version': 1, 'error': {'code': 'not_found', 'details': {'topic': 'children'}}}) + 'exit 3\n'
                           if mode == 'old' else
                           emit({'ok': True, 'schema_version': 1, 'result': {'name': 'children', 'side_effect_class': 'read_only'}}) + 'exit 0\n') +
                          'fi\nprintf "query\\n" >> "$0.calls"\n' +
                          ('exec /bin/sleep 2\n' if mode == 'timeout' else
                           emit({'ok': False, 'schema_version': 1, 'error': {'code': 'journal_busy', 'message': 'fixture'}}) + 'exit 6\n'))
        target.chmod(0o700)
        return str(target)

    try:
        report['native_lifecycle_active_at_admission'] = native_active()
        # Attach the empty fixture and warm only capability discovery before any
        # work is launched. The first two Stops with pending work still start
        # from an unused budget; no pending result is waived by this preflight.
        empty, _ = stop(fresh=True)
        report['empty_preflight_allows'] = empty is None
        fixture = ('import json,os; print(json.dumps(dict(type="result",status="completed",'
                   'result=json.dumps(dict(provider_stripped="AGENTCTL_CALLER_PROVIDER" not in os.environ,'
                   'session_stripped="AGENTCTL_CALLER_NATIVE_SESSION_ID" not in os.environ)))))')
        launched = command(['run', '--adapter', 'generic-process', '--timeout', '5s',
                            '--', sys.executable, '-c', fixture])
        assert launched.returncode == 0, 'generic_child_failed'
        admission = json.loads(launched.stdout)['result']
        child = admission['id']
        record(child, 'admitted')
        assert admission['state'] == 'completed', 'child_not_completed'
        identity = json.loads(command(['identity', '--json', '--provider', 'claude',
                                       '--native-session-id', session]).stdout)['result']
        # Compare private correlators internally; never publish native scope IDs.
        caller = admission['caller_identity']
        report['caller_provider_matches'] = caller['provider'] == 'claude'
        report['caller_identity_matches'] = (caller.get('provider') == identity['provider']['id']
                                             and caller.get('native_session_id') == identity['native_session']['id']
                                             and caller.get('provenance') == 'explicit')
        # Explicit unmanaged declarations must not create a false managed parent.
        report['unmanaged_parent_absent'] = admission.get('parent_execution_id') is None
        before = hashlib.sha256(journal.read_bytes()).hexdigest()
        query = command(['children', '--provider', 'claude', '--native-session-id', session])
        page = json.loads(query.stdout)['result']
        report['authoritative_child_found'] = (query.returncode == 0 and page['complete'] is True
                                               and any(row['id'] == child for row in page['executions']))
        report['isolated_query_bytes_unchanged'] = before == hashlib.sha256(journal.read_bytes()).hexdigest()
        first, first_time = stop(fresh=True)
        first_blocks = json.loads(counter.read_text()).get('blocks') if counter.exists() else 0
        second, second_time = stop()
        second_blocks = json.loads(counter.read_text()).get('blocks') if counter.exists() else 0
        third, third_time = stop()
        report['stop_seconds'] = [first_time, second_time, third_time]
        report['budget_counts'] = [first_blocks, second_blocks]
        report['first_decisions'] = [v.get('decision') if v else None for v in (first, second, third)]
        report['first_call_sequence'] = (root/'real.calls').read_text().splitlines()
        report['first_two_block'] = all(value and value.get('decision') == 'block'
                                        and child in value['reason']
                                        and f'result {child} --content' in value['reason']
                                        for value in (first, second))
        report['third_allows'] = third is None
        for mode in ('error', 'timeout', 'old'):
            executable = stub('agentctl-' + mode, mode)
            # First execution of a new script can exceed the 150 ms capability
            # budget under the macOS sandbox. Warm its executable, not the hook
            # capability cache, so the control tests the actual query boundary.
            subprocess.run([executable, 'help', 'children'], env=env, cwd=root/'cwd',
                           capture_output=True, timeout=3)
            prior_calls = Path(executable + '.calls').read_text().splitlines()
            value, elapsed = stop(executable=executable, fresh=True)
            report[mode + '_fails_open'] = value is None
            report[mode + '_seconds'] = elapsed
            calls = Path(executable + '.calls').read_text().splitlines()
            new_calls = calls[len(prior_calls):]
            report[mode + '_help_observed'] = 'help' in new_calls
            if mode == 'old':
                report['old_query_skipped'] = 'query' not in new_calls
            else:
                report[mode + '_actual_query_observed'] = 'query' in new_calls
            if mode == 'old':
                value, _ = stop(executable=executable, fresh=True)
                report['unsupported_cached'] = value is None and Path(executable + '.calls').read_text().splitlines() == calls
        for scope in ('global', 'lane'):
            raw = manifest_file.read_text()
            if scope == 'global':
                changed = raw.replace('stop_requires_waiter=false',
                                      'stop_requires_waiter=false\nstop_requires_agentctl_collection=false')
            else:
                changed = raw + '\nstop_requires_agentctl_collection=false\n'
            manifest_file.write_text(changed)
            value, _ = stop(fresh=True)
            report[scope + '_kill_switch_allows'] = value is None
            manifest_file.write_text(raw)
        # Pending work still blocks with the original executable after controls.
        pending, _ = stop(fresh=True)
        report['pending_after_controls_blocks'] = bool(pending and pending.get('decision') == 'block')
        result = command(['result', child, '--content'])
        assert result.returncode == 0, 'collection_failed'
        collected = True
        record(child, 'collected')
        descendant = json.loads(result.stdout)
        report['descendant_declarations_stripped'] = all(descendant.values())
        value, _ = stop(fresh=True)
        report['collected_fresh_budget_allows'] = value is None
        report['execution_collected'] = True
        report['native_lifecycle_active_after_collection'] = native_active()
        # All pass/fail booleans must succeed; timings/sequences are descriptive.
        assert all(v for v in report.values() if type(v) is bool), 'collection_boolean_failed'
        output.write_text(json.dumps(report, indent=2) + '\n')
        output.chmod(0o600)
    finally:
        manifest_file.write_text(original_manifest)
        # Recover a receipt lost between journal admission and stdout delivery.
        # This journal/profile contains only this probe's native-session child.
        if child is None and journal.exists():
            found = command(['children', '--provider', 'claude', '--native-session-id', session])
            assert found.returncode == 0, 'probe_discovery_cleanup_failed'
            rows = json.loads(found.stdout)['result']['executions']
        else:
            rows = [{'id': child}] if child is not None and not collected else []
        for row in rows:
            result = command(['await', row['id'], '--content', '--timeout', '5s'])
            if result.returncode != 0:
                raise RuntimeError('probe_collection_cleanup_failed')
            record(row['id'], 'collected_in_cleanup')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Private handles/tracebacks must never reach native hook stdout.
        detail = str(exc) if isinstance(exc, AssertionError) else type(exc).__name__
        trace = exc.__traceback__
        lines = []
        partial = {}
        while trace:
            if trace.tb_frame.f_code.co_filename == __file__:
                lines.append(trace.tb_lineno)
                partial.update(trace.tb_frame.f_locals.get('report', {}))
            trace = trace.tb_next
        Path(sys.argv[4]).write_text(json.dumps(dict(partial, probe_failure=detail, worker_lines=lines)))
        raise SystemExit(1)
