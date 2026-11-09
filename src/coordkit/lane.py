"""Fenced card ownership and durable agentctl terminal watching."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from . import bus, escalation
from .actl import AgentctlClient
from .anxcli import AnxClient
from .manifest import duration
from .process import ToolError, parse_ts
from .state import read_json, write_json

TERMINAL = {'completed', 'failed', 'cancelled', 'orphaned', 'launch_failed'}


class Refusal(ToolError):
    def __init__(self, reason, detail=''):
        super().__init__('lane', 9, detail, reason)


def event(manifest, row, *, once=False):
    """Append a standalone, synced, verified row; optionally deduplicate its ID."""
    manifest.writable()
    path = manifest.paths.events
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        end = stream.seek(0, os.SEEK_END)
        if end:
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b'\n':
                # Preserve the interrupted writer's bytes, but fence them off from
                # this receipt. Coordkit writers share this lock and repair;
                # agentctl callbacks append independently.
                stream.write(b'\n')
                stream.flush()
                print('warning: newline-terminated torn lane event tail', file=sys.stderr)
        if once:
            from .usage import tail_lines
            recorded, malformed = False, 0
            for line in tail_lines(path, 1024 * 1024):
                try:
                    parsed = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                if not isinstance(parsed, dict):
                    malformed += 1
                    continue
                if parsed.get('id') == row['id'] and all(parsed.get(k) == v for k, v in row.items()):
                    recorded = True
            if malformed:
                print(f'warning: skipped {malformed} malformed lane event row(s)', file=sys.stderr)
            if recorded:
                os.fsync(stream.fileno())  # Also commit any tail repair before phase completion.
                return
        value = dict(ts=dt.datetime.now(dt.timezone.utc).isoformat(), **row)
        payload = (json.dumps(value)+'\n').encode('utf-8')
        stream.write(payload)
        stream.flush()
        # O_APPEND sets this descriptor's position to the end of its own write.
        # A callback on another descriptor can append before or after us without
        # changing that position. A pre-write EOF snapshot is not our offset.
        offset = stream.tell() - len(payload)
        os.fsync(stream.fileno())
        stream.seek(offset)
        appended = stream.read(len(payload))
        if appended != payload or json.loads(appended) != value:
            raise Refusal('event_append_unverified', str(path))


def handoff_actor(manifest):
    who = manifest.lane.card_handoff
    return 'actor:' + (manifest.anx.lead_actor if who == 'lead' else who.removeprefix('actor:'))


def lease(manifest, plan, verb):
    if not plan.get('card'):
        return
    args = ['lease', verb, plan['card'], '--owner', plan['label']]
    if verb == 'acquire':
        try:
            AnxClient(manifest).call(*args, '--ttl', manifest.lane.lease_ttl, write=True)
        except ToolError as exc:
            if exc.exit_code != 13:
                raise
            # `anx cards create` leases the card to its creator, usually this lead; hand that
            # lease to the lane atomically instead of refusing our own card.
            client = AnxClient(manifest)
            owner = (client.call('lease','show',plan['card']).get('lease') or {}).get('owner','')
            lead = {handoff_actor(manifest)} | {'actor:' + name for name in client.identities()}
            if owner not in lead:
                raise
            client.call('lease','release',plan['card'],'--owner',owner,'--handoff-to',plan['label'],
                        '--ttl',manifest.lane.lease_ttl,'--reason','lead hands card to lane',write=True)
        print(f"LANE_CARD_LEASE {plan['label']} {verb} {plan['card']}", flush=True)
        return
    if verb == 'renew':
        args += ['--ttl', manifest.lane.lease_ttl]
    else:
        current = AnxClient(manifest).call('lease','show',plan['card']).get('lease') or {}
        if current.get('owner') == handoff_actor(manifest):
            return  # Release succeeded before a lost terminal receipt.
        if current.get('owner') and current['owner'].removeprefix('actor:') != plan['label']:
            raise Refusal('lease_owner_changed', current['owner'])
        args += ['--handoff-to', handoff_actor(manifest)]
    AnxClient(manifest).call(*args, write=True)
    print(f"LANE_CARD_LEASE {plan['label']} {verb} {plan['card']}", flush=True)


def launch_run(manifest, plan, suffix='', pool=None, prompt=None):
    directory = manifest.paths.out_dir/'req'/(plan['label']+suffix)
    directory.mkdir(parents=True, exist_ok=True)
    # A different capacity-fallback pool replaces a pinned selector. Relaunching
    # the plan's own pool keeps the selector the run was launched with.
    selector = plan.get('selector') if pool in (None, plan.get('pool')) else None
    raw = AgentctlClient(manifest).delegate(prompt or plan['prompt'], directory,
            key=plan['key']+suffix, pool=pool or plan['pool'], selector=selector, label=plan['label'], detach=True,
            card=plan.get('card'), owns=plan['owns'], access=plan['access'], timeout='20m', wakeable=True)
    doc = json.loads(raw)
    if doc.get('ok') is not True:
        raise ToolError('agentctl',6,'unsuccessful delegate receipt','protocol_error')
    result = doc.get('result',{})
    execution = result.get('id') or result.get('execution_id')
    if not execution:
        raise ToolError('agentctl',6,'missing execution id','protocol_error')
    plan['execution_id'] = execution
    plan['phase'] = 'running'
    plan.pop('rate_limit', None)
    write_json(Path(plan['plan_file']),plan)
    event(manifest, {'kind':'launched','execution_id':execution,'label':plan['label'], 'card':plan.get('card'), 'owns':plan['owns']})
    AgentctlClient(manifest).subscribe(execution)
    return execution


def unread(manifest, plan):
    bus.configure(manifest)
    rows = bus.read()
    closed = {r.get('re') for r in rows if r.get('kind') in ('ack','answer','decision')}
    return [r for r in rows[plan['bus_start']:] if r.get('kind') in ('ruling','ask')
            and bus.for_lane(r,plan['label']) and r.get('id') not in closed]


def can_continue(manifest, plan):
    if not manifest.lane.autocontinue_hours or plan.get('autocontinued'):
        return False
    if time.time()-plan['started'] >= manifest.lane.autocontinue_hours*3600:
        return False
    if any(plan['label'] in e.get('labels',[]) for e in AgentctlClient(manifest).recent(nonterminal=True)):
        return False
    return bool(unread(manifest,plan))


def alarm(manifest, plan, cause, detail):
    sent = plan.setdefault('alarms', {})
    if cause not in sent:
        escalation.ask(manifest, plan.get('card'), kind='lanes', situation=f'Lane {plan["label"]} {cause}: {detail}',
            impact='This lane cannot safely make progress or release ownership.',
            recommend='Inspect the execution, reconcile effects and explicitly disposition or relaunch the work.',
            why='Automated recovery cannot prove a safe next step.')
        sent[cause] = time.time()
        write_json(Path(plan['plan_file']), plan)


def expired(manifest, plan):
    return time.time()-plan['started'] >= manifest.lane.max_run_hours*3600


HUNG_LIVENESS = {'unreachable', 'lost', 'exited', 'orphaned'}

# Provider throttling is not a hung process. agentctl does not publish a stable
# rate-limit field yet (another change is adding retry events under `status`), so
# read every shape we know tolerantly, but only structured 429/rate-limit markers
# and only while the evidence is fresh enough to still explain the silence.
RATE_LIMIT_STATES = {'rate_limited', 'rate-limited', 'paused_rate_limit', 'paused_provider_rate_limit'}
RATE_LIMIT_CODES = {'provider_rate_limit', 'provider_rate_limited', 'rate_limited',
                    'rate-limit', 'provider.rate-limit', 'quota_exceeded'}
RATE_LIMIT_EVENT_TYPES = {'retry', 'rate_limit', 'rate-limit'}
RATE_LIMIT_EVIDENCE_BOUND = 5 * 60.0


def _epoch(value):
    """Best-effort epoch seconds from a numeric tick or an ISO timestamp."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value >= 10 ** 11 else value if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        try:
            number = float(text)
        except ValueError:
            try:
                return parse_ts(text).timestamp()
            except (AttributeError, TypeError, ValueError):
                return None
        return number / 1000 if number >= 10 ** 11 else number if number >= 0 else None
    return None


