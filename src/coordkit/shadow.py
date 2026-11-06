"""Bounded read-only comparison; only the manifest-local shadow journal is writable."""
import argparse
import ast
import collections
import contextlib
import datetime as dt
from dataclasses import asdict
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
import time
import uuid

from . import bus, incumbent, lane, merge
from .actl import AgentctlClient
from .anxcli import AnxClient, past_promises
from .extract import clean
from .manifest import ManifestError, duration
from .process import ToolError, parse_ts
from .state import read_json, write_json

WINDOW = 24 * 3600
TERMINAL = {'completed','failed','cancelled','orphaned','launch_failed'}


def protocol_hash(path, names):
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name in names) or
             (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in n.targets))]
    # Docstrings and comments are publication metadata, not parser semantics.
    for node in nodes:
        if isinstance(node, ast.FunctionDef) and ast.get_docstring(node) is not None:
            node.body = node.body[1:]
    return hashlib.sha256('\n\n'.join(ast.unparse(n) for n in nodes).encode()).hexdigest()


def read_rows(path):
    # Ignore only an unfinished trailing append. Malformed complete records fail closed.
    rows = []
    with path.open('rb') as stream:
        for raw in stream:
            if not raw.endswith(b'\n'):
                break
            row = json.loads(raw)
            if not isinstance(row,dict):
                raise ValueError(f'invalid_record: {path}')
            rows.append(row)
    return rows


def directory(manifest):
    if manifest.mode != 'shadow':
        raise ManifestError('shadow_requires_shadow_manifest')
    root = manifest.paths.root.resolve()
    if not root.is_relative_to(manifest.file.parent):
        raise ManifestError('shadow_output_must_be_manifest_local')
    target = (root/'shadow').resolve()
    if not target.is_relative_to(root):
        raise ManifestError('shadow_output_symlink_escape')
    sources = [manifest.paths.bus_log.resolve(),manifest.paths.events.resolve()]
    for name in ('state.json','diffs.jsonl','run.lock'):
        path = (target/name).resolve()
        if not path.is_relative_to(target) or path in sources:
            raise ManifestError('shadow_output_overlaps_input')
    if any(source.is_relative_to(target) for source in sources):
        raise ManifestError('shadow_output_overlaps_input')
    return target


def normalize_events(rows):
    linkage = {r['execution_id']:r for r in rows if r.get('execution_id') and r.get('label')}
    out = []
    for row in rows:
        if isinstance(row.get('event'),dict):
            event = row['event']
            old = linkage.get(event.get('execution_id'),{})
            row = dict(old, **{k:v for k,v in event.items() if k in ('execution_id','kind','state')},
                       ts=event.get('occurred_at') or event.get('observed_at'))
        out.append(row)
    return out


