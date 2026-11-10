#!/usr/bin/env python3
"""Scriptable fake, keyed by executable and argv prefix; all calls are journaled."""
import json
import os
from pathlib import Path
import sys

name = os.environ["CK_FAKE_NAME"]
args = sys.argv[1:]
stdin = sys.stdin.read() if "--prompt-stdin" in args else ""
record = {"tool": name, "args": args, "stdin": stdin}
# Pinned ANX flag subset emitted by mirror/lifecycle, checked against CLI help.
# Keep this before scripted success responses: a permissive fake hid a required
# cards-create body in production. Unknown commands belong to other fixtures.
if name == 'anx':
    tokens = list(args)
    while tokens and tokens[0].startswith('--'):
        flag = tokens.pop(0)
        if flag in ('--workspace', '--as', '--base-url') and tokens:
            tokens.pop(0)
        elif flag != '--json':
            print('ANX contract: invalid global flag '+flag, file=sys.stderr)
            sys.exit(2)
    path = tuple(tokens[:2]) if tokens[:1] in (['cards'], ['boards'], ['work'], ['bridge'], ['inbox']) else tuple(tokens[:1])
    # (positional count, required value flags, optional value flags, boolean flags)
    contracts = {
        ('cards', 'create'): (0, {'--board', '--handle', '--title', '--column'}, {'--body', '--body-file', '--request-key'}, set()),
        ('cards', 'get'): (1, set(), set(), set()),
        ('cards', 'list'): (0, set(), {'--board'}, set()),
        ('cards', 'move'): (1, {'--column'}, {'--reason'}, set()),
        ('cards', 'resolve'): (1, set(), {'--reason', '--body', '--body-file', '--resolution-ref', '--column'}, set()),
        ('boards', 'get'): (1, set(), set(), set()),
        ('boards', 'create'): (0, {'--title', '--summary'}, set(), set()),
        ('work', 'cadence'): (1, {'--every', '--budget', '--stop-rule'}, set(), set()),
        ('orient',): (0, set(), set(), set()),
        ('context',): (1, set(), set(), set()),
        ('note',): (2, set(), {'--next', '--evidence', '--decided-by'}, {'--met'}),
        ('ask',): (1, {'--subject-ref', '--situation', '--impact', '--recommend', '--why', '--if-no-answer'},
                   {'--alternative', '--details', '--evidence', '--by', '--supersedes'}, {'--again'}),
        ('inbox', 'list'): (0, set(), {'--status', '--limit', '--cursor'}, {'--unread'}),
        ('inbox', 'read'): (1, set(), set(), {'--force'}),
        ('api',): (1, {'--method', '--path', '--from-file'}, set(), set()),
        **{('bridge', verb): (0, set(), set(), set()) for verb in ('status', 'install', 'add')},
    }
    if path in contracts:
        count, required, optional, switches = contracts[path]
        positionals, seen, error = [], set(), ''
        remaining = tokens[len(path):]
        while remaining:
            token = remaining.pop(0)
            if token in required | optional:
                seen.add(token)
                if not remaining or not remaining.pop(0).strip():
                    error = 'missing value for '+token; break
            elif token in switches:
                seen.add(token)
            elif token.startswith('--'):
                error = 'unknown flag '+token; break
            else:
                positionals.append(token)
        if path == ('cards', 'create') and not {'--body', '--body-file'} & seen:
            error = '--body or --body-file is required'
        if path == ('cards', 'resolve') and not {'--body', '--body-file', '--resolution-ref'} & seen:
            error = 'resolution evidence (--body, --body-file or --resolution-ref) is required'
        if error or required - seen or len(positionals) != count:
            print(f'ANX contract {path}: {error or "missing flags/positionals"} {sorted(required-seen)}', file=sys.stderr)
            sys.exit(2)
        if path == ('note',) and '--met' in seen and ('--evidence' not in seen or '--next' in seen):
            print('ANX contract: --met needs --evidence and excludes --next', file=sys.stderr)
            sys.exit(2)
if "--request-file" in args:
    record["request"] = json.loads(Path(args[args.index("--request-file") + 1]).read_text())