def _evidence_time(value):
    if not isinstance(value, dict):
        return None
    for key in ('observed_at', 'updated_at', 'occurred_at', 'timestamp', 'created_at', 'time_created', 'at', 'ts'):
        at = _epoch(value.get(key))
        if at is not None:
            return at
    return None


def _rate_limit_marker(value):
    if not isinstance(value, dict):
        return False
    return (value.get('status') == 429 or value.get('status_code') == 429
            or value.get('code') in RATE_LIMIT_CODES or value.get('type') in RATE_LIMIT_CODES
            or value.get('category') in RATE_LIMIT_CODES)


def rate_limit(status, now=None):
    """Return fresh retry evidence when a nonterminal run is waiting on provider 429s."""
    if not isinstance(status, dict):
        return None
    now = time.time() if now is None else now
    status_at = _evidence_time(status)
    candidates = []

    def add(fields, at=None):
        at = status_at if at is None else at
        candidates.append((now if at is None else at, fields))

    state = status.get('state')
    if state in RATE_LIMIT_STATES:
        add(dict(retry_after_ms=status.get('retry_after_ms'),
                 reset_at=status.get('reset_at'), model=status.get('model'), status=429))
    for key in ('rate_limit', 'rateLimit'):
        container = status.get(key)
        if isinstance(container, dict):
            add(container, _evidence_time(container))
    # A bare `retry` object is not proof of throttling: require a 429/rate-limit marker.
    retry = status.get('retry')
    if _rate_limit_marker(retry):
        add(retry, _evidence_time(retry))
    for key in ('failure', 'error'):
        failure = status.get(key)
        if _rate_limit_marker(failure):
            add(failure, _evidence_time(failure))
    if _rate_limit_marker(status):
        add(status, status_at)
    events = status.get('events')
    if isinstance(events, list):
        for row in reversed(events[-64:]):
            if not isinstance(row, dict) or row.get('type') not in RATE_LIMIT_EVENT_TYPES:
                continue
            error = row.get('error') if isinstance(row.get('error'), dict) else row
            if not _rate_limit_marker(error):
                continue
            add(error, _evidence_time(row) or _evidence_time(error))
            break
    if not candidates:
        return None
    at, found = max(candidates, key=lambda item: item[0])
    found = dict(found)
    retry = found.get('retry_after_ms')
    if retry is None:
        retry = found.get('retryAfterMs')
    retry = retry if type(retry) in (int, float) and retry >= 0 else None
    # A provider retry window makes its own evidence relevant for twice that long,
    # but a small 429 window must not pin a lane forever on a retained old event.
    window = max(2 * (retry / 1000 if retry is not None else 0), RATE_LIMIT_EVIDENCE_BOUND)
    if at is None or at < now - window:
        return None
    return {'retry_after_ms': retry,
            'reset_at': found.get('reset_at') or found.get('resets_at'),
            'model': found.get('model') or found.get('model_id'),
            'status': found.get('status') or 429}


