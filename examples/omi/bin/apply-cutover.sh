#!/bin/bash
# Customize paths, labels and preconditions on disposable copies before use.
# Omi coordkit cutover, see CUTOVER.md.
#
#   apply-cutover.sh            dry run: read-only checks, prints every step; writes nothing (logs go to a temp dir)
#   apply-cutover.sh --apply    pause the duplicate jobs, flip $HOME/workspace/omi/coordkit.toml to live,
#                               `coordkit up --apply` (audit/reconciliation jobs + question hooks in ~/.claude, ~/.claude-1, ~/.claude-2),
#                               verify (doctor complete and green, jobs loaded AND one clean run each for sweep and
#                               reconcile, hooks present, hook fail-open probe). ANY failure, error exit or signal
#                               rolls back, fail-closed.
#   apply-cutover.sh --rollback RUN_DIR
#                               finish a rollback from RUN_DIR/state.env after an uncatchable kill mid-apply.
#
# Run it from a session that may edit ~/.claude*/settings.json (some harness policies refuse self-modification). Exit codes: 0 applied and verified; 1 failed and rolled back cleanly (pre-cutover state);
# 2 precondition failed (nothing changed); 3 rollback incomplete: incumbents deliberately left PAUSED, read the
# log and CUTOVER.md rollback guidance before touching anything; 4 applied and verified but the done journal could not
# be written (do not --rollback that run).
set -u
MODE=dry
case "${1:-}" in --apply) MODE=apply ;; --rollback) MODE=rollback ;; "") ;; *) echo "usage: $0 [--apply | --rollback RUN_DIR]"; exit 2 ;; esac
if [ "$MODE" = rollback ] && [ -z "${2:-}" ]; then echo "usage: $0 --rollback RUN_DIR"; exit 2; fi  # before the lock
CK=${COORDKIT_CHECKOUT:-$HOME/src/coordkit}
M=$HOME/workspace/omi/coordkit.toml
ROOT=$CK/examples/omi/.coordkit
PY=${COORDKIT_PYTHON:-python3.11}
U=$(id -u)
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" PYTHONPATH=$CK/src
INCUMBENT=(com.example.omi-missedwork com.example.omi.shadow com.example.omi.obligations)
LIVE_JOBS=(com.example.omi.loop.audit com.example.omi.reconcile)
KICK_JOBS=(com.example.omi.reconcile)  # loop.audit delegates a paid agent run: not kicked
# This example manages three explicitly scoped harness config directories.
SETTINGS=($HOME/.claude/settings.json $HOME/.claude-1/settings.json $HOME/.claude-2/settings.json)
CHECKS="escalation_limits manifest binaries anx agentctl reconciler_age github launchd dead_letters shadow_guard"

# One apply or rollback at a time, host-wide (atomic mkdir; the holder's pid is inside). Released on every exit.
LOCK=$ROOT/cutover/.apply.lock
HAVE_LOCK=0
release_lock() { [ $HAVE_LOCK = 1 ] && rm -rf "$LOCK"; HAVE_LOCK=0; }
quit() { release_lock; exit "$1"; }
if [ "$MODE" != dry ]; then
  mkdir -p "$ROOT/cutover" || { echo "cannot create $ROOT/cutover"; exit 2; }
  if mkdir "$LOCK" 2>/dev/null; then echo $$ > "$LOCK/pid"; HAVE_LOCK=1; trap 'quit 2' INT TERM HUP  # pre-mutation interrupts
  else echo "another apply/rollback holds $LOCK (pid $(cat "$LOCK/pid" 2>/dev/null)); if that process is gone, inspect its run, then remove the lock"; exit 2; fi
fi
if [ "$MODE" = rollback ]; then RUN=$2; else
  if [ "$MODE" = apply ]; then
    RUN=$(mktemp -d "$ROOT/cutover/apply-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX") || { echo "cannot create a run dir"; quit 2; }
  else RUN=$(mktemp -d /tmp/apply-cutover-dryrun.XXXXXX) || { echo "cannot create a run dir"; exit 2; }; fi
fi
LOG=$RUN/apply.log
ck() { "$PY" -m coordkit.cli --manifest "$M" "$@"; }
say() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
loaded() { launchctl list "$1" >/dev/null 2>&1; }
snap() { echo "$RUN/$(echo "$1" | tr '/' '_').before"; }