# ANX rejects untyped local evidence; enforce that boundary in offline tests.
if name == "anx" and "--evidence" in args:
    evidence = args[args.index("--evidence") + 1]
    prefix, sep, value = evidence.strip().partition(":")
    if not (sep and prefix.strip() and value.strip()):
        print("--evidence requires a typed ref", file=sys.stderr)
        sys.exit(2)
if name == "anx" and "ask" in args:
    index = args.index("ask")
    if index+1 >= len(args) or args[index+1].startswith("--"):
        print("ask requires a title", file=sys.stderr)
        sys.exit(2)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(record) + "\n")
responses = json.loads(Path(os.environ["FAKE_RESPONSES"]).read_text()) if os.environ.get("FAKE_RESPONSES") else []
for response in responses:
    if response["tool"] == name and all(word in args for word in response.get("contains", [])):
        payload = response.get("json", {"ok": True, "result": {}})
        if name == "agentctl" and isinstance(payload, dict) and "ok" in payload:
            # agentctl prints schema_version 1 on every envelope; fixtures written without it would fail closed.
            payload = {"schema_version": 1, **payload}
        print(response.get("stdout", json.dumps(payload)))
        print(response.get("stderr", ""), file=sys.stderr)
        sys.exit(response.get("exit", 0))
if name == 'anx' and 'ask' in args and any('check=loop_failure' in arg for arg in args):
    result = {'ask_id': 'event:ask-' + str(len(Path(os.environ['FAKE_LOG']).read_text().splitlines()))}
    if os.environ.get('FAKE_ANX_ASK_STATE'):
        # Opt-in model of Decide's replacement semantics, exercised via real argv.
        target = Path(os.environ['FAKE_ANX_ASK_STATE'])
        asks = json.loads(target.read_text()) if target.exists() else {}
        if '--supersedes' in args:
            asks[args[args.index('--supersedes') + 1]]['status'] = 'superseded'
        asks[result['ask_id']] = {'ask_id': result['ask_id'], 'status': 'open'}
        target.write_text(json.dumps(asks))
elif name == 'anx' and 'inbox' in args and 'list' in args and os.environ.get('FAKE_ANX_ASK_STATE'):
    target = Path(os.environ['FAKE_ANX_ASK_STATE'])
    result = {'items': [item for item in json.loads(target.read_text()).values() if item['status'] != 'superseded']}
elif name == 'anx' and 'inbox' in args and 'list' in args:
    result = {'items': []}
elif name == 'anx' and 'api' in args and '/agent-inbox/answers/read' in args:
    body = json.loads(Path(args[args.index('--from-file')+1]).read_text())
    result = {'answer': {'read': True, 'answer_event_id': body['answer_event_id']}}
elif name == "anx" and "get" in args:
    result = {"card": {"id": "card-id", "handle": args[-1].removeprefix("card:")}}
    if args[-1] == 'card:loop-audit':
        result['card'].update(board_ref='board:ck-sample',cadence={
            'every_seconds':1800,'progress_budget_seconds':1800,'stop_rule':'STOP file present'})
elif name == "agentctl" and "recent" in args:
    result = {"executions": []}
elif name == "agentctl" and "delegate" in args and "--plan" in args:
    result = {"native": {"permissions": "read_only"}}
elif name == "agentctl" and "delegate" in args and "--detach" in args:
    result = {"id": "exec-fake-one-two-three-four-five"}
elif name == "agentctl" and "status" in args:
    result = {"state": "completed"}
elif name == "agentctl" and "await" in args:
    print("Lane smoke completed")
    sys.exit(0)
elif name == "agentctl" and "delegate" in args:
    print('MISSED_JSON_START\n{"items":[{"severity":"med","kind":"followup","title":"Verify receipt","evidence":"snapshot","next_action":"Check card"}]}\nMISSED_JSON_END')
    sys.exit(0)
elif name == "gh":
    print("[]")
    sys.exit(0)
else:
    result = {}
envelope = {"ok": True, "result": result}
if name == "agentctl":
    envelope["schema_version"] = 1  # agentctl prints schema_version 1 on every success
print(json.dumps(envelope))