def note_rate_limit(manifest, plan, evidence):
    """Persist retry evidence and emit one event per distinct retry window."""
    record = dict(evidence, observed_at=time.time())
    previous = plan.get('rate_limit') or {}
    record['since'] = previous.get('since') or record['observed_at']
    changed = {k: v for k, v in previous.items() if k != 'observed_at'} != {
        k: v for k, v in record.items() if k != 'observed_at'}
    plan['rate_limit'] = record
    write_json(Path(plan['plan_file']), plan)
    if changed:
        event(manifest, {'kind': 'rate_limited', 'execution_id': plan.get('execution_id'),
                         'label': plan['label'], 'retry_after_ms': record['retry_after_ms'],
                         'reset_at': record['reset_at'], 'model': record['model']})
        retry = record['retry_after_ms']
        print(f'LANE_RATE_LIMITED {plan["label"]} retry_after_ms={retry if retry is not None else "unknown"}', flush=True)



def hung(manifest, status, now=None):
    """A nonterminal run whose process agentctl cannot reach, with no update for hung_after."""
    if status.get('state') in TERMINAL or status.get('liveness') not in HUNG_LIVENESS:
        return False
    updated = status.get('updated_at') or status.get('started_at')
    try:
        age = (now or time.time()) - parse_ts(updated).timestamp()  # naive stamps are UTC
    except (AttributeError, TypeError, ValueError):
        return False  # without a clock, never declare a run dead
    return age >= duration(manifest.lane.hung_after)


