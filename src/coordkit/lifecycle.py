"""Plan-first lifecycle and explicit lead identity; no implicit service installation."""
import argparse
import datetime as dt
import json
import hashlib
import os
from .paths import env_args, python_env
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import sys
import time

from . import hooks, loop
from .deadletters import ours as owned_dead_letters
from .actl import AgentctlClient
from .anxcli import AnxClient
from .manifest import ManifestError, duration, load
from .process import ToolError, run
from .state import read_json, write_json
from .services import manager


def shadow_allowed(command, argv):
    """CLI policy: deny a writer before its module can create state or contact tools."""
    tokens=[arg.partition('=')[0] for arg in argv]
    sub=tokens[0] if tokens else ''
    if command == 'bus':
        sub=next((arg for arg in tokens if arg in ('post','ack','wait','inbox','harvest','open-rulings','open-blockers','state','tail','stats')), '')
        return (sub not in ('post','ack') and not any(a in tokens for a in ('--unread','--mark','--new-only'))
                and not (sub == 'wait' and '--to' in tokens))
    if command in ('accounts', 'stock'):
        return True
    if command == 'sweep':
        # Shadow sweeps write local dedupe state and relay output, never the shadowed bus.
        return True
    if command == 'question':
        # Hooks no-op in shadow; list/bind/heuristic are local; the sweep reaches only the relay file.
        return sub in ('hook', 'list', 'bind', 'heuristic', 'sweep')
    if command == 'review':
        return sub in ('status', 'scan')  # start/record write the ledger and bus
    return ((command == 'doctor' and not {'--record-dead-letter-baseline',
            '--disposition-dead-letter', '--reopen-dead-letter'} & set(tokens))
            or (command == 'wait' and sub in ('pending','ask-guard','policy','wake-worthy')) or command in ('shadow', 'obligations'))


def ensure_resource(client, get_args, create_args):
    try:
        return client.call(*get_args)
    except ToolError as exc:
        if exc.category != 'not_found':
            raise
    return client.call(*create_args,write=True)


def initialize(project, file, workspace, board, git_repo, profile="anx"):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}',project):
        raise ManifestError('invalid_project_slug')
    file=Path(file).resolve()
    existed=file.exists()
    if not existed:
        # Construct a validated manifest in memory before touching ANX or local disk.
        text=f'''[project]
name = {json.dumps(project)}
git_repo = {json.dumps(git_repo)}
[anx]
workspace = {json.dumps(workspace)}
board = {json.dumps(board or 'ck-'+project)}
[agentctl.pools]
implement = "pool:implement"
review = "pool:review"
plan = "pool:plan"
[lane]
initiative = {json.dumps(project)}
[loop.loops.audit]
[escalation]
card = "card:loop-audit"
[launchd]
path = {json.dumps([str(Path.home()/'.local/bin'), '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'])}
python = {json.dumps(sys.executable)}
'''
        if profile == 'minimal':
            text = f'[project]\nname = {json.dumps(project)}\ngit_repo = {json.dumps(git_repo)}\n' + \
                   '[paths]\nroot = ".coordkit"\n[launchd]\n' + \
                   f'path = {json.dumps([str(Path.home()/".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"])}\n' + \
                   f'python = {json.dumps(sys.executable)}\n'
        # Parse using the same loader without leaving an unusable scaffold on a network failure.
        import tempfile
        file.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',prefix='.coordkit-init-',suffix='.toml',dir=file.parent,delete=False) as stream:
            candidate=Path(stream.name); stream.write(text)
        try:
            manifest=load(candidate)
            manifest.file=file
        finally:
            candidate.unlink()
    else:
        manifest=load(file)
        if manifest.project.name != project:
            raise ManifestError('init_project_mismatch')
    manifest.writable()
    if profile == 'anx' and manifest.anx.workspace:
        client=AnxClient(manifest)
        client.call('orient')  # reachability + --as first-use identity registration
        result=ensure_resource(client,('boards','get',manifest.anx.board),
                               ('boards','create','--title',manifest.anx.board,'--summary',f'Coordkit {project} work'))
        board_doc=result.get('board') or {}
        if board_doc.get('handle') and board_doc['handle'] != manifest.anx.board:
            raise ManifestError('created_board_handle_mismatch: '+board_doc['handle'])
        for agent in manifest.agents:
            AnxClient(manifest,agent.actor).call('orient')
        for name in manifest.loop.loops:
            settings, handle=loop.config(manifest,name)
            ref='card:'+handle
            ensure_resource(client,('cards','get',ref),('cards','create','--board',manifest.anx.board,
                '--handle',handle,'--title',f'Loop {name}','--body',settings.stop_rule,'--column','in_progress'))
            client.call('work','cadence',ref,'--every',settings.default_cadence,
                        '--budget',settings.progress_budget,'--stop-rule',settings.stop_rule,write=True)
    if not existed:
        # Exclusive creation prevents a concurrent init from clobbering user configuration.
        with file.open('x') as stream:
            stream.write(text)
    manifest.paths.root.mkdir(parents=True,exist_ok=True)
    # An empty readable bus is needed for fail-closed merge and for the first loop snapshot.
    manifest.paths.bus_log.parent.mkdir(parents=True,exist_ok=True)
    manifest.paths.bus_log.touch(exist_ok=True)
    from .selfupdate import register
    register(manifest)
    print(f'INIT_OK {manifest.file}')
    return 0


