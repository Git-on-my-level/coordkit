#!/bin/bash
# Customize paths, labels and preconditions on disposable copies before use.
# Reference cutover: repoint the lead's ~/.claude hooks from the incumbent lead_wait to coordkit, with a
# recorded backup, then `lead adopt --actor ck-omi-lead`. Run it AFTER apply-cutover.sh has exited 0 (manifest live).
# Written for an operator session other than bus-maintainer, which may not edit harness settings.
#
#   apply-lead-hooks.sh --lead-session SID [--config-dir DIR]            dry run: preconditions + exact hook diff
#   apply-lead-hooks.sh --lead-session SID [--config-dir DIR] --apply    back up, install, register, verify; on failure restore all
# DIR is the lead's Claude Code config dir (default ~/.claude). SID is the live
# lead's session id: its transcript must be under DIR/projects and written in the last 24h. The ask-guard only guards
# the registered lead session, so without it the guard is a no-op. Where DIR has the incumbent lead_wait pair it is
# swapped for coordkit's; where it has none (~/.claude-1) coordkit's pair is added. Run it from a session that may edit
# DIR/settings.json (not one living in DIR under auto mode).
#
# Exit codes: 0 done (or dry run ok); 1 failed and rolled back to the backup; 2 a precondition failed (nothing
# changed); 3 rollback incomplete (read the log; the receipt is examples/omi/.coordkit/hook-rollback.json).
# Rollback later, by hand: `coordkit down` is NOT the tool here (it also stops the live jobs). Use
#   PYTHONPATH=src python3.11 -c 'from coordkit.hooks import restore; from coordkit.manifest import load; restore(load("<M>"))'
PY=${COORDKIT_PYTHON:-python3.11}
M=${LEAD_HOOKS_MANIFEST:-$HOME/workspace/omi/coordkit.toml}  # overrides exist for rehearsal on copies
SRC=$HOME/src/coordkit/src
ROOT=${LEAD_HOOKS_ROOT:-$HOME/src/coordkit/examples/omi/.coordkit}
TARGET=${LEAD_HOOKS_TARGET:-}
export PYTHONPATH=$SRC
MODE=dry; SID=""; CONFIG_DIR=$HOME/.claude
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) MODE=apply ;;
    --lead-session) SID=${2:-}; shift ;;
    --config-dir) CONFIG_DIR=${2:-}; shift ;;
    *) echo "usage: $0 --lead-session SID [--config-dir DIR] [--apply]"; exit 2 ;;
  esac; shift
done
case "$CONFIG_DIR" in $HOME/.claude|$HOME/.claude-1|$HOME/.claude-2) ;; *)
  [ -n "${LEAD_HOOKS_TARGET:-}" ] || { echo "--config-dir must be one of the managed config dirs (got $CONFIG_DIR)"; exit 2; } ;; esac
[ -n "$TARGET" ] || TARGET=$CONFIG_DIR/settings.json
echo "$SID" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' \
  || { echo "usage: $0 --lead-session SID [--apply] (SID: the live lead's Claude Code session id)"; exit 2; }

LOCK=$ROOT/cutover/.apply.lock; HAVE_LOCK=0
release_lock() { [ $HAVE_LOCK = 1 ] && rm -rf "$LOCK"; HAVE_LOCK=0; }
quit() { release_lock; exit "$1"; }
if mkdir "$LOCK" 2>/dev/null; then echo $$ > "$LOCK/pid"; HAVE_LOCK=1; trap 'quit 2' INT TERM HUP
else echo "another apply/rollback holds $LOCK (pid $(cat "$LOCK/pid" 2>/dev/null))"; exit 2; fi
if [ $MODE = apply ]; then RUN=$ROOT/cutover/s5-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$RUN" || quit 2
else RUN=$(mktemp -d /tmp/apply-lead-hooks-dryrun.XXXXXX) || quit 2; fi
LOG=$RUN/apply.log
ck() { "$PY" -m coordkit.cli --manifest "$M" "$@"; }
say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
fail_pre() { say "PRECONDITION_FAILED $*"; quit 2; }

# -- preconditions (read-only) -----------------------------------------------------------------------------
grep -q '^mode = "live"' "$M" || fail_pre "manifest is not live: run apply-cutover.sh --apply first"
grep -q "^root = \"$ROOT\"" "$M" || fail_pre "manifest root is not $ROOT"
ls "$ROOT"/cutover/apply-*/done >/dev/null 2>&1 || fail_pre "no applied cutover run (done marker) found"
[ -e "$ROOT/hook-rollback.json" ] && fail_pre "a lead-hook receipt already exists ($ROOT/hook-rollback.json): inspect it; never install twice"
# Snapshots of the identity files rollback restores; a failed copy fails here, never later as "no prior file".
LS_FILE=$ROOT/wait/lead/lead-session
for f in "$ROOT/lead.json" "$LS_FILE"; do
  b="$RUN/$(basename "$f").before"
  if [ -e "$f" ]; then { cp -p "$f" "$b" && cmp -s "$f" "$b"; } || fail_pre "cannot snapshot $f"
  else : > "$b.absent" || fail_pre "cannot record that $f is absent"; fi
