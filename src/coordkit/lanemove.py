"""Receipt-first Claude lane relocation. Never signal, kill, or delete a source session."""
import contextlib
from dataclasses import asdict, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib

from . import addressed, bus, lane, obligations, routing, usage
from .adapters import claude_resume
from .manifest import UsageLane, load
from .process import ToolError
from .state import name_key, read_json, write_json

PHASES = ('freeze', 'quiesce', 'final-state', 'stop', 'copy', 'manifest', 'resume', 're-arm', 'done')
SESSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
EXIT_GUIDANCE = ('After recording every background shell in move-state, run /exit and choose '
                 '"Exit and stop tasks" (stop background shells), not background or stay. '
                 'Coordkit never kills; lingering shell children block quiescence.')


class Refusal(ToolError):
    def __init__(self, reason, detail=''):
        super().__init__('lane-move', 9, detail, reason)


def home(manifest):
    return manifest.paths.root/'state'/'lane-moves'


def save(plan):
    write_json(Path(plan['plan_file']), plan)


@contextlib.contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal('move_locked', str(path)) from None
        yield


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def reading(manifest, target):
    at = datetime.now(timezone.utc)
    identity = routing.roster_identity(manifest, target.name)
    observed, observed_at = usage.observed_model([usage.transcript_dir(target)/f'{target.session_id}.jsonl'])
    return dict(at=at.isoformat(), config_dir=target.config_dir,
                account=Path(target.config_dir).name, session_id=target.session_id,
                roster_identity=identity, observed_model=observed, model_observed_at=observed_at,
                usage=asdict(usage.collect([target], at)[0][1]))


def publish(manifest, plan, suffix, kind, text, *, to='all', **fields):
    """Use bus --id replay semantics; synthetic posts never impersonate the lane."""
    rid = plan['id'] + ('-' + suffix if suffix else '')
    args = ['post', '--from', 'coordkit', '--to', to, '--kind', kind,
            '--topic', 'lane-move', '--id', rid, '--force', '--ref', plan['plan_file']]
    if kind == 'handoff':
        args += ['--evidence', plan['plan_file']]
    for key, value in fields.items():
        args += ['--' + key.replace('_', '-'), value]
    result = bus.main(args + [text], manifest)
    if result:
        raise Refusal('publication_failed', rid)
    return rid


def phase(manifest, plan, name):
    # Projection first: a crash before saving retries exactly the same bus/event IDs.
    publish(manifest, plan, name, 'fyi', f"Lane {plan['name']} move {name}; receipt {plan['id']}.")
    eid = plan['id'] + '-' + name
    rows = (manifest.paths.events.read_text().splitlines() if manifest.paths.events.exists() else [])
    if not any(json.loads(row).get('id') == eid for row in rows):
        lane.event(manifest, dict(id=eid, kind='lane-move', label=plan['name'], phase=name,
                                  plan_file=plan['plan_file']))
    if name not in plan['completed']:
        plan['completed'].append(name)
    plan['phase'] = name
    plan.pop('attention', None)
    save(plan)


def attention(manifest, plan, reason):
    plan['attention'] = reason
    save(plan)
    publish(manifest, plan, 'attention-' + reason, 'ask',
            f"Lane {plan['name']} move needs attention: {reason}. No process was signalled. "
            'Resolve the condition, then rerun the same lane-move command; see receipt.',
            to=manifest.escalation.ask_to)
    raise Refusal(reason, plan['plan_file'])


def lane_for(manifest, name):
    matches = [row for row in manifest.lanes if row.name == name]
    if len(matches) != 1:
        raise Refusal('lane_not_declared', name)
    return matches[0]