def expire(manifest, plan, reason='deadline_exceeded'):
    """Stop renewal immediately. Release only after the execution is proven stopped."""
    output = manifest.paths.out_dir/(plan['label']+'.out')
    why = ('max_run_hours exceeded' if reason == 'deadline_exceeded'
           else f'execution unreachable for {manifest.lane.hung_after}')
    output.write_text(f'FAILED: lane {why}.\n')
    plan['completion'] = {'rc':1, 'state':'failed', 'out':str(output)}
    plan['phase'] = 'deadline_exceeded'
    write_json(Path(plan['plan_file']), plan)
    alarm(manifest, plan, reason, why+'; lease renewal stopped')
    client = AgentctlClient(manifest)
    execution = plan.get('execution_id')
    if not execution:
        raise Refusal('launch_outcome_uncertain', plan['label'])
    if client.status(execution).get('state') not in TERMINAL:
        client.cancel(execution)
        if client.status(execution).get('state') not in TERMINAL:
            raise Refusal('deadline_cancel_unconfirmed', execution)
    return finish(manifest, plan)


def _watch(manifest, plan):
    manifest.writable()
    client = AgentctlClient(manifest)
    label, execution = plan['label'], plan['execution_id']
    attempts = plan.get('relaunches',0)
    output = manifest.paths.out_dir/(label+'.out')
    ignore_attention = False
    while True:
        if expired(manifest, plan):
            return expire(manifest, plan)
        remaining = max(1, int(manifest.lane.max_run_hours*3600-(time.time()-plan['started'])))
        try:
            content = client.await_result(execution, timeout=f'{min(remaining,max(1,duration(manifest.lane.lease_ttl)//3))}s', ignore_attention=ignore_attention)
            output.write_text(content)
            code, state = 0, 'completed'
        except ToolError as exc:
            if exc.exit_code == 9:  # await timeout: renew only while the run is nonterminal
                if expired(manifest, plan):
                    return expire(manifest, plan)
                status = client.status(execution)
                if not status.get('state'):
                    alarm(manifest, plan, 'execution_state_unknown', execution)
                    raise Refusal('execution_state_unknown', execution)
                # A run waiting on provider 429 retries is throttled, not hung: keep its
                # lease and let agentctl's backoff finish instead of cancelling work.
                evidence = rate_limit(status)
                if evidence and status.get('state') not in TERMINAL:
                    note_rate_limit(manifest, plan, evidence)
                    lease(manifest,plan,'renew')
                    continue
                # Re-read before cancelling: a run that answered again is not hung.
                if hung(manifest, status) and hung(manifest, client.status(execution)):
                    return expire(manifest, plan, 'execution_hung')
                if status.get('state') not in TERMINAL:
                    lease(manifest,plan,'renew')
                continue
            status = client.status(execution)
            failure = status.get('failure') or {}
            retryable = (failure.get('code') == 'provider_capacity' or failure.get('kind') == 'capacity'
                         or (failure.get('retryable') is True and failure.get('category') in ('capacity','provider_capacity','paused_capacity'))
                         or status.get('state') == 'paused_capacity')
            if retryable and attempts >= manifest.lane.capacity_relaunch_max:
                alarm(manifest, plan, 'capacity_exhausted', str(exc))
            if retryable and attempts < manifest.lane.capacity_relaunch_max:
                if status.get('state') not in TERMINAL:
                    client.cancel(execution)  # prove cancellation before the next launch
                    if client.status(execution).get('state') not in TERMINAL:
                        raise Refusal('parked_execution_not_cancelled',execution)
                attempts += 1
                chain = manifest.lane.capacity_fallback or [plan['pool']]
                plan['relaunches'] = attempts
                previous = execution
                recovery = f'Reconcile effects from prior execution {previous} before continuing. Never repeat a completed side effect.\n\n'+plan['prompt']
                execution = launch_run(manifest,plan,f'-r{attempts}',chain[min(attempts-1,len(chain)-1)],prompt=recovery)
                event(manifest,{'kind':'relaunched','execution_id':execution,'previous_execution_id':previous,'label':label})
                print(f'LANE_RELAUNCHED {label} exec={execution} attempt={attempts}',flush=True)
                continue
            if status.get('state') not in TERMINAL:
                alarm(manifest, plan, 'execution_state_unknown', str(exc))
                event(manifest,{'kind':'attention','execution_id':execution,'label':label,'reason':str(exc)})
                if exc.exit_code == 10:
                    ignore_attention = True
                    continue
                raise  # unknown runs retain their lease; never launch alongside them
            output.write_text(str(exc)+'\n')
            code, state = exc.exit_code, status['state']
        if code == 0 and can_continue(manifest,plan):
            plan['autocontinued'] = True
            prompt = plan['prompt']+'\nAddress these unread obligations:\n'+json.dumps(unread(manifest,plan))
            previous = execution
            execution = launch_run(manifest,plan,'-ac',prompt=prompt)
            event(manifest,{'kind':'autocontinued','execution_id':execution,'previous_execution_id':previous,'label':label})
            print(f'LANE_AUTOCONTINUE {label} exec={execution}',flush=True)
            continue
        plan['completion'] = {'rc':code, 'state':state, 'out':str(output)}
        plan['phase'] = 'completing'
        write_json(Path(plan['plan_file']),plan)
        return finish(manifest,plan)