def collect(manifest):
    for kind in ('BUS','MERGE'):
        value = getattr(manifest.shadow,'incumbent_'+kind.lower()+'_source')
        if not value:
            raise ManifestError('missing_key: shadow.incumbent_'+kind.lower()+'_source')
        if protocol_hash(manifest.path(value),getattr(incumbent,kind+'_NAMES')) != getattr(incumbent,kind+'_SHA'):
            raise ValueError('INCUMBENT_PROTOCOL_DRIFT: '+kind)
    rows, events = read_rows(manifest.paths.bus_log), normalize_events(read_rows(manifest.paths.events))
    if manifest.shadow.lane_records:
        records = manifest.path(manifest.shadow.lane_records)
        if not records.is_dir():
            raise ValueError('incumbent_lane_records_missing')
        plans = {}
        for path in sorted(records.glob('*.plan.json')):
            plan = read_json(path)
            if not isinstance(plan,dict) or not plan.get('label'):
                raise ValueError('invalid_incumbent_lane_record')
            old = plans.get(plan['label'],{})
            if plan.get('launched_at','') >= old.get('launched_at',''):
                plans[plan['label']] = plan
        for row in events:
            plan = plans.get(row.get('label'),{})
            if not row.get('card') and plan.get('card') and row.get('ts','') >= plan.get('launched_at',''):
                row['card'] = plan['card']
    client, actl = AnxClient(manifest), AgentctlClient(manifest)
    cards_doc = client.call('cards','list')  # Workspace-wide: the target board may not exist during shadow observation.
    if (not isinstance(cards_doc.get('cards'),list) or cards_doc.get('has_more')
            or cards_doc.get('truncated') or cards_doc.get('next_cursor')):
        raise ValueError('incomplete_card_snapshot')
    cards = cards_doc['cards']
    executions = actl.recent(nonterminal=True)
    latest = {}
    for row in events:
        if row.get('label') and row.get('execution_id'):
            latest[row['label']] = row
    statuses = {}
    # Only current lane records need status probes, not every historical execution.
    for row in latest.values():
        if row.get('kind') not in ('terminal',) and row.get('state') not in TERMINAL:
            statuses[row['execution_id']] = actl.status(row['execution_id'])
    linked = {r['execution_id']:r['card'] for r in events if r.get('execution_id') and r.get('card')}
    refs = {linked[r['execution_id']] for r in latest.values() if r['execution_id'] in linked}
    refs.update('card:'+c['handle'] for c in cards if c.get('handle','').startswith(manifest.loop.receipt_prefix))
    # agentctl is host-wide: other projects' executions carry card labels from other ANX
    # workspaces, which this workspace cannot resolve. Probe only cards this workspace lists.
    workspace_refs = {'card:'+c['handle'] for c in cards if c.get('handle')}
    for execution in executions:
        refs.update(ref for ref in ('card:'+tag.removeprefix('anx.card.') for tag in execution.get('labels',[])
                    if tag.startswith('anx.card.') and not any(r.get('execution_id') == execution.get('id') and r.get('card') for r in latest.values()))
                    if ref in workspace_refs)
    leases = {}
    for ref in sorted(refs):
        try:
            leases[ref] = client.call('lease','show',ref).get('lease') or {}
        except ToolError as exc:
            # Incumbent lane records can name cards that do not exist in this workspace. That is
            # evidence about the incumbent, recorded as such; any other failure stays unavailable.
            if exc.category not in ('not_found','card_unresolved'):
                raise
            leases[ref] = {'card_missing': True}
    orient = client.call('orient')
    # GitHub is an independent read-only source for the PR/hold coverage set.
    # A public repo can have more open community PRs than the cap; holds and lanes concern only
    # our own PRs, and bus-mentioned PRs are added from the bus itself.
    prs = json.loads(merge.gh(manifest,'pr','list','--state','open','--author','@me','--limit','1000','--json','number'))
    if not isinstance(prs,list) or len(prs) >= 1000:
        raise ValueError('incomplete_pr_snapshot')
    return {'bus':rows,'events':events,'executions':executions,'statuses':statuses,
            'cards':cards,'leases':leases,'orient':orient,'prs':prs}


PHASE_RANK = {'unknown_execution':3, 'in_progress':3, 'review':2, 'ready':1}


def ref_of(card):
    return 'card:'+card['handle'] if card.get('handle') else card['ref']


def row_id(row):
    # Legacy bus rows predate ids; identify them by content so holds still compare.
    return row.get('id') or 'noid-' + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:12]