def templates(manifest):
    manifest.validate_launchd()
    jobs=[loop.plist(manifest,name) for name in manifest.loop.loops]
    local_work = manifest.addressed.agents or manifest.addressed.roles or manifest.questions.config_dirs
    interval = min(manifest.reconciler.interval_seconds, max(30, manifest.addressed.sweep_interval)) if local_work else manifest.reconciler.interval_seconds
    jobs.append({'Label':manifest.launchd.label_prefix+'.reconcile',
        'ProgramArguments':[manifest.launchd.python,'-m','coordkit.cli','--manifest',str(manifest.file),'sweep'],
        'WorkingDirectory':str(manifest.file.parent),
        'EnvironmentVariables':{'PATH':':'.join(manifest.launchd.path)},
        'StartInterval':interval,
        'StandardOutPath':str(manifest.paths.root/'reconcile/stdout.log'),
        'StandardErrorPath':str(manifest.paths.root/'reconcile/stderr.log')})
    mcp_job = read_json(manifest.paths.root/'mcp-service.json')
    if mcp_job:
        from .mcp_install import spec
        argv = mcp_job['ProgramArguments']
        jobs.append(spec(manifest, Path(argv[argv.index('--secret-file')+1]), int(argv[argv.index('--port')+1])))
    if (manifest.paths.root/'relay/service.json').exists() and manifest.relay is not None:
        from .relay import spec as relay_spec
        jobs.append(relay_spec(manifest))
    for job in jobs:
        # A src checkout is runnable before installation; installed wheels need no override.
        job['EnvironmentVariables'].update(python_env())
    return jobs


def launchctl(*args):
    return [os.environ.get('LAUNCHCTL', 'launchctl'), *args]


def checked(argv):
    result=run(argv,timeout=120)
    if result.returncode:
        raise ToolError(argv[0],6,result.stderr+result.stdout)
    return result.stdout


def dead_letter_snapshot(manifest):
    result = AgentctlClient(manifest).call('anx','status')
    letters = result.get('dead_letters', [])
    count = result.get('dead_letter_count', len(letters))
    if not isinstance(count, int) or count < len(letters) or count < 0:
        raise ManifestError('invalid_dead_letter_snapshot')
    return {'count': count, 'letters': letters,
            'fingerprints': [hashlib.sha256(json.dumps({k:v for k,v in row.items() if k != 'attempts'},sort_keys=True).encode()).hexdigest() for row in letters]}