write_mode() {  # atomic: temp file in the same dir, then rename; exactly one mode line changes
  "$PY" - "$M" "$1" <<'PY'
import os, sys, tempfile
from pathlib import Path
p, want = Path(sys.argv[1]), sys.argv[2]
s = p.read_text(); old = 'mode = "shadow"' if want == 'live' else 'mode = "live"'
if s.count(old) != 1:
    sys.exit(f'unexpected manifest mode (want {want})')
fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='.coordkit.toml.')
with os.fdopen(fd, 'w') as f:
    f.write(s.replace(old, f'mode = "{want}"', 1)); f.flush(); os.fsync(f.fileno())
os.chmod(tmp, p.stat().st_mode & 0o7777)  # keep the manifest's permissions across the atomic rename
os.replace(tmp, p)
PY
}

doctor_ok() {  # doctor_ok FILE allow_drift RC: every check exactly once; green (or only the expected drift); rc agrees
  "$PY" - "$1" "$2" "$CHECKS" "$3" <<'PY'
import sys
path, allow, names, rc = sys.argv[1], sys.argv[2] == 'yes', sys.argv[3].split(), int(sys.argv[4])
expected = {'missing job com.example.omi.loop.audit', 'missing job com.example.omi.reconcile',
            'unexpected job com.example.omi.shadow',
            'unexpected job com.example.omi.obligations'}
seen, bad = {}, []
for line in open(path):
    if not line.startswith('DOCTOR '):
        continue
    _, check, rest = line.rstrip('\n').split(' ', 2)
    seen[check] = seen.get(check, 0) + 1
    if rest.startswith('green'):
        continue
    if allow and check == 'launchd' and rest.startswith('red ') and {x.strip() for x in rest[4:].split(';') if x.strip()} <= expected:
        continue
    # A stamp left by a rolled-back live pass: the apply archives reconcile/state.json before `up`, and the
    # post-apply doctor (allow=no) still requires this check green.
    if allow and check == 'reconciler_age' and rest == 'red reconciler overdue beyond two intervals':
        continue
    bad.append(line.strip())
    continue
missing = [n for n in names if seen.get(n) != 1]
reds = sum(1 for l in open(path) if l.startswith('DOCTOR ') and not l.split(' ', 2)[2].startswith('green'))
rc_ok = rc == 0 if reds == 0 else (allow and rc == 1)  # doctor exits 1 iff some check is red
if missing or bad or not rc_ok:
    print('missing or repeated checks:', missing, 'rc:', rc, '\nred:', '\n'.join(bad)); sys.exit(1)
PY
}