def views(manifest, snapshot, at):
    rows = snapshot['bus']
    ours = {'rulings':sorted(row_id(r) for r in bus.open_rulings(rows)),
            'blockers':sorted(row_id(r) for r in bus.open_blockers(rows)), 'holds':{},'lanes':{},'loops':{}}
    theirs = {'rulings':sorted(row_id(r) for r in incumbent.open_rulings(rows)),
              'blockers':sorted(row_id(r) for r in incumbent.open_blockers(rows)), 'holds':{},'lanes':{},'loops':{}}
    numbers = {str(p['number']) for p in snapshot['prs']}
    # Both holds_on implementations skip every row that neither lists nor mentions the PR, so each
    # PR only needs the rows naming it (in bus order). Scanning all rows per PR took tens of minutes.
    naming = collections.defaultdict(list)
    for row in rows:
        named = set(map(str,row.get('prs') or []))
        named.update(m.group(1) for m in merge.PR_MENTION.finditer(row.get('text') or ''))
        numbers.update(named)
        for number in named:
            naming[number].append(row)
    with contextlib.redirect_stdout(io.StringIO()):
        for number in sorted(numbers):
            subset = naming.get(number, [])
            ours['holds'][number] = sorted(row_id(r) for r in merge.holds_on(number,subset).values())
            theirs['holds'][number] = sorted(row_id(r) for r in incumbent.holds_on(number,subset).values())
    cards = {ref_of(c):c for c in snapshot['cards']}
    latest = {}
    for row in snapshot['events']:
        if row.get('label') and row.get('execution_id'):
            latest[row['label']] = row
    live = {e['id']:e for e in snapshot['executions'] if e.get('state') not in TERMINAL}
    # Join authoritative card linkage from incumbent launch records before labels:
    # old launchers sometimes label an initiative rather than the actual card.
    linked = {r['execution_id']:r for r in snapshot['events'] if r.get('card') and r.get('execution_id')}
    for execution in live.values():
        if execution['id'] not in linked:
            refs = [t[9:] for t in execution.get('labels',[]) if t.startswith('anx.card.')]
            labels = [t for t in execution.get('labels',[]) if not t.startswith(('anx.card.','pr-','card-'))]
            if refs and labels:
                latest[labels[0]] = {'label':labels[0],'card':'card:'+refs[0],
                                     'execution_id':execution['id'],'kind':'launched'}
    for label,row in latest.items():
        ref = row.get('card') or linked.get(row['execution_id'],{}).get('card')
        if not ref:
            continue
        status = snapshot['statuses'].get(row['execution_id'],{})
        state = (live.get(row['execution_id']) or status or row).get('state')
        terminal = state in TERMINAL or row.get('kind') == 'terminal'
        probe = {**(live.get(row['execution_id']) or {}), **status}
        started = probe.get('started_at') or (row.get('ts') if row.get('kind') == 'launched' else None)
        if not terminal and state is not None and ((started and
                at-parse_ts(started).timestamp() >= manifest.lane.max_run_hours*3600) or lane.hung(manifest, probe, at)):
            # coordkit expires a lane past max_run_hours or once its run is unreachable for
            # hung_after (lane.expire): failed, lease to the lead.
            terminal, state = True, 'failed'
        # A finished lane goes to review; a failed one goes back to ready (lane.finish).
        expected_phase = ('review' if state == 'completed' else 'ready') if terminal else 'in_progress'
        owner = (row.get('handoff') or row.get('handoff_to') or 'actor:'+manifest.anx.lead_actor) if terminal else label
        if not terminal and state is None:
            expected_phase = 'unknown_execution'
        # Several lanes can share one (umbrella) card: a live lane outranks a finished one,
        # which outranks a failed one, whatever order the labels come in.
        previous = ours['lanes'].get(ref)
        if not previous or PHASE_RANK[expected_phase] >= PHASE_RANK[previous['phase']]:
            ours['lanes'][ref] = {'phase':expected_phase,'owner':owner.removeprefix('actor:')}
        card, lease = cards.get(ref,{}), snapshot['leases'].get(ref,{})
        if card.get('resolution') == 'done' or card.get('column_key') in ('done','cancelled'):
            ours['lanes'].pop(ref,None)
            continue
        theirs['lanes'][ref] = {'phase':card.get('column_key',card.get('placement')),
                                'owner':lease.get('owner','').removeprefix('actor:')}
    overdue = {ref_of(c) for c in past_promises(snapshot['orient'])}
    loops = {}
    for entry in (snapshot['orient'] or {}).get('loops') or []:
        ref = entry.get('ref') or entry.get('card_ref') or ('card:'+entry['handle'] if entry.get('handle') else None)
        if ref:
            loops[ref] = entry
    for ref,card in cards.items():
        if not ref.removeprefix('card:').startswith(manifest.loop.receipt_prefix):
            continue
        if card.get('resolution') or card.get('column_key') in ('done','cancelled'):
            continue  # a resolved loop card is stopped; neither side expects receipts from it
        # `cards list` omits cadence; ANX reports it per loop in orient.loops.
        entry = loops.get(ref) or {}
        cadence = card.get('cadence') or {k:v for k,v in (entry.get('loop') or {}).items() if k == 'every_seconds'}
        seconds = cadence.get('every_seconds')
        receipt = card.get('last_receipt_at') or card.get('last_evidence_at') or entry.get('last_evidence_at')
        # Never infer clean cadence from an absent receipt timestamp.
        promise = entry.get('promise') or (card.get('plan_health') or {}).get('promise') or {}
        # The cadence promise deadline (last receipt plus cadence) is ANX's own clock; a bare
        # interval since the receipt ignores the progress budget, so it is only the fallback.
        if promise.get('source') == 'cadence' and promise.get('at'):
            due = at > parse_ts(promise['at']).timestamp()
        else:
            due = None if not seconds or not receipt else at-parse_ts(receipt).timestamp() > seconds
        ours['loops'][ref] = {'past_due':due}
        theirs['loops'][ref] = {'past_due':ref in overdue}
    return ours, theirs