def prepare(manifest, name, destination):
    plans = [value for path in home(manifest).glob('*.json')
             if isinstance(value := read_json(path), dict) and 'plan_file' in value]
    active = [p for p in plans if p['name'] == name and 'done' not in p['completed']]
    if len(active) > 1:
        raise Refusal('multiple_moves', name)
    if active:
        if active[0]['manifest'] != str(manifest.file):
            raise Refusal('move_manifest_mismatch', active[0]['plan_file'])
        if active[0]['after']['config_dir'] != destination:
            raise Refusal('move_in_progress', active[0]['plan_file'])
        return active[0]
    current = lane_for(manifest, name)
    # Repeating a completed command is a no-op, even after the destination grew.
    previous = [p for p in plans if p['name'] == name and p['manifest'] == str(manifest.file)
                and p['after'] == asdict(current)]
    if current.config_dir == destination and previous:
        return max(previous, key=lambda p: p['id'])
    if str(Path(current.config_dir).resolve()) == destination:
        raise Refusal('same_config', destination)
    identity = routing.roster_identity(manifest, name)
    session = current.session_id or identity.get('session_id', '')
    if not SESSION.fullmatch(session):
        raise Refusal('session_identity_required', name)
    before = replace(current, session_id=session)
    after = replace(before, config_dir=destination)
    source = usage.transcript_dir(before)/f'{session}.jsonl'
    if not source.is_file() or source.is_symlink():
        raise Refusal('session_file_missing_or_symlink', str(source))
    rid = f'lane-move-{name}-{time.time_ns()}'
    plan = dict(version=1, id=rid, name=name, manifest=str(manifest.file),
                plan_file=str(home(manifest)/(rid+'.json')), phase='planned', completed=[],
                declared_before=asdict(current), before=asdict(before), after=asdict(after),
                source=str(source), destination=str(usage.transcript_dir(after)/source.name),
                stop_policy='verify-only', session_policy='copy-preserve-original',
                before_reading=reading(manifest, before))
    plan['move_state_file'] = str(Path(plan['plan_file']).with_suffix('.move-state.json'))
    save(plan)
    return plan


def tracked_holders(manifest, plan, snapshot):
    """Remember descendants before /exit, including children later reparented to init."""
    recorded = plan.setdefault('holders', {})
    live = {int(pid) for pid, row in recorded.items()
            if snapshot.get(int(pid), {}).get('started') == row['started']}
    live.update(pid for pid, row in snapshot.items() if plan['before']['session_id'] in row['argv'])
    identity = routing.roster_identity(manifest, plan['name'])
    pid = identity.get('pid')
    if pid in snapshot and snapshot[pid]['started'] == identity.get('pid_started'):
        live.add(pid)
    if snapshot and not live:
        live.update(claude_resume.transcript_holders(manifest, UsageLane(**plan['before'])) & snapshot.keys())
    while True:
        children = {pid for pid, row in snapshot.items() if row.get('ppid') in live}
        if children <= live:
            break
        live.update(children)
    for pid in live:
        previous = recorded.get(str(pid), {})
        recorded[str(pid)] = dict(snapshot[pid], origin_parent=previous.get('origin_parent', snapshot[pid].get('ppid')))
    save(plan)
    return live