# -- rollback (fail-closed) ------------------------------------------------------------------------------
PHASE=pre; ORIGINAL=""
record() {  # atomic and checked: a temp file renamed over state.env
  printf 'PHASE=%q\nORIGINAL=%q\n' "$PHASE" "$ORIGINAL" > "$RUN/state.env.tmp" && mv "$RUN/state.env.tmp" "$RUN/state.env" \
    || { say "STATE_JOURNAL_WRITE_FAILED"; return 1; }
}
rollback() {
  # In-memory recursion guard (not the journaled PHASE): a new process resumes a journaled `rolling` run.
  [ "${IN_ROLLBACK:-0}" = 1 ] && return
  IN_ROLLBACK=1; PHASE=rolling; trap - EXIT; trap '' INT TERM HUP  # a rollback runs to completion
  record || say "ROLLBACK could not journal PHASE=rolling"
  say "ROLLBACK starting ($1)"
  local bad=0 lanes
  actual_state() {  # what is really true right now, for the operator
    local st="STOP=$([ -e "$ROOT/STOP" ] && echo present || echo MISSING) STOP-sweep=$([ -e "$ROOT/STOP-sweep" ] && echo present || echo MISSING)"
    for j in $ORIGINAL "${LIVE_JOBS[@]}"; do st="$st $j=$(loaded "$j" && echo LOADED || echo unloaded)"; done
    echo "$st manifest=$(grep -m1 '^mode' "$M" 2>/dev/null)"
  }
  stop_here() {
    PHASE=rollback_incomplete; record || true
    say "ROLLBACK_INCOMPLETE ($1). Actual state: $(actual_state). Read $LOG; CUTOVER.md section 7; retry with --rollback $RUN."; quit 3
  }
  fence_and_quiesce() {  # STOP fence present and verified, every originally loaded incumbent paused and verified
    touch "$ROOT/STOP" "$ROOT/STOP-sweep" >>"$LOG" 2>&1
    local fenced=yes; { [ -e "$ROOT/STOP" ] && [ -e "$ROOT/STOP-sweep" ]; } || fenced=no
    for j in $ORIGINAL; do
      if loaded "$j"; then launchctl bootout gui/$U/$j >>"$LOG" 2>&1; fi
      if loaded "$j"; then say "ROLLBACK cannot pause $j"; return 1; fi
    done
    [ $fenced = yes ] || { say "ROLLBACK STOP fence missing (incumbents are paused)"; return 1; }
  }
  # 0. a retried rollback may find incumbents a killed rollback had resumed: re-fence and pause them first.
  local fenced=0; fence_and_quiesce && fenced=1
  new_lanes() {  # checked inventory of coordkit lanes started during this run
    [ -d "$ROOT/runs/req" ] || return 0
    lanes=$(find "$ROOT/runs/req" -name '*.plan.json' -newer "$RUN/started" 2>>"$LOG") || stop_here "cannot inventory coordkit lanes"
    [ -z "$lanes" ] || stop_here "a coordkit lane started during this run; coordkit jobs are unloaded (no new launches), drain or transfer it, then rerun --rollback: $lanes"
  }
  # 1. quiesce coordkit's own launchers too, whatever step 0 found (the manifest stays live), verified.
  local quiet=1
  for j in "${LIVE_JOBS[@]}"; do
    if loaded "$j"; then launchctl bootout gui/$U/$j >>"$LOG" 2>&1; fi
    if loaded "$j"; then say "ROLLBACK cannot quiesce $j"; quiet=0; fi
  done
  [ $fenced = 1 ] || stop_here "the STOP fence or an incumbent pause could not be verified"
  [ $quiet = 1 ] || stop_here "a coordkit live job could not be unloaded"
  new_lanes
  # 2. coordkit down while the manifest still says live: exit 0 and DOWN_OK, or stop (manifest stays live).
  if grep -q '^mode = "live"' "$M" 2>/dev/null; then
    ck down >"$RUN/down.txt" 2>&1; local drc=$?; cat "$RUN/down.txt" >>"$LOG"
    { [ $drc = 0 ] && grep -q '^DOWN_OK' "$RUN/down.txt"; } || stop_here "down rc=$drc without DOWN_OK"
  fi
  for j in "${LIVE_JOBS[@]}"; do if loaded "$j"; then stop_here "$j still loaded after down"; fi; done
  new_lanes  # recheck: nothing may have started between the first scan and down
  # 3. the manifest back to its exact pre-run bytes and mode (atomic), verified.
  if ! cmp -s "$M" "$RUN/manifest.before"; then
    "$PY" -c 'import os,shutil,sys,tempfile; fd,t=tempfile.mkstemp(dir=os.path.dirname(sys.argv[2]),prefix=".coordkit.toml.rb."); os.close(fd); shutil.copy2(sys.argv[1],t); os.replace(t,sys.argv[2])' \
      "$RUN/manifest.before" "$M" >>"$LOG" 2>&1 && cmp -s "$M" "$RUN/manifest.before" || stop_here "manifest restore failed"
  fi
  # 4. settings exactly as before (down reversed the hook receipts) BEFORE anything is resumed.
  for f in "${SETTINGS[@]}"; do
    "$PY" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))==json.load(open(sys.argv[2])) else 1)' "$f" "$(snap "$f")" \
      || stop_here "settings differ from the pre-run snapshot: $f"
  done
  # 5. only now resume exactly the jobs that were loaded at the start, then clear the STOP markers.
  for j in $ORIGINAL; do
    path=$(cat "$RUN/plist-path.$j" 2>/dev/null)
    { [ -n "$path" ] && cmp -s "$path" "$RUN/plist.$j"; } || stop_here "$j definition changed or unrecorded (recorded path: ${path:-none})"
  done
  rm -f "$ROOT/STOP" "$ROOT/STOP-sweep"  # this run created both (pre-existing markers fail the preconditions)
  if [ -e "$ROOT/STOP" ] || [ -e "$ROOT/STOP-sweep" ]; then stop_here "could not clear the STOP markers (jobs still paused)"; fi
  local resumed=""
  for j in $ORIGINAL; do
    loaded "$j" || launchctl bootstrap gui/$U "$(cat "$RUN/plist-path.$j")" >>"$LOG" 2>&1
    if loaded "$j"; then resumed="$resumed $j"; else say "ROLLBACK resume $j FAILED"; bad=1; break; fi
  done
  if [ $bad = 1 ]; then  # all or nothing: restore the fence and re-pause everything, verified
    if fence_and_quiesce; then stop_here "an incumbent failed to resume; fence restored and all incumbents re-paused (verified)"
    else stop_here "an incumbent failed to resume AND the fence or a re-pause could not be verified: inspect launchctl now"; fi
  fi
  PHASE=rolled_back; record || say "ROLLBACK could not journal PHASE=rolled_back"
  touch "$RUN/rolled-back" || say "ROLLBACK could not write the rolled-back marker"
  say "ROLLED_BACK cleanly (pre-cutover state)"; quit 1
}