def differences(snapshot, ours, theirs, at, manifest=None):
    diffs = []
    for key, code in [('rulings','OPEN_RULINGS'),('blockers','OPEN_BLOCKERS'),('holds','HELD_PRS'),
                      ('lanes','LANE_PHASE_OWNER'),('loops','LOOP_CADENCE')]:
        if key in ('rulings','blockers'):
            items = [('*',ours[key],theirs[key])] if ours[key] != theirs[key] else []
        else:
            items = [(ref,ours[key].get(ref),theirs[key].get(ref)) for ref in sorted(set(ours[key])|set(theirs[key]))
                     if ours[key].get(ref) != theirs[key].get(ref)]
        for ref,left,right in items:
            whitelist = None
            if key == 'holds':
                delta = set(left or []) ^ set(right or [])
                changed = [r for r in snapshot['bus'] if r.get('id') in delta]
                if changed and len(changed) == len(delta) and all(r.get('frm') == 'lead' and
                        r.get('kind') not in ('hold','unhold') and merge.free_hold_asserts(ref,r.get('text','')) for r in changed):
                    whitelist = 'LEAD_FREE_TEXT_HOLD'
            if key == 'lanes' and left and right and left['owner'] == right['owner']:
                events = [r for r in snapshot['events'] if r.get('card') == ref and r.get('ts') and
                          0 <= at-parse_ts(r['ts']).timestamp() <= 600]
                if (len({r.get('execution_id') for r in events if r.get('execution_id')}) > 1
                        and any(r.get('kind') == 'relaunched' for r in events)
                        and {left['phase'],right['phase']} <= {'in_progress','review'}):
                    whitelist = 'CAPACITY_RELAUNCH_CHURN'
            if key == 'lanes' and whitelist is None and left:
                # The incumbent never moves a card or releases its lease when a lane ends, so its
                # column and owner go stale (footgun 7). Accept the difference only when the
                # incumbent lease is expired (or its card is missing) and coordkit agrees with the
                # live executions: finished with nothing running, or running under coordkit's owner.
                lease = snapshot['leases'].get(ref) or {}
                # Runs coordkit would already have expired (hung or past max_run_hours) are not live.
                running = [e for e in snapshot['executions'] if e.get('state') not in TERMINAL
                           and not (manifest and (lane.hung(manifest, e, at) or (e.get('started_at') and
                                    at-parse_ts(e['started_at']).timestamp() >= manifest.lane.max_run_hours*3600)))
                           and 'anx.card.'+ref.removeprefix('card:') in (e.get('labels') or [])]
                stale = lease.get('state') == 'expired' or lease.get('card_missing') or right is None
                if stale and ((left['phase'] in ('ready','review','done') and not running) or
                              (left['phase'] == 'in_progress' and any(left['owner'] in (e.get('labels') or []) for e in running))):
                    whitelist = 'INCUMBENT_STALE_PROJECTION'
            diffs.append({'code':code,'ref':ref,'coordkit':left,'incumbent':right,'whitelist':whitelist})
    return diffs


def append_record(path, record):
    with path.open('a') as stream:
        stream.write(clean(json.dumps(record,sort_keys=True))+'\n')
        stream.flush()
        os.fsync(stream.fileno())


def comparison_scope(manifest):
    # Bind evidence to the full effective configuration (including loaded lead
    # identity), source locations and incumbent semantics. Stream contents evolve
    # during observation and are deliberately not part of the scope.
    inputs = {'manifest':asdict(manifest), 'protocols':{},
              'lane_records':str(manifest.path(manifest.shadow.lane_records)) if manifest.shadow.lane_records else None,
              'tools':{key:os.environ.get(key) for key in ('ANX','AGENTCTL','GH')}}
    for kind in ('BUS','MERGE'):
        source = getattr(manifest.shadow,'incumbent_'+kind.lower()+'_source')
        inputs['protocols'][kind] = protocol_hash(manifest.path(source),getattr(incumbent,kind+'_NAMES'))
    digest = hashlib.sha256(json.dumps(inputs,sort_keys=True,default=str).encode()).hexdigest()
    # Keep the journal's existing redaction-safe hash representation.
    return '.'.join(digest[i:i+16] for i in range(0,len(digest),16))


SAMPLE_SECONDS = 280  # launchd waits 300 s after each run ends; 300+280 stays inside max_gap 600