def move_state(manifest, plan):
    """The lane completes this inventory before acknowledging freeze and exiting."""
    path = Path(plan.setdefault('move_state_file', str(Path(plan['plan_file']).with_suffix('.move-state.json'))))
    snapshot = claude_resume.processes()
    tracked_holders(manifest, plan, snapshot)
    if not path.exists():
        command = [sys.executable, '-m', 'coordkit.cli', '--manifest', str(manifest.file), 'bus']
        write_json(path, dict(goal='', open_prs=[], lanes_in_flight=[], bus_name=plan['name'], next_steps=[],
                             session_id=plan['before']['session_id'], transcript=plan['source'],
                             inventory_complete=False, background_shells=[],
                             waiters=[dict(name=plan['name'], flags=['--to', plan['name'], '--own-asks'])],
                             re_arms=[dict(name=plan['name'], kind='bus-waiter',
                                           argv=command + ['wait', '--to', plan['name'], '--own-asks']),
                                      dict(name='heartbeat', kind='heartbeat', argv=['agent-heartbeat', '50'],
                                           env=dict(AGENT_HEARTBEAT_KEY=plan['before']['session_id']),
                                           verify_argv=['agent-heartbeat', 'status'])]))
    state = read_json(path)
    if not isinstance(state, dict) or not isinstance(state.get('background_shells'), list):
        raise Refusal('invalid_move_state', str(path))
    for shell in state.get('background_shells', []):
        if not isinstance(shell, dict):
            raise Refusal('invalid_background_shell', str(path))
        pid = shell.get('pid')
        if type(pid) is not int or pid <= 0:
            raise Refusal('invalid_background_shell_pid', str(path))
        current = snapshot.get(pid)
        if current and (not shell.get('started') or current['started'] == shell['started']):
            previous = plan['holders'].get(str(pid), {})
            plan['holders'][str(pid)] = dict(current, origin_parent=previous.get('origin_parent', current.get('ppid')))
    save(plan)
    # Keep observed process evidence alongside the lane's explicitly recorded restart recipes.
    state['observed_holders'] = plan['holders']
    write_json(path, state)
    return state


def final_state(manifest, plan):
    state = move_state(manifest, plan)
    entries = state.get('re_arms', [])
    if state.get('inventory_complete') is not True or not isinstance(entries, list) or not entries:
        raise Refusal('move_state_inventory_required', plan['move_state_file'])
    if state.get('bus_name') != plan['name']:
        raise Refusal('move_state_lane_mismatch', plan['move_state_file'])
    names = set()
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not entry['name'] or entry['name'] in names
                or not isinstance(entry.get('argv'), list) or not entry['argv']
                or not all(isinstance(arg, str) and arg for arg in entry['argv'])
                or entry.get('kind') not in ('bus-waiter', 'heartbeat', 'monitor', 'watcher', 'background-shell')):
            raise Refusal('invalid_rearm_entry', plan['move_state_file'])
        names.add(entry['name'])
        if entry.get('verify_argv') is not None and (not isinstance(entry['verify_argv'], list)
                or not entry['verify_argv'] or not all(isinstance(arg, str) and arg for arg in entry['verify_argv'])):
            raise Refusal('invalid_rearm_verifier', entry['name'])
        if entry['kind'] == 'bus-waiter' and ('wait' not in entry['argv'] or not any(
                flag in entry['argv'] and len(entry['argv']) > entry['argv'].index(flag)+1
                for flag in ('--to', '--signal', '--subject'))):
            raise Refusal('invalid_bus_waiter', entry['name'])
        if entry['kind'] == 'heartbeat' and not entry.get('verify_argv'):
            raise Refusal('heartbeat_verifier_required', entry['name'])
        if not isinstance(entry.get('env', {}), dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in entry.get('env', {}).items()):
            raise Refusal('invalid_rearm_environment', entry['name'])
    if not any(e['kind'] == 'heartbeat' for e in entries) or not any(
            e['kind'] == 'bus-waiter' and '--to' in e['argv']
            and e['argv'][e['argv'].index('--to')+1:e['argv'].index('--to')+2] == [plan['name']]
            for e in entries):
        raise Refusal('default_rearms_required', plan['move_state_file'])
    if any(not isinstance(shell, dict) or (shell.get('rearm') not in names and not shell.get('completed_reason'))
           for shell in state.get('background_shells', [])):
        raise Refusal('background_shell_rearm_required', plan['move_state_file'])
    # Every observed descendant must have a restart recipe (or an explicit completed-task reason).
    covered = {str(shell.get('pid')) for shell in state.get('background_shells', [])}
    children = {pid for pid, row in plan['holders'].items()
                if str(row.get('origin_parent', row.get('ppid'))) in plan['holders']}
    if not children <= covered:
        raise Refusal('background_shell_inventory_required', ','.join(sorted(children - covered)))
    state['waiters'] = [dict(name=e['name'], flags=e['argv'][e['argv'].index('wait')+1:])
                        for e in entries if e['kind'] == 'bus-waiter']
    write_json(Path(plan['move_state_file']), state)
    plan['move_state'] = state
    save(plan)
    return state