def record_dead_letter_baseline(manifest):
    manifest.writable()
    path = manifest.paths.root/'dead-letter-baseline.json'
    if path.exists():
        raise ManifestError('dead_letter_baseline_exists: preserve original activation baseline')
    snapshot = dead_letter_snapshot(manifest)
    snapshot['recorded_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(path,snapshot)
    print(f'DEAD_LETTER_BASELINE count={snapshot["count"]} path={path}')


def installed_jobs(manifest):
    return manager(manifest).installed_labels(manifest.launchd.label_prefix)

def _legacy_installed_jobs(manifest):
    """Installed namespace is authoritative even when lifecycle records were lost."""
    raw = checked(launchctl('list'))
    prefix = manifest.launchd.label_prefix+'.'
    return sorted({line.split()[-1] for line in raw.splitlines()
                   if line.split() and line.split()[-1].startswith(prefix)})


def installation_drift(manifest, include_pending_questions=True):
    state = read_json(manifest.paths.root/'lifecycle.json', {})
    recorded = {job['label']:job for job in state.get('jobs', [])}
    installed = set(installed_jobs(manifest))
    expected = {job['Label']:job for job in templates(manifest)} if installed or recorded else {}
    drift = []
    for label in sorted(installed | set(recorded) | set(expected)):
        if label not in installed:
            drift.append(f'missing job {label}')
            continue
        if label not in expected:
            drift.append(f'unexpected job {label}')
            continue
        if manager(manifest).name == 'systemd':
            drift.extend(manager(manifest).drift(expected[label]))
            continue
        raw = manager(manifest).detail(label)
        match = re.search(r'^\s*path = (.+)$', raw, re.M)
        # Read the plist actually loaded, using records only if print omits the path.
        path = Path(match[1].strip()) if match else Path(recorded.get(label, {}).get('file',
                    manifest.paths.root/'launchd'/(label+'.plist')))
        try:
            actual = plistlib.loads(path.read_bytes())
            if actual != expected[label]:
                drift.append(f'plist drift {label}')
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            drift.append(f'cannot read installed plist {label}: {exc}')
        job = expected[label]
        if job['EnvironmentVariables']['PATH'] not in raw:
            drift.append(f'loaded PATH drift {label}')
        # launchctl renders each argument on its own line; verify the complete argv
        # rather than a substring of one flag. Detect edits after bootstrap too.
        arguments = re.search(r'arguments = \{(.*?)\n\s*\}', raw, re.S)
        if arguments:
            argv = [line.strip().strip('"') for line in arguments[1].splitlines() if line.strip()]
            if argv != job['ProgramArguments']:
                drift.append(f'loaded arguments drift {label}')
        else:
            drift.append(f'loaded arguments unavailable {label}')
        interval = re.search(r'run interval = (\d+)', raw)
        if job.get('StartInterval') and (not interval or int(interval[1]) != job['StartInterval']):
            drift.append(f'loaded interval drift {label}')
    drift.extend(hooks.drift(manifest))
    drift.extend(hooks.question_drift(manifest, include_pending_questions))
    client = AnxClient(manifest)
    for name in manifest.loop.loops:
        settings, handle = loop.config(manifest,name)
        ref = 'card:'+handle
        try:
            card = client.resolve_card(ref)
        except ToolError as exc:
            if exc.category != 'card_unresolved':
                raise
            drift.append(f'loop card missing {ref}: {exc}')
            continue
        if card.get('board_ref') != 'board:'+manifest.anx.board:
            drift.append(f'loop card board drift {ref}')
        expected_cadence = {'every_seconds':duration(settings.default_cadence),
                            'progress_budget_seconds':duration(settings.progress_budget),
                            'stop_rule':settings.stop_rule}
        cadence = card.get('cadence') or client.cadence(ref)
        if any(cadence.get(key) != value for key,value in expected_cadence.items()):
            drift.append(f'loop card cadence drift {ref}')
        stopped = (manifest.paths.root/'loops'/name/'STOP').exists()
        resolved = bool(card.get('resolution')) or card.get('column_key') in ('done','cancelled')
        if stopped != resolved:
            drift.append(f'loop card stop state drift {ref}')
    return drift


def doctor(manifest, strict_dead_letters=False, for_apply=False):
    findings=[]
    def check(name, fn):
        try:
            detail=fn()
            level = 'warn' if isinstance(detail, str) and detail.startswith('WARN ') else 'green'
            print(f'DOCTOR {name} {level} {detail if detail is not None else ""}')
        except (ToolError,ManifestError,OSError,ValueError,KeyError) as exc:
            findings.append(name)
            print(f'DOCTOR {name} red {exc}')
    def binaries():
        path=':'.join(manifest.launchd.path) if manifest.launchd.path else os.environ.get('PATH','')
        requirements = ([('ANX', 'anx')] if manifest.anx.workspace else []) + ([('AGENTCTL', 'agentctl')] if manifest.agentctl.pools else [])
        for env,default in requirements:
            binary=shutil.which(os.environ.get(env,default),path=path)
            if not binary or not Path(binary).is_absolute():
                raise ManifestError('binary_unavailable: '+default)
        if not Path(manifest.launchd.python).is_file() or not os.access(manifest.launchd.python,os.X_OK):
            raise ManifestError('python_unavailable')
        return 'absolute executable paths resolve'
    client=AnxClient(manifest)
    def identity():
        if not manifest.anx.workspace:
            return 'absent (optional)'
        orient=client.call('orient')
        if not orient.get('principal',{}).get('id'):
            raise ManifestError('anx_lead_unresolved')
        if manifest.mode == 'shadow':
            client.call('cards','list')
        else:
            board=client.call('boards','get',manifest.anx.board)
            if not board.get('board',{}).get('id'):
                raise ManifestError('anx_board_unresolved')
        return f'workspace={manifest.anx.workspace} actor={client.actor}'
    def pools():
        if not manifest.agentctl.pools:
            return 'absent (optional)'
        if manifest.mode == 'shadow':
            return 'manifest pools configured; shadow skips config probes'
        result=AgentctlClient(manifest).call('config','doctor')
        if result.get('valid') is not True or result.get('launch_recipes',{}).get('compatible') is False:
            raise ManifestError('agentctl_config_invalid')
        return ','.join(manifest.agentctl.pools)
    def github():
        if not manifest.anx.workspace and not manifest.agentctl.pools:
            return 'absent (optional)'
        raw=checked([os.environ.get('GH','gh'),'pr','list','-R',manifest.project.git_repo,'--limit','1','--json','number'])
        if not isinstance(json.loads(raw),list):
            raise ManifestError('github_invalid_response')
        return manifest.project.git_repo
    def jobs():
        drift = installation_drift(manifest, include_pending_questions=not for_apply)
        if drift:
            raise ManifestError('; '.join(drift))
        return 'installed namespace and templates agree'
    def reconciler_age():
        if manifest.mode == 'shadow':
            # Shadow schedules no reconciler; a stamp left by a rolled-back live pass only ages (apply archives it).
            return 'shadow: reconciler not scheduled'
        state = read_json(manifest.paths.root/'reconcile/state.json', {})
        stamp = state.get('last_completed', state.get('started_at', state.get('last_run')))
        interval = manifest.reconciler.interval_seconds
        if stamp is None:
            return 'not started'
        age = max(0, time.time()-stamp)
        detail = f'age={age:.0f}s interval={interval}s deadline={2*interval}s'
        if age > 2*interval:
            raise ManifestError('reconciler overdue beyond two intervals; '+detail)
        return 'last reconciliation within two intervals; '+detail
    def unresolved_obligations():
        if manifest.mode == 'shadow':
            return 'shadow: reconciler not scheduled'
        path = manifest.paths.root/'reconcile/state.json'
        unresolved = read_json(path, {}).get('unresolved_obligations', {})
        if unresolved:
            cards = {row.get('card') for row in unresolved.values() if row.get('card')}
            sample = ','.join(sorted(unresolved)[:5])
            return (f'WARN unresolved obligations={len(unresolved)} cards={len(cards)} '
                    f'sample={sample}; details={path}')
        return 'no unresolved obligation cards'
    def deadletters():
        if not manifest.agentctl.pools:
            return 'absent (optional)'
        if manifest.mode == 'shadow' and not strict_dead_letters:
            return 'shadow skips spool probe'
        snapshot = dead_letter_snapshot(manifest)
        baseline = read_json(manifest.paths.root/'dead-letter-baseline.json', {'count':0,'fingerprints':[]})
        # The spool is host-wide and capped, so a stable total proves nothing; attribute entries.
        fresh = [row for row,fp in zip(snapshot['letters'],snapshot['fingerprints'])
                 if fp not in set(baseline['fingerprints'])]
        ours = owned_dead_letters(manifest, snapshot['letters'], include_baseline=strict_dead_letters)
        # Entries the status call counts but does not list cannot be attributed either.
        hidden = snapshot['count'] - len(snapshot['letters'])
        if ours or hidden > baseline['count'] - len(baseline.get('letters', [])):
            raise ManifestError(f'dead_letters: total={snapshot["count"]} baseline={baseline["count"]} new={len(fresh)} '
                                f'ours={len(ours)} {" ".join(r.get("execution_id","?") for r in ours[:5])}'.rstrip())
        from .deadletters import dispositions
        return (f'total={snapshot["count"]} baseline={baseline["count"]} new={len(fresh)} ours=0 '
                f'foreign={len(fresh)} dispositioned={len(dispositions(manifest))} strict={strict_dead_letters}')
    def guard():
        writes=[('bus',['post']),('bus',['ack']),('bus',['wait','--to','lead']),('lane',['_watch']),
                ('loop',['run']),('reconcile',[]),('up',['--apply']),('init',[]),('lead',['adopt']),
                ('review',['record']),('question',['answer']),('doctor',['--disposition-dead-letter','x'])]
        if any(shadow_allowed(c,a) for c,a in writes):
            raise ManifestError('shadow_guard_incomplete')
        return f'{len(writes)} writer routes disabled in shadow'
    from .escalation import validate
    def alarm_path():
        if not manifest.anx.workspace:
            return 'absent (optional)'
        validate(manifest)
        client.resolve_card(manifest.escalation.card)
        return manifest.escalation.card
    check('escalation_limits',alarm_path)
    check('manifest',lambda:manifest.validate_launchd() if manifest.launchd.path else 'services unconfigured (optional)')
    check('binaries',binaries); check('anx',identity); check('agentctl',pools)
    check('reconciler_age',reconciler_age)
    check('unresolved_obligations',unresolved_obligations)
    from .answers import health as answer_health
    check('unread_answers', lambda: answer_health(manifest))
    from .loop_answers import health as loop_answer_health
    check('loop_answers', lambda: loop_answer_health(manifest))
    check('github',github); check('launchd',jobs); check('dead_letters',deadletters); check('shadow_guard',guard)
    from .relay import health as relay_health
    check('relay', lambda: relay_health(manifest))
    from .selfupdate import health
    check('selfupdate', health)
    from .adapters import available
    for adapter in available(manifest):
        if callable(getattr(adapter, 'status', None)):
            check(adapter.name, adapter.status)
    from .usage import doctor_warnings
    for warning in doctor_warnings(manifest):
        print('DOCTOR model_drift warn ' + warning)
    return 1 if findings else 0


def up(manifest,apply=False):
    manifest.writable()
    # Pending question-hook installs are exactly what this apply does; they never block it.
    health=doctor(manifest, for_apply=True)
    jobs=templates(manifest)
    for drift in installation_drift(manifest):
        print('UP_DIFF '+drift)
    for config_dir in hooks.pending_question_installs(manifest):
        print('UP_DIFF question hooks to install in '+str(Path(config_dir).expanduser()/'settings.json'))
    client=AnxClient(manifest)
    anx=[os.environ.get('ANX','anx'),'--workspace',manifest.anx.workspace,'--as',manifest.anx.agent_prefix]
    commands=[]
    if manifest.anx.bridge_answers:
        # The bridge is one service per host, shared by every workspace: adopt a running one,
        # never reinstall over it (install refuses while legacy fleet units remain).
        if not client.call('bridge','status').get('running'):
            commands.append([*anx,'bridge','install'])
        commands.append([*anx,'bridge','add'])
    else:
        print('UP_SKIP anx bridge: reconciler collects answers (set [anx] bridge_answers = true to register)')
    if manifest.rc_host.enabled:
        commands.append(['claude-host-setup','--prefix',manifest.rc_host.prefix,
                         manifest.rc_host.env+'='+str(manifest.file.parent)])
    for job in jobs:
        rendered = manager(manifest).render(job)
        print(rendered.decode() if isinstance(rendered, bytes) else '\n'.join(rendered.values()), end='')
    for command in commands:
        print('UP_ACTION '+shlex.join(command))
    if not apply:
        print('UP_PLAN_ONLY')
        return health
    if health:
        raise ManifestError('doctor_not_green')
    # Question hooks (the question escalation contract): backup, receipt, then the settings write, per
    # config dir; `down` reverses each. Installed before the jobs so a failure stops the apply.
    for config_dir in hooks.pending_question_installs(manifest):
        settings=Path(config_dir).expanduser()/'settings.json'
        backup=manifest.paths.root/'hook-backups'/f'{hooks.dir_tag(config_dir)}-{int(time.time())}.json'
        backup.parent.mkdir(parents=True,exist_ok=True)
        backup.write_bytes(settings.read_bytes())
        hooks.install_questions(manifest,config_dir,backup)
        print('UP_QUESTION_HOOKS '+str(settings))
    state_path=manifest.paths.root/'lifecycle.json'
    state=read_json(state_path,{'jobs':[], 'hook_backups':[]})
    for job in jobs:
        service = manager(manifest)
        Path(job['StandardOutPath']).parent.mkdir(parents=True,exist_ok=True)
        path = service.path(job['Label'])
        record={'label':job['Label'],'file':str(path)}
        previous=next((entry for entry in state['jobs'] if entry['label']==job['Label']),None)
        if previous and path.exists():
            same = (path.read_bytes() == service.render(job)) if service.name == 'launchd' else not service.drift(job)
            if same:
                continue
        # Persist ownership before bootstrap so down can recover a lost install.
        state['jobs'] = [entry for entry in state['jobs'] if entry['label'] != job['Label']]
        state['jobs'].append(record); write_json(state_path,state)
        service.install(job, previous=bool(previous))
    for command in commands:
        checked(command)
    from .selfupdate import register
    register(manifest)
    print('UP_OK')
    return 0


def down(manifest):
    manifest.writable()
    state_path=manifest.paths.root/'lifecycle.json'
    state=read_json(state_path,{'jobs':[], 'hook_backups':[]})
    records = {job['label']:job for job in state['jobs']}
    labels = set(installed_jobs(manifest)) | set(records)
    for label in sorted(labels):
        manager(manifest).uninstall(label)
        state['jobs'] = [job for job in state['jobs'] if job['label'] != label]
        write_json(state_path,state)
    if state.get('hook_backups') and not hooks.receipt_path(manifest).exists():
        raise ManifestError('hook_rollback_snapshot_missing: inspect original and edited settings before restoring')
    hooks.restore(manifest)
    hooks.restore_questions(manifest)
    (manifest.paths.root/'mcp-service.json').unlink(missing_ok=True)
    (manifest.paths.root/'relay/service.json').unlink(missing_ok=True)
    state['hook_backups'] = []
    write_json(state_path,state)
    print('DOWN_OK')
    return 0


def adopt(manifest,actor=None):
    manifest.writable()
    actor=actor or manifest.anx.lead_actor
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}',actor):
        raise ManifestError('invalid_actor')
    AnxClient(manifest,actor).call('orient')
    write_json(manifest.paths.root/'lead.json',{'actor':actor,'ts':dt.datetime.now(dt.timezone.utc).isoformat()})
    manifest.anx.lead_actor=actor
    print(f'LEAD_ADOPTED {actor}')
    return 0