def single_pass(manifest, hours=24, at=None):
    target = directory(manifest)
    target.mkdir(parents=True,exist_ok=True)
    with (target/'run.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        at = time.time() if at is None else at
        journal = read_rows(target/'diffs.jsonl') if (target/'diffs.jsonl').exists() else []
        state = journal[-1] if journal else {}
        scope = None
        def overrun(signum, frame):
            raise TimeoutError(f'sample exceeded {SAMPLE_SECONDS}s')
        # A sample killed from outside writes nothing and its gap resets the streak; time out here
        # instead so the journal records an unavailable sample (run() reaps any tool in flight).
        previous = signal.signal(signal.SIGALRM, overrun)
        signal.alarm(SAMPLE_SECONDS)
        try:
            scope = comparison_scope(manifest)
            snapshot = collect(manifest)
            ours, theirs = views(manifest,snapshot,at)
            diffs = differences(snapshot,ours,theirs,at,manifest)
        except (OSError,ValueError,KeyError,TypeError,SyntaxError,ToolError) as exc:
            ours, theirs = {}, {}
            diffs = [{'code':'SOURCE_UNAVAILABLE','detail':str(exc),'whitelist':None}]
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        clean_pass = not any(d.get('whitelist') is None for d in diffs)
        gap = at-state.get('at',at)
        since = state.get('clean_since')
        # One sample that could not read a source is unobserved, not a divergence: it keeps the
        # streak if it ran on time and the next sample is clean. A second in a row resets.
        unavailable = bool(diffs) and all(d.get('code') == 'SOURCE_UNAVAILABLE' for d in diffs)
        bridged = (unavailable and since is not None and not state.get('unobserved')
                   and 0 <= gap <= manifest.shadow.max_gap_seconds)
        if bridged:
            pass
        elif not clean_pass:
            since = None
        elif since is None or state.get('scope') != scope or not 0 <= gap <= manifest.shadow.max_gap_seconds:
            since = at
        streak = max(0,at-since) if since is not None else 0
        record = {'id':uuid.uuid4().hex,'ts':dt.datetime.fromtimestamp(at,dt.timezone.utc).isoformat(),
                  'at':at,'scope':scope,'clean':clean_pass,'diffs':diffs,'clean_since':since,
                  'clean_seconds':streak,'required_seconds':WINDOW,'target_seconds':hours*3600,
                  'unobserved':bridged,'eligible':clean_pass and streak >= WINDOW}
        # Journal first; state is a replaceable projection. A crash before state save
        # loses streak credit conservatively and never fabricates a clean sample.
        append_record(target/'diffs.jsonl',record)
        state = dict(record,last_at=at,coordkit=ours,incumbent=theirs)
        write_json(target/'state.json',json.loads(clean(json.dumps(state))))
        print(clean(json.dumps(record,sort_keys=True)))
        return 0 if clean_pass else 5


def report(manifest, at=None):
    target = directory(manifest)
    journal = read_rows(target/'diffs.jsonl') if (target/'diffs.jsonl').exists() else []
    state = journal[-1] if journal else None
    if state is None:
        print('SHADOW_REPORT no samples; eligible=false')
        return 5
    at = time.time() if at is None else at
    fresh = 0 <= at-state['at'] <= manifest.shadow.max_gap_seconds
    try:
        scope_matches = state.get('scope') == comparison_scope(manifest)
    except (OSError,ValueError,TypeError,SyntaxError):
        scope_matches = False
    eligible = state['eligible'] and fresh and scope_matches
    print(json.dumps({'samples_path':str(target/'diffs.jsonl'),'clean_hours':state['clean_seconds']/3600,
                      'required_hours':24,'fresh':fresh,'scope_matches':scope_matches,'eligible':eligible,
                      'last_at':state['at'],'diffs':state['diffs']},sort_keys=True))
    return 0 if eligible else 5


def main(argv,manifest):
    parser = argparse.ArgumentParser(prog='coordkit shadow',allow_abbrev=False)
    subs = parser.add_subparsers(dest='verb',required=True)
    subs.add_parser('diff',allow_abbrev=False)
    p = subs.add_parser('run',allow_abbrev=False)
    p.add_argument('--hours',type=float,default=24)
    subs.add_parser('report',allow_abbrev=False)
    args = parser.parse_args(argv)
    hours = getattr(args,'hours',24)
    if not math.isfinite(hours) or hours <= 0:
        parser.error('--hours must be finite and positive')
    return report(manifest) if args.verb == 'report' else single_pass(manifest,hours)