def finish(manifest,plan):
    """Replay a persisted completion; each remote projection is retry safe."""
    execution, label = plan['execution_id'], plan['label']
    result = plan['completion']
    code, state, output = result['rc'], result['state'], result['out']
    if plan.get('card'):
        if code == 0:
            bus.main(['post','--from',label,'--to','lead','--kind','handoff','--subject',plan['card'],
                      '--evidence',output,'--id','lane-'+execution,'--force',f'Lane {label} completed.'],manifest)
        elif plan.get('phase') != 'released':
            client = AnxClient(manifest)
            client.note(plan['card'],f'Lane {label} stopped: {state}',evidence=output)
            # A failed lane's card must not keep looking in progress with nobody working it.
            client.call('cards','move',plan['card'],'--column','ready',write=True)
        lease(manifest,plan,'release')
    plan['phase'] = 'released'
    write_json(Path(plan['plan_file']),plan)
    # A crash after append but before plan save must not duplicate the terminal row.
    rows = [json.loads(line) for line in manifest.paths.events.read_text().splitlines()] if manifest.paths.events.exists() else []
    if not any(r.get('kind') == 'terminal' and r.get('execution_id') == execution for r in rows):
        event(manifest, {'kind':'terminal','execution_id':execution,'label':label,'state':state,
                         'rc':code,'card':plan.get('card'),'out':output,
                         'handoff':handoff_actor(manifest) if plan.get('card') else None, 'handoff_how':'lease_handoff'})
    plan['terminal'], plan['phase'] = state, 'terminal'
    write_json(Path(plan['plan_file']),plan)
    print(f'LANE_EXIT {label} rc={code} exec={execution} out={output}',flush=True)
    return code


def watch(manifest, plan):
    manifest.writable()
    lock_path = Path(plan['plan_file']).with_suffix('.watch.lock')
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal('watcher_already_running',plan['label'])
        plan = read_json(Path(plan['plan_file']),plan)
        plan['watcher_pid'] = os.getpid()
        write_json(Path(plan['plan_file']),plan)
        if plan.get('terminal'):
            return 0
        if plan.get('phase') == 'deadline_exceeded':
            return expire(manifest, plan)
        return finish(manifest,plan) if plan.get('completion') else _watch(manifest,plan)


def spawn_watcher(*args, **kwargs):
    return subprocess.Popen(*args, **kwargs)


def detach_watcher(manifest,plan):
    log = manifest.paths.out_dir/(plan['label']+'.watch.log')
    with log.open('a') as stream:
        process = spawn_watcher([sys.executable,'-m','coordkit.cli','--manifest',str(manifest.file),
                                'lane','_watch',plan['plan_file']], stdin=subprocess.DEVNULL,
                                stdout=stream,stderr=subprocess.STDOUT,start_new_session=True)
    # Child owns updates to the plan, avoiding a parent/child lost-update race.
    print(f'LANE_WATCHER {plan["label"]} pid={process.pid} plan={plan["plan_file"]}',flush=True)
    return process