if [ "$MODE" = rollback ]; then
  [ -f "$RUN/state.env" ] && [ -f "$RUN/manifest.before" ] || { echo "no state.env/manifest.before in $RUN"; quit 2; }
  . "$RUN/state.env"; LOG=$RUN/apply.log
  case "${PHASE:-}" in mutating|rolling|rollback_incomplete) terminal=no ;; *) terminal=yes ;; esac
  if [ -e "$RUN/done" ] || [ -e "$RUN/rolled-back" ] || [ $terminal = yes ]; then
    echo "refusing: run $RUN is not an interrupted apply (PHASE=${PHASE:-unset}${RUN:+, done marker: $([ -e "$RUN/done" ] && echo yes || echo no)}); a completed cutover is undone with CUTOVER.md section 7"
    quit 2
  fi
  rollback "manual --rollback after an interrupted apply"
fi

# -- preconditions (read-only) ----------------------------------------------------------------------------
fail_pre() { say "PRECONDITION_FAILED $*"; quit 2; }
[ -f "$M" ] || fail_pre "no live manifest at $M"
grep -q '^mode = "shadow"' "$M" || fail_pre "manifest is not in shadow mode (already applied, or edited)"
grep -q "^root = \"$ROOT\"" "$M" || fail_pre "manifest root is not the absolute shared root $ROOT"
"$PY" - "$M" <<'PY' >>"$LOG" 2>&1 || fail_pre "apply scope is not pinned (bridge/RC must be off; exactly ~/.claude, ~/.claude-1 and ~/.claude-2)"
# `down` reverses jobs and hook receipts only: anything else up --apply could enroll must be off.
import sys
from pathlib import Path
from coordkit.manifest import load
m = load(sys.argv[1])
dirs = sorted(str(Path(d).expanduser()) for d in m.questions.config_dirs)
ok = (not m.anx.bridge_answers and not m.rc_host.enabled
      and dirs == sorted(str(Path.home()/d) for d in ('.claude', '.claude-1', '.claude-2')))
print('scope', dict(bridge=m.anx.bridge_answers, rc=m.rc_host.enabled, dirs=dirs)); sys.exit(0 if ok else 1)
PY
for f in "${SETTINGS[@]}"; do [ -r "$f" ] || fail_pre "cannot read $f"; done
if [ -e "$ROOT/STOP" ] || [ -e "$ROOT/STOP-sweep" ]; then fail_pre "a STOP marker already exists (someone fenced the pre-cutover jobs); resolve it first"; fi
for j in "${LIVE_JOBS[@]}"; do if loaded "$j"; then fail_pre "$j is already loaded"; fi; done
"$PY" - "$ROOT" <<'PY' || fail_pre "an unrestored question-hook receipt exists"
import json, sys
from pathlib import Path
sys.exit(1 if any(not json.loads(p.read_text()).get('restored') for p in Path(sys.argv[1]).glob('hook-questions-*.json')) else 0)
PY
ck doctor >"$RUN/doctor-pre.txt" 2>&1; drc=$?
doctor_ok "$RUN/doctor-pre.txt" yes "$drc" >>"$LOG" 2>&1 || fail_pre "shadow doctor (rc=$drc) is incomplete or has unexpected red checks (see $RUN/doctor-pre.txt)"
# Drain: incumbent executions finish under their own watchers; coordkit works only
# on the new ck-omi board. Snapshot the in-flight executions (the drain receipt) and refuse if any targets a
# ck-omi card, the only place coordkit could act on the same card. Fail closed if either source is unreadable.
agentctl recent --state nonterminal --limit 200 --output json >"$RUN/nonterminal.json" 2>>"$LOG" \
  || fail_pre "cannot inventory in-flight executions (agentctl recent)"