def main(argv,manifest,command):
    from .ops import parser_for
    parser = argparse.ArgumentParser(prog='coordkit '+command) if command == 'lead' else parser_for(command)
    if command == 'lead':
        parser.add_argument('verb',choices=['adopt']); parser.add_argument('--actor')
    args=parser.parse_args(argv)
    if command == 'up': return up(manifest,args.apply)
    if command == 'down': return down(manifest)
    if command == 'doctor':
        if args.record_dead_letter_baseline:
            record_dead_letter_baseline(manifest)
        if args.disposition_dead_letter or args.reopen_dead_letter:
            from . import deadletters
            if args.reopen_dead_letter:
                print(f'DEAD_LETTER_REOPENED {args.reopen_dead_letter} '
                      f'entries={deadletters.reopen(manifest,args.reopen_dead_letter)}')
            else:
                snapshot = dead_letter_snapshot(manifest)
                n = deadletters.disposition(manifest, args.disposition_dead_letter, args.reason,
                                            args.by or manifest.anx.lead_actor, snapshot['letters'])
                print(f'DEAD_LETTER_DISPOSITIONED {args.disposition_dead_letter} entries={n} reason={args.reason!r}')
        return doctor(manifest,args.strict_dead_letters)
    return adopt(manifest,args.actor)
