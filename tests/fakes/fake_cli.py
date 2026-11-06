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
if "--request-file" in args:
    record["request"] = json.loads(Path(args[args.index("--request-file") + 1]).read_text())
# ANX rejects untyped local evidence; enforce that boundary in offline tests.
if name == "anx" and "--evidence" in args:
    evidence = args[args.index("--evidence") + 1]
    if ":" not in evidence:
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
        print(response.get("stdout", json.dumps(response.get("json", {"ok": True, "result": {}}))))
        print(response.get("stderr", ""), file=sys.stderr)
        sys.exit(response.get("exit", 0))
if name == "anx" and "get" in args:
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
print(json.dumps({"ok": True, "result": result}))