anx --workspace omi --as ck-omi-lead --json cards list --board ck-omi >"$RUN/ck-omi-cards.json" 2>>"$LOG" \
  || fail_pre "cannot list ck-omi board cards (anx)"
"$PY" - "$RUN/nonterminal.json" "$RUN/ck-omi-cards.json" <<'PY' >>"$LOG" 2>&1 || fail_pre "an in-flight execution targets a ck-omi card, or the inventory is incomplete (see the log)"
import json, sys
ex = json.load(open(sys.argv[1]))
cards = json.load(open(sys.argv[2]))
if not ex.get('ok') or ex['result'].get('has_more') or not cards.get('ok') or (cards['result'].get('has_more') or cards['result'].get('next_cursor')):
    print('incomplete inventory'); sys.exit(1)
handles = {c.get('handle') for c in cards['result'].get('cards', [])}
live = [(e['id'], l[len('anx.card.'):]) for e in ex['result']['executions'] for l in e.get('labels') or [] if l.startswith('anx.card.')]
clash = [x for x in live if x[1] in handles]
print('in-flight executions', len(ex['result']['executions']), 'card-linked', len(live), 'ck-omi cards', len(handles), 'clash', clash)
sys.exit(1 if clash else 0)
PY
# Rehearse reconciliation read-only against a disposable copy of the bus: a temp copy
# of the state root as the apply leaves it (mirror cursor at the bus end, no stale reconcile state), the manifest
# in live mode pointed at the copy, `reconcile --dry-run`. It must finish inside the kick window without a
# traceback (rc 0 or 5) and leave every bus stock below its threshold (no first-pass stock asks for history).
REH=$(mktemp -d) || fail_pre "no temp dir for the reconcile rehearsal"
BUS_LOG=$("$PY" -c 'import sys; from coordkit.manifest import load; print(load(sys.argv[1]).paths.bus_log)' "$M") || fail_pre "cannot resolve the bus log"
{ rsync -a --exclude cutover "$ROOT/" "$REH/root/" && rm -f "$REH/root/reconcile/state.json" && mkdir -p "$REH/root/state" \
  && wc -l < "$BUS_LOG" | tr -d ' ' > "$REH/root/state/mirror.cursor" \
  && sed -e 's/^mode = "shadow"/mode = "live"/' -e "s#^root = \".*\"#root = \"$REH/root\"#" "$M" > "$REH/coordkit.toml" \
  && [ "$(grep -c '^root = ' "$REH/coordkit.toml")" = 1 ]; } || { rm -rf "$REH"; fail_pre "cannot stage the reconcile rehearsal"; }
t0=$(date +%s)
timeout 150 "$PY" -m coordkit.cli --manifest "$REH/coordkit.toml" reconcile --dry-run >"$RUN/reconcile-rehearsal.txt" 2>"$RUN/reconcile-rehearsal.err"; rrc=$?
secs=$(( $(date +%s) - t0 ))
timeout 120 "$PY" -m coordkit.cli --manifest "$REH/coordkit.toml" sweep --dry-run >"$RUN/sweep-rehearsal.txt" 2>"$RUN/sweep-rehearsal.err"; src=$?
rm -rf "$REH"
say "REHEARSAL live sweep --dry-run rc=$src ($(grep '^SWEEP findings=' "$RUN/sweep-rehearsal.txt" | cut -c1-80))"
{ [ "$src" = 0 ] || [ "$src" = 5 ]; } && ! grep -q Traceback "$RUN/sweep-rehearsal.err" && grep -q '^SWEEP findings=' "$RUN/sweep-rehearsal.txt" \
  || fail_pre "the live sweep rehearsal failed (rc=$src; see $RUN/sweep-rehearsal.err)"