done
transcript=$(ls "$CONFIG_DIR"/projects/*/"$SID".jsonl 2>/dev/null | head -1)
[ -n "$transcript" ] || fail_pre "no transcript for lead session $SID under $CONFIG_DIR/projects"
[ $(( $(date +%s) - $(stat -f %m "$transcript") )) -lt 86400 ] || fail_pre "lead session $SID has not written its transcript in 24h ($transcript)"
say "LEAD_SESSION $SID ($transcript)"
[ -r "$TARGET" ] || fail_pre "cannot read $TARGET"
# The exact reviewed transformation (CUTOVER s5): remove the two incumbent lead_wait hooks (PreToolUse
# AskUserQuestion ask-guard, UserPromptSubmit pending), add coordkit's ask-guard and Stop pending; nothing else.
"$PY" - "$M" "$TARGET" <<'PY' >>"$LOG" 2>&1 || fail_pre "the hook diff is not the reviewed one (see $LOG)"
import json, sys
from pathlib import Path
from coordkit.hooks import transformation, entries
from coordkit.manifest import load
m = load(sys.argv[1]); before = json.loads(Path(sys.argv[2]).read_text()); after = transformation(m, before, fresh_ok=True)
b, a = entries(before), entries(after)
gone = [json.loads(k) for k in set(b) - set(a)]; new = [json.loads(k) for k in set(a) - set(b)]
for e, md, h in gone: print('REMOVE', e, md, h['command'][:200])
for e, md, h in new: print('ADD', e, md, h['command'][:200])
swap = (sorted((e, md.get('matcher')) for e, md, h in gone) == [('PreToolUse', 'AskUserQuestion'), ('UserPromptSubmit', None)]
        and all('lead_wait.py' in h['command'] for _, _, h in gone))
print('MODE', 'swap' if swap else 'add' if not gone else 'unexpected')
ok = ((swap or not gone)
      and sorted((e, md.get('matcher'), h['command'].split(' wait ', 1)[-1]) for e, md, h in new)
          == [('PreToolUse', 'AskUserQuestion', 'ask-guard'), ('Stop', None, 'pending --brief')]
      and all(str(m.file) in h['command'] for _, _, h in new)
      and {k: v for k, v in before.items() if k != 'hooks'} == {k: v for k, v in after.items() if k != 'hooks'})
sys.exit(0 if ok else 1)
PY
grep -E '^(MODE|REMOVE|ADD)' "$LOG" | while read -r l; do say "DIFF $l"; done
ck doctor >"$RUN/doctor-pre.txt" 2>&1 || fail_pre "doctor is not green before the hook change (see $RUN/doctor-pre.txt)"
say "PRECONDITIONS_OK mode=$MODE target=$TARGET run=$RUN"
if [ $MODE = dry ]; then
  say "WOULD back up $TARGET, install the diff above with a receipt, register lead session $SID, probe ask-guard (lead + other session) + pending, lead adopt --actor ck-omi-lead, doctor, up --plan"
  say "DRY_RUN_OK: nothing changed (log in $RUN)"; quit 0
fi

# -- apply ------------------------------------------------------------------------------------------------
rollback() {
  trap '' INT TERM HUP EXIT
  say "ROLLBACK starting ($1)"
  if "$PY" - "$M" "$RUN/settings.json" "$TARGET" <<'PY' >>"$LOG" 2>&1; then
import json, sys
from pathlib import Path
from coordkit.hooks import restore
from coordkit.manifest import load
restore(load(sys.argv[1]))
assert json.loads(Path(sys.argv[3]).read_text()) == json.loads(Path(sys.argv[2]).read_text()), 'settings differ from the backup'
PY
    ident=ok
    for f in "$ROOT/lead.json" "$LS_FILE"; do
      b="$RUN/$(basename "$f").before"
      if [ -e "$b" ]; then { cp -p "$b" "$f" && cmp -s "$b" "$f"; } || ident="failed to restore $f"
      elif [ -e "$b.absent" ]; then rm -f "$f"; [ -e "$f" ] && ident="failed to remove $f"
      else ident="no snapshot record for $f"; fi
    done
    if [ "$ident" = ok ]; then
      touch "$RUN/rolled-back"; say "ROLLED_BACK: $TARGET equals the backup; lead.json and lead-session restored"; quit 1
    fi
    say "ROLLBACK_INCOMPLETE: settings restored but $ident (snapshots in $RUN)"; quit 3
  fi
  say "ROLLBACK_INCOMPLETE: restore failed or settings differ from $RUN/settings.json (see $LOG); repair by hand"; quit 3
}
{ cp -p "$TARGET" "$RUN/settings.json" && cmp -s "$TARGET" "$RUN/settings.json"; } || fail_pre "backup failed"
trap 'rollback "error exit"' EXIT
trap 'rollback "signal"' INT TERM HUP
"$PY" - "$M" "$TARGET" "$RUN/settings.json" <<'PY' >>"$LOG" 2>&1 || rollback "install failed"
import sys
from coordkit.hooks import install
from coordkit.manifest import load
install(load(sys.argv[1]), sys.argv[2], sys.argv[3], fresh_ok=True)
PY
say "INSTALLED lead hooks (receipt $ROOT/hook-rollback.json, backup $RUN/settings.json)"
grep -q lead_wait.py "$TARGET" && rollback "an incumbent lead_wait hook is still present"
ck wait register-lead --session "$SID" >>"$LOG" 2>&1 || rollback "register-lead failed"
[ "$(cat "$LS_FILE" 2>/dev/null)" = "$SID" ] || rollback "lead session not registered at $LS_FILE"
# What the guard must decide right now: deny for the lead while any lane is in flight, otherwise stay silent.
"$PY" - "$M" <<'PY' >"$RUN/inflight.txt" 2>>"$LOG" || rollback "cannot compute in-flight lanes for the probe"
import sys
from coordkit import wait
from coordkit.manifest import load
m = load(sys.argv[1]); wait.CONFIG = m; wait.ROOT = m.paths.root/'wait'
cfg = wait.State('lead').data.get('config') or {}
print(len(wait.in_flight(cfg.get('events') or [wait.default_events()])))
PY
running=$(cat "$RUN/inflight.txt"); echo "$running" | grep -Eq '^[0-9]+$' || rollback "in-flight count unreadable: $running"
probe() { printf '{"tool_name":"AskUserQuestion","tool_input":{"questions":[]},"session_id":"%s"}' "$1" | ck wait ask-guard 2>>"$LOG"; }
lead_out=$(probe "$SID"); lrc=$?; other_out=$(probe 00000000-0000-0000-0000-000000000000); orc=$?
{ [ $lrc = 0 ] && [ $orc = 0 ] && [ -z "$other_out" ]; } || rollback "ask-guard probe failed (rc lead=$lrc other=$orc other_out=${other_out:0:80})"
if [ "$running" -gt 0 ]; then
  echo "$lead_out" | grep -q '"permissionDecision": "deny"' || rollback "ask-guard did not deny the lead with $running lane(s) in flight"
else
  [ -z "$lead_out" ] || rollback "ask-guard decided with no lane in flight: ${lead_out:0:120}"
fi
ck wait pending --brief >"$RUN/pending.txt" 2>>"$LOG" || rollback "pending --brief failed (rc=$?)"
grep -q '^lead-wait\[lead\]: ' "$RUN/pending.txt" || rollback "pending --brief printed no summary"
say "PROBES ok (in flight=$running; ask-guard $([ "$running" -gt 0 ] && echo denied || echo silent) for the lead, silent for another session; pending: $(head -1 "$RUN/pending.txt" | cut -c1-120))"
ck lead adopt --actor ck-omi-lead >>"$LOG" 2>&1 || rollback "lead adopt failed"
ck doctor --strict-dead-letters >"$RUN/doctor-post.txt" 2>&1; drc=$?
"$PY" - "$RUN/doctor-post.txt" "$drc" <<'PY' >>"$LOG" 2>&1 || rollback "doctor incomplete or red after the hook change (see doctor-post.txt)"
# Every check exactly once; green, except strict dead_letters (not caused by hooks); rc agrees.
import sys
names = 'escalation_limits manifest binaries anx agentctl reconciler_age github launchd dead_letters shadow_guard'.split()
seen, red = {}, []
for line in open(sys.argv[1]):
    if line.startswith('DOCTOR '):
        _, check, rest = line.rstrip('\n').split(' ', 2)
        seen[check] = seen.get(check, 0) + 1
        if not rest.startswith('green'): red.append(check)
rc = int(sys.argv[2])
ok = all(seen.get(n) == 1 for n in names) and set(red) <= {'dead_letters'} and (rc == 0) == (not red)
print('doctor-post seen', seen, 'red', red, 'rc', rc); sys.exit(0 if ok else 1)
PY
[ $drc = 0 ] || say "NOTE doctor --strict-dead-letters red only on dead_letters (not caused by hooks; review in s6)"
ck up --plan >"$RUN/post-hooks-plan.txt" 2>&1; urc=$?
[ $urc = 0 ] && grep -qx 'UP_PLAN_ONLY' "$RUN/post-hooks-plan.txt" || rollback "up --plan did not complete (rc=$urc; see post-hooks-plan.txt)"
grep -q '^UP_DIFF' "$RUN/post-hooks-plan.txt" && rollback "up --plan shows drift after the hook change (see post-hooks-plan.txt)"
trap - EXIT INT TERM HUP
touch "$RUN/done"
say "LEAD_HOOKS_APPLIED: incumbent lead_wait hooks replaced by coordkit ask-guard + Stop pending; lead session $SID registered; lead adopted ck-omi-lead; doctor complete; up --plan clean. Review the recorded verification output."
quit 0