def compensate_launch(manifest,plan):
    # A failed receipt may still have started work. Prove absence before release.
    active = AgentctlClient(manifest).recent(nonterminal=True)
    if any(plan['label'] in e.get('labels',[]) for e in active):
        raise Refusal('launch_outcome_uncertain',plan['label'])
    lease(manifest,plan,'release')
    if plan.get('card'):
        AnxClient(manifest).call('cards','move',plan['card'],'--column','ready',write=True)
    plan['phase'], plan['terminal'] = 'terminal', 'launch_failed'
    write_json(Path(plan['plan_file']),plan)


def _recover(manifest,plan,dry_run=False):
    """Reconciler supervision; watcher kernel lock is the authority, PID diagnostic only."""
    if plan.get('terminal'):
        return False
    if dry_run:
        print(f'RECONCILE watcher would_check {plan["label"]}')
        return False
    lock_path = Path(plan['plan_file']).with_suffix('.watch.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        plan = read_json(Path(plan['plan_file']),plan)
        if plan.get('terminal'):
            return False
        if plan.get('phase') == 'deadline_exceeded':
            expire(manifest, plan)
            return True
        if plan.get('completion'):
            finish(manifest,plan)
            return True
        if expired(manifest, plan):
            expire(manifest, plan)
            return True
        client = AgentctlClient(manifest)
        if not plan.get('execution_id'):
            matches = [e for e in client.recent(label=plan['label']) if plan['label'] in e.get('labels',[])]
            if len(matches) > 1:
                raise Refusal('launch_outcome_uncertain',plan['label'])
            if not matches:
                compensate_launch(manifest,plan)
                return True
            plan['execution_id'] = matches[0]['id']
            write_json(Path(plan['plan_file']),plan)
        status = client.status(plan['execution_id'])
        if status.get('state') not in TERMINAL:
            if not status.get('state'):
                raise Refusal('execution_state_unknown',plan['execution_id'])
            evidence = rate_limit(status)
            if evidence:
                note_rate_limit(manifest, plan, evidence)
            elif hung(manifest, status) and hung(manifest, client.status(plan['execution_id'])):
                expire(manifest, plan, 'execution_hung')
                return True
            # The watcher is gone, so a throttled run's lease may have lapsed too.
            # Renew, or reacquire when ANX says this lane still owns it (exit 2/13).
            try:
                lease(manifest,plan,'renew')
            except ToolError as exc:
                if exc.exit_code not in (2,13):
                    raise
                current = AnxClient(manifest).call('lease','show',plan['card']).get('lease') or {}
                if current.get('owner','').removeprefix('actor:') not in ('',plan['label']):
                    raise Refusal('lease_owner_changed',current['owner']) from exc
                lease(manifest,plan,'acquire')
            if evidence:
                detach_watcher(manifest,plan)
                print(f'RECONCILE watcher restored {plan["label"]} (rate limited)')
                return True
        else:
            # Terminal recovery runs synchronously before the zombie-lease sweep.
            return _watch(manifest,plan) == 0
    detach_watcher(manifest,plan)
    print(f'RECONCILE watcher restored {plan["label"]}')
    return True


def recover(manifest, plan, dry_run=False):
    try:
        return _recover(manifest, plan, dry_run)
    except ToolError as exc:
        if dry_run:
            raise
        # _recover reloads under its lock; never overwrite that newer snapshot.
        plan = read_json(Path(plan['plan_file']), plan)
        if plan.get('phase') != 'deadline_exceeded':
            alarm(manifest, plan, exc.category, str(exc))
        print(f'RECONCILE lane attention {plan["label"]} {exc.category}')
        return True


def main(argv, manifest):
    """Internal watcher entry point used by reconciler recovery of existing plans."""
    parser = argparse.ArgumentParser(prog='coordkit lane')
    sub = parser.add_subparsers(dest='command', required=True)
    watcher = sub.add_parser('_watch')
    watcher.add_argument('plan')
    args = parser.parse_args(argv)
    return watch(manifest, read_json(Path(args.plan)))