say "REHEARSAL live reconcile --dry-run rc=$rrc in ${secs}s ($(grep -c would_fix "$RUN/reconcile-rehearsal.txt") would_fix; $(head -1 "$RUN/reconcile-rehearsal.err"))"
{ [ "$rrc" = 0 ] || [ "$rrc" = 5 ]; } && ! grep -q Traceback "$RUN/reconcile-rehearsal.err" \
  || fail_pre "the live reconcile rehearsal failed (rc=$rrc after ${secs}s; see $RUN/reconcile-rehearsal.err)"
"$PY" - "$RUN/reconcile-rehearsal.txt" <<'PY' >>"$LOG" 2>&1 || fail_pre "the first live reconcile would ask on a bus stock at threshold (see the log)"
import re, sys
over = [l.strip() for l in open(sys.argv[1]) for m in [re.match(r'RECONCILE stock (rulings|blockers|holds|asks|harvest|inbox) count=(\d+) .*threshold=(\d+)', l)]
        if m and int(m[2]) >= int(m[3])]
print('stock at threshold:', over); sys.exit(1 if over else 0)
PY
say "PRECONDITIONS_OK mode=$MODE manifest=$M root=$ROOT run=$RUN (drain receipt: $RUN/nonterminal.json)"

if [ "$MODE" = dry ]; then
  for j in "${INCUMBENT[@]}"; do if loaded "$j"; then say "WOULD PAUSE $j"; else say "WOULD SKIP $j (not loaded; rollback never resumes it)"; fi; done
  [ -e "$ROOT/reconcile/state.json" ] && say "WOULD ARCHIVE reconcile/state.json (left by a rolled-back live pass) into the run dir"
  say "WOULD touch STOP + STOP-sweep, seed state/mirror.cursor to the bus end ($(wc -l < "$("$PY" -c 'import sys; from coordkit.manifest import load; print(load(sys.argv[1]).paths.bus_log)' "$M")" | tr -d ' ') rows), flip mode to live (atomic), coordkit up --apply"
  TMP=$(mktemp -d); sed 's/^mode = "shadow"/mode = "live"/' "$M" > "$TMP/coordkit.toml"
  "$PY" -m coordkit.cli --manifest "$TMP/coordkit.toml" up --plan >"$RUN/up-plan.txt" 2>&1; rc=$?; rm -rf "$TMP"
  "$PY" - "$RUN/up-plan.txt" <<'PY' || { say "DRY_RUN_FAILED plan has unexpected differences (see $RUN/up-plan.txt)"; exit 2; }
import re, sys
ok = re.compile(r'^UP_DIFF (missing job com\.example\.omi\.(loop\.audit|reconcile)|unexpected job com\.example\.omi\.'
                r'(shadow|obligations)|question hooks to install in ' + re.escape(str(__import__('pathlib').Path.home())) + r'/\.claude(-1|-2)?/settings\.json)$')
lines = open(sys.argv[1]).read().splitlines()
bad = [l for l in lines if l.startswith('UP_DIFF') and not ok.match(l)]
sys.exit(1 if bad or 'UP_PLAN_ONLY' not in lines else 0)
PY
  doctor_ok "$RUN/up-plan.txt" yes "$rc" >>"$LOG" 2>&1 || { say "DRY_RUN_FAILED plan's doctor is incomplete, unexpectedly red, or rc=$rc disagrees"; exit 2; }
  say "PLAN rc=$rc ok (expected pre-cutover drift only; doctor complete)"
  say "WOULD VERIFY doctor complete+green; jobs ${LIVE_JOBS[*]} loaded; kick ${KICK_JOBS[*]} once each (exit 0, or 5 = findings with completion proof and no new traceback); question hooks in ${SETTINGS[*]}; hook probe exit 0 + no decision"
  say "DRY_RUN_OK: nothing changed (log in $RUN). Run with --apply to act."
  exit 0