def open_work(manifest, plan):
    bus.configure(manifest)
    # No age window: even an old unpicked obligation must block a move.
    name = bus.resolve(plan['name'])
    return [item for item in obligations.report(manifest) if item['owner'] == name]


def quiet(manifest, target, idle, plan=None):
    snapshot = claude_resume.processes()
    if plan and tracked_holders(manifest, plan, snapshot):
        return False, 'background_holder_present'
    if claude_resume.holder_present(manifest, target, snapshot):
        return False, 'holder_present'
    # Include explicitly bound alternate paths: a stale canonical path cannot hide activity.
    for path in claude_resume.transcript_paths(manifest, target):
        try:
            if time.time() - path.stat().st_mtime < idle:
                return False, 'transcript_active'
        except FileNotFoundError:
            continue
    return True, ''


def quiesce(manifest, plan, timeout, idle):
    deadline = time.monotonic() + timeout
    while True:
        ready, reason = quiet(manifest, UsageLane(**plan['before']), idle, plan)
        if ready:
            # Destination must also be free; it may already contain an old copy.
            ready, reason = quiet(manifest, UsageLane(**plan['after']), idle, plan)
        if ready:
            return
        if time.monotonic() >= deadline:
            attention(manifest, plan, reason + '_timeout')
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def atomic_bytes(path, data, *, exclusive=False, mode=0o600):
    """Fsync staging then publish; exclusive copies never overwrite an existing session."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.lane-move-', dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def copy_session(plan):
    source, destination = Path(plan['source']), Path(plan['destination'])
    if source.is_symlink():
        raise Refusal('source_symlink', str(source))
    digest = checksum(source)
    if plan.get('copy_sha256') and plan['copy_sha256'] != digest:
        raise Refusal('source_changed', str(source))
    # Receipt precedes the copy. Recovery accepts an identical published file only.
    plan['copy_sha256'] = digest
    save(plan)
    if destination.is_symlink():
        raise Refusal('destination_symlink', str(destination))
    if not destination.exists():
        try:
            atomic_bytes(destination, source.read_bytes(), exclusive=True)
        except FileExistsError:
            pass  # Another writer won: verify, never overwrite it.
    if checksum(source) != digest or checksum(destination) != digest:
        raise Refusal('copy_checksum_mismatch', str(destination))
    plan['copy_verified_at'] = time.time()
    save(plan)


def edited_manifest(data, plan):
    """Rewrite only the selected scalar lane table; prove TOML semantics before publishing."""
    text = data.decode('utf-8')
    raw = tomllib.loads(text)
    expected = tomllib.loads(text)
    target = next(row for row in expected['lanes'] if row['name'] == plan['name'])
    target.update(config_dir=plan['after']['config_dir'], session_id=plan['after']['session_id'])
    headers = list(re.finditer(r'^\s*\[.*\][ \t]*(?:#.*)?$', text, re.M))
    for index, header in enumerate(headers):
        if not re.fullmatch(r'\s*\[\[\s*lanes\s*\]\][ \t]*(?:#.*)?', header[0]):
            continue
        end = headers[index+1].start() if index+1 < len(headers) else len(text)
        try:
            block = tomllib.loads(text[header.start():end])['lanes'][0]
        except (ValueError, KeyError, IndexError):
            continue
        if block.get('name') != plan['name']:
            continue
        block.update(config_dir=target['config_dir'], session_id=target['session_id'])
        replacement = '[[lanes]]\n' + ''.join(f'{key} = {json.dumps(value, ensure_ascii=False)}\n'
                                              for key, value in block.items()) + '\n'
        updated = text[:header.start()] + replacement + text[end:]
        if tomllib.loads(updated) == expected and raw != expected:
            return updated.encode('utf-8')
    raise Refusal('manifest_edit_unsupported', 'Cannot isolate the lane table without changing other TOML values')


def update_manifest(manifest, plan):
    with locked(manifest.file.with_name(manifest.file.name + '.lane-move.lock')):
        current = load(manifest.file)
        declared = asdict(lane_for(current, plan['name']))
        if declared == plan['after']:
            if not plan.get('manifest_backup'):
                raise Refusal('manifest_changed_without_receipt', plan['name'])
            if checksum(Path(plan['manifest_backup'])) != plan['manifest_before_sha256']:
                raise Refusal('manifest_backup_changed', plan['manifest_backup'])
            return current
        if declared != plan['declared_before']:
            raise Refusal('manifest_lane_changed', plan['name'])
        data = manifest.file.read_bytes()
        # Load and byte snapshot are separate reads. Do not edit a different lane
        # version if an uncooperative writer changed the document between them.
        raw_lane = next(row for row in tomllib.loads(data.decode('utf-8'))['lanes']
                        if row['name'] == plan['name'])
        raw_target = UsageLane(**raw_lane)
        raw_target.config_dir = str(manifest.path(raw_target.config_dir))
        raw_target.cwd = str(manifest.path(raw_target.cwd))
        if asdict(raw_target) != declared:
            raise Refusal('manifest_lane_changed', plan['name'])
        updated = edited_manifest(data, plan)
        backup = Path(plan['plan_file']).with_suffix('.manifest.bak')
        if not backup.exists():
            atomic_bytes(backup, data, exclusive=True)
        plan['manifest_backup'] = str(backup)
        backup_digest = checksum(backup)
        if plan.get('manifest_before_sha256') and plan['manifest_before_sha256'] != backup_digest:
            raise Refusal('manifest_backup_changed', str(backup))
        plan['manifest_before_sha256'] = backup_digest
        plan['manifest_input_sha256'] = hashlib.sha256(data).hexdigest()
        plan['manifest_after_sha256'] = hashlib.sha256(updated).hexdigest()
        save(plan)
        if manifest.file.read_bytes() != data:
            raise Refusal('manifest_changed', str(manifest.file))
        atomic_bytes(manifest.file, updated, mode=manifest.file.stat().st_mode & 0o777)
        return load(manifest.file)


def operator_command(target):
    return 'cd ' + shlex.quote(target.cwd) + ' && ' + shlex.join(
        ['env', 'CLAUDE_CONFIG_DIR=' + target.config_dir, 'COORDKIT_AS=' + target.name,
         'claude', '--resume', target.session_id])


def rearmed(manifest, plan):
    name, since = plan['name'], plan['resume_requested_at']
    # Plain listening() accepts old posts and busy transcripts. Neither proves re-arm.
    if not addressed.listening(manifest, name)[0]:
        return False
    if addressed.listening(manifest, name, waiter_only=True)[0]:
        if not plan.get('waiter_was_armed', True):
            return True
        for base in {manifest.paths.bus_log.parent/'state/cursors', manifest.paths.root/'state/cursors'}:
            cursor = base/(name_key(name) + '.wait')
            if cursor.exists() and cursor.stat().st_mtime >= since:
                return True
    note = read_json(routing.home(manifest)/'hooks'/f'{name_key(name)}.json', {}) or {}
    return (note.get('name') == name and note.get('session_id') == plan['after']['session_id']
            and note.get('at', 0) >= since
            and str(Path(note.get('transcript_path', '')).expanduser().resolve()) == plan['destination'])


def rearm_env(plan, entry):
    env = dict(os.environ)
    # These are the mover's identity, not the resumed lane's. In particular a
    # heartbeat must not treat the coordinator's turns as wakes for this lane.
    for key in ('CLAUDE_PID', 'AGENTCTL_EXECUTION_ID', 'CODEX_THREAD_ID'):
        env.pop(key, None)
    return {**env, 'CLAUDE_CONFIG_DIR': plan['after']['config_dir'], 'COORDKIT_AS': plan['name'],
            'CLAUDE_CODE_SESSION_ID': plan['after']['session_id'], **entry.get('env', {})}


def verify_rearm(manifest, plan, entry, outcome, snapshot):
    """Verify this entry, never substitute a lane-wide listening() result."""
    pid = outcome.get('pid')
    current = snapshot.get(pid, {})
    if not pid or not outcome.get('started') or current.get('started') != outcome['started']:
        return False
    if entry['kind'] == 'bus-waiter':
        argv = shlex.split(current.get('argv', ''))
        # A live generic waiter with different flags cannot satisfy the recorded recipe.
        if 'wait' not in argv or argv[argv.index('wait'):] != entry['argv'][entry['argv'].index('wait'):]:
            return False
        if '--to' in entry['argv'] and not any(flag in entry['argv'] for flag in ('--signal', '--subject')):
            name = entry['argv'][entry['argv'].index('--to')+1]
            cursor = manifest.paths.bus_log.parent/'state/cursors'/(name_key(name)+'.wait')
            if not cursor.exists() or cursor.stat().st_mtime < outcome['requested_at']:
                return False
    if entry.get('verify_argv'):
        result = subprocess.run(entry['verify_argv'], cwd=plan['after']['cwd'], env=rearm_env(plan, entry),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2)
        if result.returncode:
            return False
        if entry['kind'] == 'heartbeat':
            try:
                status = json.loads(result.stdout)
            except ValueError:
                return False
            if (not isinstance(status, dict) or status.get('event') != 'arm'
                    or str(status.get('owner', '')).split(':')[0] != str(pid)):
                return False
    return True


def rearm_all(manifest, plan, no_rearm=False):
    state = plan.get('move_state')
    if not state:  # Older, paused receipts acquire the same mandatory inventory.
        state = final_state(manifest, plan)
    outcomes = plan.setdefault('rearm_outcomes', {})
    for index, entry in enumerate(state['re_arms']):
        outcome = outcomes.setdefault(entry['name'], {})
        if not outcome:
            outcome.update(status='skipped' if no_rearm else 'intent', requested_at=time.time(),
                           argv=entry['argv'], no_rearm=no_rearm)
            save(plan)  # Never repeat an ambiguous spawn after a crash.
            if not no_rearm:
                try:
                    log_path = Path(plan['plan_file']).with_suffix(f'.rearm-{index}.log')
                    with log_path.open('ab') as log:
                        os.chmod(log.name, 0o600)
                        child = subprocess.Popen(entry['argv'], cwd=plan['after']['cwd'], env=rearm_env(plan, entry),
                                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                                 start_new_session=True)
                    outcome.update(status='started', pid=child.pid, log=str(log_path),
                                   started=claude_resume.start_time(child.pid))
                except (OSError, subprocess.SubprocessError) as exc:
                    outcome.update(status='failed', error=str(exc))
                save(plan)
        if no_rearm:
            # Operator supplies the exact resumed process identity in move-state for manual arms.
            manual = next((e for e in read_json(Path(plan['move_state_file']))['re_arms']
                           if e['name'] == entry['name']), {})
            if manual.get('pid') and manual.get('started'):
                outcome.update(pid=manual['pid'], started=manual['started'], no_rearm=True,
                               manual_recovery_at=time.time())
    # Probe once after all starts: a successful spawn alone never proves readiness.
    snapshot = claude_resume.processes()
    complete = True
    for entry in state['re_arms']:
        outcome = outcomes[entry['name']]
        try:
            verified = verify_rearm(manifest, plan, entry, outcome, snapshot)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            outcome['verification_error'] = str(exc)
            verified = False
        outcome['verified'] = verified
        outcome['checked_at'] = time.time()
        if verified:
            outcome['status'] = 'verified'
        elif outcome['status'] == 'verified':
            outcome['status'] = 'verification_pending'
        save(plan)
        complete = verified and complete
    return complete


def resume(manifest, plan):
    target = UsageLane(**plan['after'])
    plan['operator_command'] = operator_command(target)
    if 'resume_requested_at' not in plan:
        plan['waiter_was_armed'] = addressed.listening(manifest, target.name, waiter_only=True)[0]
        plan['resume_requested_at'] = time.time()
        save(plan)
    print('LANE_MOVE_RESUME ' + plan['operator_command'], flush=True)
    print('LANE_MOVE_REARM Rerun --apply after resume: arm and verify every move-state entry '
          '(--no-rearm verifies operator-managed entries). Outcomes and output logs are in the receipt.', flush=True)
    if target.resume != 'headless' or plan.get('launch'):
        return
    ready, reason = quiet(manifest, target, 0)
    if not ready:
        attention(manifest, plan, reason)
    if checksum(Path(plan['destination'])) != plan['copy_sha256']:
        raise Refusal('copy_checksum_mismatch', plan['destination'])
    if checksum(Path(plan['source'])) != plan['copy_sha256']:
        raise Refusal('source_changed', plan['source'])
    # Write intent BEFORE Popen. An uncertain launch is never automatically repeated.
    plan['launch'] = dict(status='intent', requested_at=time.time())
    save(plan)
    inbox = shlex.join(['coordkit', '--manifest', str(manifest.file), 'bus', 'inbox', target.name, '--full'])
    prompt = (f'Your bus name is {target.name}. Read your inbox with {inbox}. '
              f'Read move-state {plan["move_state_file"]}. '
              'Lane-move arms and verifies its recorded waiters, heartbeat and monitors by default; '
              'do not create duplicate waiters. With --no-rearm the operator handles each entry. '
              'Take no new work until the lane-move handoff confirms all re-arms.')
    argv = ['claude', '--resume', target.session_id, '-p', prompt]
    with Path(plan['plan_file']).with_suffix('.resume.log').open('ab') as log:
        os.chmod(log.name, 0o600)
        child = subprocess.Popen(argv, cwd=target.cwd,
                                 env=dict(os.environ, CLAUDE_CONFIG_DIR=target.config_dir, COORDKIT_AS=target.name),
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    plan['launch'] = dict(status='started', pid=child.pid, argv=argv, started_at=time.time())
    save(plan)


def apply(manifest, plan, timeout, idle, no_rearm=False):
    if 'done' in plan['completed']:
        print('LANE_MOVE_DONE ' + plan['plan_file'])
        return 0
    current = asdict(lane_for(load(manifest.file), plan['name']))
    if current not in (plan['declared_before'], plan['after']):
        raise Refusal('manifest_lane_changed', plan['name'])
    if 'manifest' not in plan['completed']:
        move_state(manifest, plan)
        print('LANE_MOVE_STATE ' + plan['move_state_file'], flush=True)
        print('LANE_MOVE_EXIT ' + EXIT_GUIDANCE, flush=True)
        publish(manifest, plan, 'freeze-ask', 'ask',
                f"Lane {plan['name']}: finish the current turn, take no new work; ack this freeze "
                f"only after picking up all open obligations and completing {plan['move_state_file']} "
                '(goal, open PRs, lanes/exec IDs, bus name, next steps, every re-arm and background shell). '
                + EXIT_GUIDANCE,
                to=plan['name'])
        work = open_work(manifest, plan)
        if work:
            raise Refusal('open_obligations', ','.join(item['id'] for item in work))
        final_state(manifest, plan)
        phase(manifest, plan, 'freeze')
        # Hold both account/session locks across quiesce, copy and manifest commit.
        # Other coordkit resumes share these locks; external holders are probed afresh.
        with contextlib.ExitStack() as locks:
            for target in (UsageLane(**plan['before']), UsageLane(**plan['after'])):
                if locks.enter_context(claude_resume.session_lock(target)) is None:
                    raise Refusal('session_resume_locked', target.config_dir)
            quiesce(manifest, plan, timeout, idle)
            phase(manifest, plan, 'quiesce')
            final_state(manifest, plan)
            publish(manifest, plan, 'final-state-post', 'state',
                    f"Lane {plan['name']} final state: session={plan['before']['session_id']} "
                    f"transcript={plan['source']} move-state={plan['move_state_file']}", subject='lane-move', state='DONE')
            phase(manifest, plan, 'final-state')
            quiesce(manifest, plan, timeout, idle)
            phase(manifest, plan, 'stop')
            if not plan.get('copy_sha256'):
                plan['before_reading'] = reading(manifest, UsageLane(**plan['before']))
                save(plan)
            copy_session(plan)
            phase(manifest, plan, 'copy')
            # Copy and phase projection may take time; revalidate immediately before edit.
            if open_work(manifest, plan):
                raise Refusal('open_obligations', plan['name'])
            quiesce(manifest, plan, timeout, 0)  # copied mtime is new; source digest proves stability
            if checksum(Path(plan['source'])) != plan['copy_sha256']:
                raise Refusal('source_changed', plan['source'])
            manifest = update_manifest(manifest, plan)
            phase(manifest, plan, 'manifest')
    else:
        manifest = load(manifest.file)
        if asdict(lane_for(manifest, plan['name'])) != plan['after']:
            raise Refusal('manifest_lane_changed', plan['name'])
    if 'resume' not in plan['completed']:
        # If a launch intent survived a crash, only real re-arm evidence allows progress.
        with claude_resume.session_lock(UsageLane(**plan['after'])) as lock:
            if lock is None:
                raise Refusal('session_resume_locked', plan['name'])
            resume(manifest, plan)
        phase(manifest, plan, 'resume')
    else:
        print('LANE_MOVE_RESUME ' + plan['operator_command'], flush=True)
    deadline = time.monotonic() + timeout
    # Prove the destination resumed before creating waiters: our own waiter is not resume evidence.
    while not plan.get('resume_verified_at'):
        if rearmed(manifest, plan):
            plan['resume_verified_at'] = time.time()
            save(plan)
            break
        if time.monotonic() >= deadline:
            attention(manifest, plan, 'rearm_timeout')
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    while not rearm_all(manifest, plan, no_rearm):
        if time.monotonic() >= deadline:
            attention(manifest, plan, 'rearm_entries_timeout')
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    phase(manifest, plan, 're-arm')
    if 'after_reading' not in plan:
        plan['after_reading'] = reading(manifest, UsageLane(**plan['after']))
        save(plan)
    publish(manifest, plan, '', 'handoff',
            f"Lane {plan['name']} moved: {plan['before']['config_dir']} -> {plan['after']['config_dir']}. "
            'Listening verified. Before/after usage, roster/account and observed model are in the receipt.',
            to=manifest.escalation.ask_to)
    phase(manifest, plan, 'done')
    print('LANE_MOVE_DONE ' + plan['plan_file'])
    return 0


def main(argv, manifest):
    from .ops import parser_for
    parser = parser_for('lane-move')
    args = parser.parse_args(argv)
    if args.timeout < 0 or args.idle_seconds < 0:
        parser.error('--timeout and --idle-seconds must be nonnegative')
    manifest.writable()
    destination = str(Path(args.to_config).expanduser().resolve())
    with locked(home(manifest)/(name_key(args.name)+'.lock')):
        plan = prepare(manifest, args.name, destination)
        print('LANE_MOVE_PLAN ' + plan['plan_file'])
        if not args.apply:
            print(json.dumps(plan, indent=2))
            return 0
        return apply(manifest, plan, args.timeout, args.idle_seconds, args.no_rearm)