fi

# -- apply ----------------------------------------------------------------------------------------------
{ cp "$M" "$RUN/manifest.before" && cmp -s "$M" "$RUN/manifest.before"; } || fail_pre "manifest snapshot failed"
for f in "${SETTINGS[@]}"; do { cp "$f" "$(snap "$f")" && cmp -s "$f" "$(snap "$f")"; } || fail_pre "settings snapshot failed: $f"; done
touch "$RUN/started" || fail_pre "cannot write the run marker"
for j in "${INCUMBENT[@]}"; do
  if loaded "$j"; then
    # Record where each job was loaded from and its exact definition; rollback resumes only that.
    path=$(launchctl print gui/$U/$j 2>/dev/null | awk -F'= ' '/^[[:space:]]*path = /{print $2; exit}')
    { [ -n "$path" ] && [ -f "$path" ] && cp "$path" "$RUN/plist.$j" && printf '%s' "$path" > "$RUN/plist-path.$j"; } \
      || fail_pre "cannot record the plist of $j"
    ORIGINAL="$ORIGINAL $j"
  fi
done
PHASE=mutating; record || fail_pre "cannot journal the run state"
trap 'rollback "error exit"' EXIT
trap 'rollback "signal"' INT TERM HUP

{ touch "$ROOT/STOP" "$ROOT/STOP-sweep" && [ -e "$ROOT/STOP" ] && [ -e "$ROOT/STOP-sweep" ]; } || rollback "STOP markers not written"
for j in "${INCUMBENT[@]}"; do
  case " $ORIGINAL " in
    *" $j "*) say "PAUSE $j"; launchctl bootout gui/$U/$j >>"$LOG" 2>&1; if loaded "$j"; then rollback "could not pause $j"; fi ;;
    *) say "SKIP $j (was not loaded; never resumed)" ;;
  esac
done
# Project forward only: seed the mirror cursor to the shared bus's current end.
# Keep the previous value in the run directory for audit and rollback.
BUS_LOG=$("$PY" -c 'import sys; from coordkit.manifest import load; print(load(sys.argv[1]).paths.bus_log)' "$M") || rollback "cannot resolve the bus log"
mkdir -p "$ROOT/state" && { cat "$ROOT/state/mirror.cursor" 2>/dev/null || echo none; } > "$RUN/mirror.cursor.before" \
  && rows=$(wc -l < "$BUS_LOG" | tr -d ' ') && printf '%s\n' "$rows" > "$ROOT/state/mirror.cursor.tmp" \
  && mv "$ROOT/state/mirror.cursor.tmp" "$ROOT/state/mirror.cursor" && say "SEEDED mirror.cursor=$rows (was $(cat "$RUN/mirror.cursor.before"))" \
  || rollback "could not seed the mirror cursor"
# A reconcile state left by a rolled-back live pass is stale (its stamp makes the doctor's reconciler_age red, and
# its obligations_since predates this run): move it into the run dir so the first live pass starts fresh.
if [ -e "$ROOT/reconcile/state.json" ]; then
  mv "$ROOT/reconcile/state.json" "$RUN/reconcile-state.before" && say "ARCHIVED reconcile/state.json -> $RUN/reconcile-state.before" \
    || rollback "could not archive the stale reconcile state"
fi
{ write_mode live >>"$LOG" 2>&1 && grep -q '^mode = "live"' "$M"; } || rollback "mode flip failed"
say "RUN coordkit up --apply"
ck up --apply >"$RUN/up-apply.txt" 2>&1 || rollback "up --apply failed (see up-apply.txt)"
grep -q '^UP_OK' "$RUN/up-apply.txt" || rollback "up --apply did not print UP_OK"

# -- verify ---------------------------------------------------------------------------------------------
ck doctor >"$RUN/doctor-post.txt" 2>&1; prc=$?
doctor_ok "$RUN/doctor-post.txt" no "$prc" >>"$LOG" 2>&1 || rollback "doctor incomplete, red or rc=$prc after apply (see doctor-post.txt)"
for j in "${LIVE_JOBS[@]}"; do loaded "$j" || rollback "$j not loaded"; done
# Verify one bounded execution of each job. Exit 5 means findings, so accept it
# only with proof the run completed (reconcile: reconcile/state.json last_completed at or after the kick; sweep: a
# `SWEEP findings=` summary in the stdout it wrote since the kick) and no traceback in the stderr since the kick.
for j in "${KICK_JOBS[@]}"; do
  runs() { launchctl print gui/$U/$1 2>/dev/null | awk -F'= ' '/^[[:space:]]*runs = /{print $2; exit}'; }
  size() { [ -f "$1" ] && wc -c < "$1" | tr -d ' ' || echo 0; }
  since() { tail -c +$(( $2 + 1 )) "$1" 2>/dev/null; }
  dir="$ROOT/${j##*.}"; errsize=$(size "$dir/stderr.log"); outsize=$(size "$dir/stdout.log")
  before=$(runs "$j"); before=${before:-0}; kicked=$(date +%s)
  launchctl kickstart -k gui/$U/$j >>"$LOG" 2>&1 || rollback "kickstart $j failed"
  ok=""; code=""; deadline=$(( $(date +%s) + 180 )); sleep 3
  while [ "$(date +%s)" -lt $deadline ]; do
    info=$(launchctl print gui/$U/$j 2>/dev/null); now=$(runs "$j"); now=${now:-0}
    if [ "$now" -gt "$before" ] && ! echo "$info" | grep -q 'state = running'; then
      code=$(echo "$info" | awk -F'= ' '/^[[:space:]]*last exit code = /{print $2; exit}')
      if [ "$code" = 0 ]; then ok=yes
      elif [ "$code" = 5 ] && ! since "$dir/stderr.log" "$errsize" | grep -q Traceback; then
        case "$j" in
          *.reconcile) "$PY" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("last_completed",0) >= float(sys.argv[2]) else 1)' \
                         "$dir/state.json" "$kicked" 2>>"$LOG" && ok=yes ;;
          *.sweep) since "$dir/stdout.log" "$outsize" | grep -q '^SWEEP findings=' && ok=yes ;;
        esac
      fi
      break
    fi
    sleep 3
  done
  [ -n "$ok" ] || rollback "$j did not complete a clean run within 180s (last exit code ${code:-none})"
  say "VERIFIED $j ran and exited $code"
  if [ "$code" = 5 ]; then say "FINDINGS $j: $(since "$dir/stdout.log" "$outsize" | grep -E 'would_fix|^SWEEP|ESCALAT|ASK' | head -5 | tr '\n' ' ')"; fi
done
for f in "${SETTINGS[@]}"; do
  "$PY" - "$f" <<'PY' || rollback "question hooks missing in $f"
import json, sys
doc = json.load(open(sys.argv[1])).get('hooks', {})
has = lambda event, verb, matcher=None: any(g.get('matcher') == matcher and any(f' question hook {verb} ' in h.get('command', '') + ' '
                                            for h in g.get('hooks', [])) for g in doc.get(event, []))
sys.exit(0 if has('PreToolUse', 'pretooluse', 'AskUserQuestion') and has('Stop', 'stop') and has('Notification', 'notification') else 1)
PY
done
out=$(echo '{"tool_name":"Bash","tool_input":{}}' | "$PY" -m coordkit.cli --manifest "$M" question hook pretooluse --config-dir $HOME/.claude-1); prc=$?
{ [ $prc = 0 ] && [ -z "$out" ]; } || rollback "hook probe rc=$prc or printed a decision for a non-question tool call"
PHASE=done
marked=0; touch "$RUN/done" 2>>"$LOG" && marked=1; record && marked=1
if [ $marked = 0 ]; then
  trap - EXIT INT TERM HUP; release_lock
  say "CUTOVER_APPLIED_UNRECORDED: the cutover is applied and verified, but the done journal could not be written. Do NOT --rollback $RUN; record it by hand (touch $RUN/done) and continue with CUTOVER sections 5-6."
  exit 4
fi
trap - EXIT INT TERM HUP; release_lock
say "CUTOVER_APPLIED: ${LIVE_JOBS[*]} loaded, sweep+reconcile ran clean, question hooks in ${SETTINGS[*]}, doctor green. Paused:${ORIGINAL}. Next: bus-maintainer runs CUTOVER sections 5-6."
exit 0
