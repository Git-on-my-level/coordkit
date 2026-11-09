"""State review checklist. Add a lifecycle row when introducing a marker or state field.

The scanner is deliberately small: literal file/cursor/lock paths, persisted state keys,
lane phase values, and writer functions. Dynamic filenames belong to the writer's row.
It is a review fence, not a proof that arbitrary Python cannot hide state.
"""
import ast
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]/'src/coordkit'


def markers(source):
    tree = ast.parse(source)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for part in ast.walk(node.right):
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    found.add(part.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'with_suffix':
            for arg in node.args:
                found.update(part.value for part in ast.walk(arg) if isinstance(part, ast.Constant) and isinstance(part.value,str))
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and isinstance(node.slice, ast.Constant):
            root = ast.unparse(node.value)
            if root in ('state','state.data','plan') or root.startswith('state['):
                found.add(root+':'+str(node.slice.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'setdefault' and node.args and isinstance(node.args[0], ast.Constant):
            root = ast.unparse(node.func.value)
            if root in ('state','state.data','plan'):
                found.add(root+':'+str(node.args[0].value))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(isinstance(n,ast.Call) and ((isinstance(n.func,ast.Attribute) and n.func.attr in ('write_text','write_bytes','unlink','touch'))
                   or (isinstance(n.func,ast.Name) and n.func.id == 'write_json')) for n in ast.walk(node)):
                found.add('writer:'+node.name)
        if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple) and ast.unparse(node.target) == '(name, empty)' and isinstance(node.iter, (ast.Tuple, ast.List)):
            for pair in node.iter.elts:
                if isinstance(pair, ast.Tuple) and pair.elts and isinstance(pair.elts[0], ast.Constant):
                    found.add('state.data:'+str(pair.elts[0].value))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and ast.unparse(target) == "plan['phase']" and isinstance(node.value,ast.Constant):
                    found.add('phase:'+node.value.value)
    return found


# module, markers, enter, normal exit, max age/backstop, escalation
REGISTRY = []

def row(module, names, enter, exit, max_age, escalation):
    REGISTRY.extend((module,name,enter,exit,max_age,escalation) for name in names.split('|'))

row('manifest.py', 'lead.json', 'lead adopt', 'next adoption', 'persistent identity, no blocking latch', 'doctor identity check')
row('actl.py', 'request.json|writer:delegate', 'delegate request', 'execution receipt', 'bounded tool timeout', 'loop failures or lane alarm')
row('extract.py', '.tmp|writer:commit', 'transcript extraction', 'atomic checkpoint replacement', 'one cycle max_age', 'loop failure ask')
row('bus.py', 'bus.lock|harvest.lock|.lock|.|.tmp|writer:append|writer:project_pending|writer:dispatch', 'bus command / atomic cursor update', 'kernel releases lock / rename', 'bounded caller invocation; kernel on death', 'caller failure and reconciliation mirror ask')
row('bus.py', 'spill_ref|origin=mcp', 'bus spill or connector-originated post',
    'immutable row retained with bus history; archive retention owns spill files',
    'no latch; ref roots bound reads and activity TTL bounds local evidence',
    'text never grants ref access; service origin never proves a local listener')
row('mcp_server.py', 'state|mcp|calls.ndjson', 'every MCP request', 'append-only audit journal of connector calls', 'reviewed with the connector; rotation documented in docs/MCP.md', 'journal is the record; refusals counted per outcome')
row('mcp_server.py', 'writes-enabled', 'the guarded go-live turns writes on', 'operator removes the file (writes off)', 'until removed; the kill file overrides it', 'write tools refuse while absent')
row('mcp_server.py', '.ndjson.1', 'the journal passes JOURNAL_MAX', 'the next rotation replaces it', 'at most two journal files', 'none needed: bounded audit')
row('mcp_server.py', 'state:preauth|state:work|state:timer', 'a connection is accepted (in memory, per connection)', 'released when the handler thread ends', 'HEADER_DEADLINE, then REQUEST_DEADLINE (a timer closes the socket)', 'over-capacity connections are closed at once')
row('mcp_server.py', 'kill', 'an operator disables the connector', 'operator removes the file', 'until removed (explicit kill switch)', 'every request answers 503 while present')
row('mcp_server.py', 'cursors|aliases.json', 'observe existing bus CLI state for read receipts and routing', 'CLI replaces cursors/address book; MCP never writes them', 'snapshot per request, no persisted cache', 'missing/malformed cursors give no receipt; aliases fall back to manifest')
row('addressed.py', 'lead-digest.ndjson|state:lead_digest_at|writer:flush_lead_digest', 'an escalation or machine finding that would wake the lead (lead_escalations=false)', 'flushed into one fyi to the lead, queue truncated under its writer lock', 'one hour (DIGEST_EVERY)', 'the hourly digest itself')
row('addressed.py', 'state:dead_owner_ids', 'obligations whose owner is not listening at the hourly digest', 'replaced by each digest; an unchanged set downgrades the digest from ask to fyi', 'one hour (DIGEST_EVERY)', 'the digest RE-HOME line, as an ask to the lead')
row('bus.py', 'mirror-dead-letters.jsonl', 'a mirrored row whose card definitely does not exist', 'append-only audit; reconcile counts new entries', 'next reconcile interval', 'internal mirror ask (escalation.internal_to)')
row('reconcile.py', 'state/mirror-dead-letters.jsonl|state:mirror_dead_seen', 'reconcile reports new mirror dead letters', 'advances to the dead-letter count', 'one reconcile interval', 'internal mirror ask')
row('bus.py', 'harvest.cursor|mirror.cursor', 'post or harvest', 'cursor advances after projection/harvest', 'reconciler interval; limits stock threshold', 'mirror failure or stock ask to lead')
row('stock.py', 'harvest.cursor|mirror.cursor', 'read existing cursor', 'read-only observation', 'limits per kind', 'stock threshold lead ask')
row('wait.py', 'state.json|.tmp|.lock|writer:save', 'wait cursor transaction', 'atomic state replacement / kernel unlock', 'wait timeout_minutes; bounded seen caches', 'wait output; alarms owned by reconciler')
row('wait.py', 'waiter.lock|writer:cmd_wait', 'wait arms and writes pid', 'finally clears pid and kernel lock', 'timeout_minutes', 'already-armed receipt or stale pid diagnosis')
row('wait.py', 'lead-session|writer:cmd_register', 'register-lead', 'next registration overwrites', 'persistent hint, no blocking latch', 'lead.json is identity authority')
row('wait.py', 'state.data:deadlines', 'arm-deadline', 'fire and remove under three-way merge', 'explicit deadline at', 'lead wake output')
row('wait.py', 'state.data:stale|state.data:stale_times', 'stale execution observed', 'terminal record removes in-flight entry', 'wait.stale_alert_after; 500 entry cap', 'repeat stale wake; lane recovery ask')
row('wait.py', 'state.data:missing_sources|state.data:identities', 'missing or replaced source', 'source returns / identity changes', 'wait timeout_minutes', 'source error / attention wake')
row('wait.py', 'state.data:anx_seen|state.data:bus_line|state.data:bus_near|state.data:bus_seen|state.data:config|state.data:muted', 'consume source event', 'source changes / bounded cache rollover', '1000 ANX / 2000 event cap; repeat window', 'lead waiter wake; reconciler owns obligation age')
row('lifecycle.py', '.plist|lifecycle.json|state:jobs|state:hook_backups|writer:up|writer:down', 'up apply records before bootstrap', 'down enumerates installation and restores hooks', 'persistent installation until explicit down', 'doctor drift and reconciler lead ask')
row('lifecycle.py', 'writer:initialize', 'init', 'validated manifest and resource receipt', 'bounded tool timeout', 'command failure')
row('lifecycle.py', 'dead-letter-baseline.json|writer:record_dead_letter_baseline', 'explicit baseline recording', 'immutable baseline; never automatically replaced', 'persistent audit evidence, no latch', 'new dead letters fail doctor')
row('lifecycle.py', 'lead.json|writer:adopt', 'lead adopt', 'next adoption', 'persistent identity, no blocking latch', 'doctor')
row('lifecycle.py', 'reconcile/state.json', 'read last-run receipt', 'new successful receipt', 'two reconciler intervals', 'doctor red')
row('loop.py', 'STOP|writer:stop', 'loop stop with reason', 'explicit human removal after reactivation', 'explicit disposition; card resolved', 'stopped card excluded from cadence asks')
row('loop.py', 'cycle.lock|writer:run_cycle', 'cycle begins with pid/start/deadline', 'finally truncate metadata; kernel unlock', 'loop.max_age', 'failure ask; skipped run receipt counts')
row('loop.py', 'result.lock|fails|skip.json|writer:record_failure', 'failed, unknown or skipped cycle', 'successful cycle resets counter', 'fail_report_after cycles', 'durable escalation ask')
row('loop.py', 'escalation.json|writer:retry_escalation', 'failure threshold', 'successful cycle after confirmed send clears', 'retry next cycle; loop.max_age per run', 'ANX ask then configured page')
row('loop.py', 'pending.json|writer:_cycle', 'extract completed before publication', 'receipt/checkpoint complete then unlink', 'next bounded cycle; loop.max_age', 'failure threshold ask')
row('loop.py', 'offsets|.json|plan.json|req', 'auditor request / transcript checkpoint', 'commit checkpoint on success; archive evidence', 'one cycle max_age; archive retained', 'cycle failure ask')
row('loop.py', '.plist|writer:install', 'stage plist', 'up apply or down', 'persistent staged configuration, no latch', 'doctor drift')
row('shadow.py', 'diffs.jsonl|state.json|run.lock|writer:single_pass', 'shadow sample', 'append journal then replace projection / unlock', 'max_gap_seconds=600; 24h freshness gate', 'unclean resets streak; report exit 5')
row('lane.py', '.watch.lock|plan:watcher_pid|writer:watch', 'watcher takes ownership', 'kernel unlock on exit', 'lane.max_run_hours; reconciliation every interval', 'recover restores watcher or asks')
row('lane.py', '.plan.json|plan:phase|plan:execution_id|phase:launching|phase:running|writer:launch|writer:launch_run|writer:main|writer:compensate_launch', 'launch/adopt prepares durable plan then acquires lease', 'terminal finish or proven launch absence compensation', 'lane.max_run_hours', 'plan/cause ANX ask')
row('lane.py', 'plan:relaunches|plan:autocontinued|writer:_watch', 'capacity failure or unread obligations', 'bounded relaunches or one continuation', 'capacity_relaunch_max / autocontinue_hours / max_run_hours', 'capacity exhaustion ask')
row('lane.py', 'plan:completion|plan:terminal|phase:completing|phase:released|writer:finish|writer:_recover', 'proven terminal execution', 'lease handoff then terminal event and plan', 'next reconciler pass / lane.max_run_hours', 'recovery failure asks; replay dedupes terminal')
row('lane.py', 'phase:deadline_exceeded|writer:expire', 'run deadline exceeded', 'confirm cancellation then finish; otherwise retain ownership', 'renewal stops at max_run_hours; lease_ttl backstop', 'deadline ask then cancellation recovery')
row('lane.py', 'plan:alarms|writer:alarm', 'unsafe recovery/capacity/deadline', 'terminal plan archived', 'one ask per plan and cause', 'ANX ask and optional pager')
row('reconcile.py', 'reconcile/state.json|state.json|state:last_run|state:last_completed|writer:main|writer:reconcile', 'run starts', 'last_completed receipt', 'two intervals doctor deadline', 'doctor red; per-check ANX asks')
row('stock.py', 'reconcile/state.json', 'read obligations_since (adoption baseline)', 'set once by the first live reconcile; archived by a cutover apply', 'persistent baseline, no latch', 'stock counts only adopted rows; reconcile asks on thresholds')
row('reconcile.py', 'run.lock|skip.json', 'reconciler invocation', 'kernel unlock; next run replaces skip receipt', 'nonblocking lock; two interval health limit', 'skip receipt and doctor red')
row('reconcile.py', 'state:fired|writer:action', 'successful action receipt', 'age GC or normal recovery clears alarm key', 'reconciler.state_max_age', 're-ask unresolved obligation after retention window')
row('reconcile.py', 'state:dead', 'known owner absent', 'release clears key; live owner clears; age GC', 'stale_lease_grace / state_max_age', 'lead lease handoff with note')
row('deadletters.py', 'dead-owners.json|state|dead-letter-baseline.json|writer:ours', 'definite agentctl owner verdict for a listed post-baseline dead letter', 'pruned when the letter leaves the spool', 'bounded by the capped spool size', 'unknown owner is never cached and counts as ours')
row('reconcile.py', 'state:loops|STOP', 'past promise', 'receipt or explicit stop clears', 'N times per-loop default_cadence', 'wake once then lead ask')
row('reconcile.py', 'state:blockers', 'blocker exceeds blocker_ask_after', 'decision/answer --re clears', 'blocker_ask_after plus fail_report_after polls', 'owner wake then lead ask')
row('reconcile.py', 'state:obligations_since', 'first reconcile run (adoption baseline)', 'never cleared; older incumbent backlog is triaged by hand', 'obligations older than the baseline or 48h are not reported', 'coordkit obligations report lists the backlog read-only')
row('reconcile.py', 'state:holds', 'hold exceeds merge.max_hold_age', 'unhold or explicit lead release', 'merge.max_hold_age plus fail_report_after polls', 'poster wake then lead ask with unhold recommendation')
row('reconcile.py', 'answers.json|reconcile/answers.json', 'answer collected before projection', 'unique correlation or abandon-answer --reason', 'reconciler.answer_max_age', 'lead ask contains retained payload')
row('reconcile.py', 'state/bus.lock|state/mirror.cursor|state:mirror_failures', 'lagged bus projection', 'successful replay resets failures and advances cursor', 'fail_report_after reconciliations', 'lead ask then configured page')

# Logical states whose storage is dynamic, remote, computed or an append-only row.
LOGICAL = [
    ('bus.inbox/wait cursors', 'cursor read', 'advance on consumption', 'wait timeout / inbox stock limits', 'caller timeout / stock ask'),
    ('bus.open ruling', 'ruling post', 'owner ack', 'ruling_ask_after', 'lead ask'),
    ('bus.open blocker', 'blocker post', 'decision/answer --re with reason', 'blocker_ask_after', 'owner wake then lead ask'),
    ('bus.hold card', 'structured hold', 'poster unhold or explicit lead release', 'merge.max_hold_age', 'poster wake then lead ask'),
    ('bus.FYI/state latches', 'identical repeat', 'text/state changes', '6h computed window; no marker stored', 'sender deduped receipt'),
    ('bus.open asks', 'ask post', 'ack/answer/decision', 'limits.asks stock threshold', 'lead ask'),
    ('bus.signal/latest state', 'state/signal post', 'superseding post', 'caller wait timeout; log retained', 'caller timeout'),
    ('lane.card lease', 'launch acquires', 'finish/compensation handoff', 'lease_ttl with max_run_hours renewal bound', 'deadline/recovery ask'),
    ('lane.event subscription', 'launch subscribes', 'agentctl terminal delivery / expiry', '72h debug subscription TTL', 'await remains primary; watcher recovery'),
    ('lane.prepared/terminal phases', 'launch prepare', 'launching or terminal archive', 'max_run_hours; terminal has no latch', 'recover ask'),
    ('wait.held tier events', 'deferred event arrives', 'settle/debounce/ride_max flush', 'ride_max or wait timeout', 'lead batch wake'),
    ('wait.seen/offsets/CI/fired', 'source consumed or deadline fired', 'new identity/verdict or bounded cache rollover', 'bounded caches / per-wait timeout', 'wake on new event'),
    ('wait.pidfile', 'waiter arms', 'finally unlinks own PID', 'wait timeout; stale PID detected', 'NOT ARMED diagnostic'),
    ('lifecycle.hook backups', 'cutover records backup', 'down reverses recorded managed hooks and preserves unrelated edits', 'explicit rollback, no automatic overwrite', 'doctor/reconciler installation drift'),
    ('shadow.streak', 'clean sample', 'gap/scope/backwards/unclean resets', 'max_gap_seconds plus 24h gate', 'report fails closed'),
]

row('manifest.py', '.local/bin|coordkit.toml', 'manifest discovery/configuration', 'explicit configuration replacement', 'persistent configuration, no latch', 'manifest validation / doctor')
row('wait.py', 'state.data:ci|state.data:fired|state.data:offsets|state.data:seen', 'consume source or deadline', 'source identity/verdict change / bounded cache rollover', '2000 seen / 500 fired cap; wait timeout', 'lead wake; obligations escalated separately')
row('wait.py', 'state.data:held', 'tiered event arrives', 'settle/debounce/ride_max flush', 'ride_max / wait timeout', 'lead batch wake')
row('wait.py', 'wait|waiter.pid', 'waiter arms', 'finally removes own pid / process unlock', 'wait timeout; stale pid diagnosed', 'NOT ARMED / already armed receipt')
row('stock.py', 'state', 'read bus cursors', 'read-only observation', 'stock thresholds', 'lead stock ask')
row('bus.py', '%m%d|-|.txt|cursors|refs|state', 'bus post spills text or reader advances cursor', 'consume cursor; append-only refs retained', 'reader timeout / stock thresholds; refs are archive', 'mirror/stock lead ask')
row('lifecycle.py', '.local/bin|Label|launchd|reconcile/stderr.log|reconcile/stdout.log', 'stage/apply manifest templates', 'down enumerates installed jobs', 'persistent installation until explicit rollback; logs archive', 'doctor/reconciler drift ask')
row('loop.py', 'Label|deadline-|err.txt|extract.txt|findings.md|launchd|loops|prior.md|prompt.md|raw.txt|runs|snapshot.txt|stderr.log|stdout.log', 'cycle creates evidence / stages configuration', 'cycle receipt or explicit down; immutable evidence retained', 'loop.max_age for live work; archive has no latch', 'failure threshold asks; doctor drift')
row('shadow.py', 'shadow', 'isolated shadow sample', 'journaled sample completion', 'max_gap_seconds; report freshness', 'report fails closed')
row('lane.py', '.md|.out|.watch.log|label|lane-prompts|req', 'launch/adopt records prompt and run evidence', 'terminal plan / compensation', 'lane.max_run_hours for live work; evidence retained', 'plan/cause ask')
row('reconcile.py', 'loops|reconcile|req', 'read plan/loop states and record reconciliation', 'completed run / disposition', 'per-obligation age limits; two interval doctor limit', 'lead ask and optional page')

row('hooks.py', 'hook-rollback.json|writer:install|writer:restore',
    'record before/after hook documents before cutover edit',
    'down reverses managed entries and verifies before marking restored',
    'persistent independent recovery receipt; explicit rollback',
    'doctor/reconciler drift; rollback conflict fails loudly')
row('lifecycle.py', 'loops|STOP', 'read configured loop stop marker',
    'read-only comparison to card resolution', 'explicit stop or reactivation',
    'doctor red and reconciler drift ask')

# Addressed bus (briefs/addressed-bus.md section 8)
row('addressed.py', 'addressed|state|leases|.json|.lock|writer:renew', 'question answer adapter records delivery confirmation', 'confirmation expires or an agent replies', 'lease_ttl without renewal, rearm_grace after a clean exit; SIGKILL leaves the heartbeat to go stale', 'send and sweep escalate recipient_deaf to the fallback and relay file')
row('addressed.py', 'register.json', 'first local post/wait/ack', 'explicit retire or manual removal', 'persistent roster entry, no blocking latch', 'deaf sweep as for any registered name')
row('addressed.py', 'sweep.json|sweep.lock|state:seen|writer:sweep|writer:_sweep', 'first sweep (adoption baseline) / a finding is raised', 'pickup keys retained while open; other findings expire after 2x realert_after', 'one direct pickup per owner and clock', 'hourly digest; local owner wake')
row('reconcile.py', 'state:obligation_seq', 'first alert on an obligation ladder', 'dropped when the obligation closes (no longer reported open)', 'bounded by open obligations', 'alerts keep unique ids after receipt pruning')
row('lifecycle.py', 'sweep/stdout.log|sweep/stderr.log', 'up installs the addressed sweep job', 'launchd appends per run; down removes the job', 'log rotation is the host newsyslog policy', 'sweep findings reach the relay file and fallback rows')
row('addressed.py', 'state:deaf_since', 'a registered name is first seen not listening', 'cleared when the name is listening again', 'one entry per registered name', 'normal messages escalate after rearm_grace of deafness, urgent at once')
row('review.py', 'review-scan.json|writer:main', 'review scan --relay raises a finding', 'keys for reviews that ended (or aged past 48h) are dropped each scan', 'bounded by open unrecorded reviews', 'one relay-file line per unrecorded review')

# Question escalation (briefs/question-escalation.md)
row('questions.py', 'state|questions|questions.json|questions.lock|qid|writer:save_all|writer:main', 'a hook, the sweep or a CLI call captures a question', 'answered: closed and delivered; closed records pruned after 7 days', 'tier deadlines (15m urgent / 30m normal), then anx', 'tier 1 live agent, tier 2 anx ask; status line counts open > 30 min')
row('questions.py', 'sessions.json|sessions.lock', 'coordkit question bind', 're-bind overwrites', 'persistent binding, no latch', 'unknown askers route to the relay')
row('questions.py', 'notifications|.json|writer:hook_notification|writer:scan_notifications', 'Notification hook (waiting for input or permission)', 'removed when the transcript grows or the sweep turns it into a question', 'notification_idle', 'question ladder')
row('questions.py', 'answers|.md|writer:deliver', 'answer to a deaf asker via delivery_cli', 'pointer file per question, read once by the asker', 'bounded by answered questions', 'relay file when no adapter confirms')
row('questions.py', '*|*.jsonl|projects', 'sweep reads harness transcripts (read-only)', 'read-only', 'transcript_window_hours', 'pending modals become questions')
row('hooks.py', 'hook-questions-|.json|settings.json|lifecycle.json|writer:install_questions|writer:restore_questions', 'cutover installs question hooks per config dir (receipt first)', 'down restores each recorded config dir via reverse()', 'persistent until down', 'doctor/up --plan/reconcile report missing question hooks')
row('deadletters.py', 'dead-letter-dispositions.json|writer:disposition|writer:reopen', 'doctor --disposition-dead-letter EXEC --reason TEXT (live, attributed letters only)', 'doctor --reopen-dead-letter EXEC removes it; a changed letter is visible again by fingerprint', 'persistent audit record, listed by doctor (dispositioned=N)', 'doctor dead_letters check and stock count skip only dispositioned fingerprints')
row('lifecycle.py', 'hook-backups|settings.json|.json|-', 'up --apply copies each managed settings.json before installing question hooks', 'kept as rollback evidence beside the receipt', 'one file per install', 'down reverses from the receipt; the backup is the audit copy')
row('deadletters.py', 'req', 'disposition reads lane plan keys (read-only)', 'read-only', 'plans are owned by lane.py', 'an unissued request key refuses the disposition')
row('usage.py', 'projects|.jsonl', 'doctor reads scoped external transcripts',
    'read-only; no cursor, cache, or receipt', 'last-hour observation; source retention belongs to the harness',
    'missing or ambiguous data is visible; model drift is a warning')

# Hook delivery is opt-in and never acknowledges or publishes bus rows.
row('deliver.py', 'state|cursors|.hook|.hook.lock|writer:deliver',
    'a known session invokes PostToolUse or UserPromptSubmit',
    'flush delivered context then atomically advance byte offset; nonblocking kernel lock releases',
    '256 KiB read and 1536 byte output per invocation; retained cursor has no waiting process',
    'errors fail open without delivery; listening evidence expires and existing deaf checks resume')
row('deliver.py', 'hooks|.json|.pending',
    'a known session successfully reads the bus via its hook',
    'stage bytes before output, publish note after successful flush, remove staging file on exit',
    'lease_ttl for hook evidence; busy_window for the exact transcript mtime',
    'expired evidence returns to the existing addressed listening/deaf behavior')
row('deliver.py', 'hook-delivery-|writer:install|writer:uninstall',
    'explicit deliver install writes before/after receipt before settings',
    'explicit uninstall reverses only managed hook entries and marks receipt restored',
    'persistent rollback evidence until explicit uninstall; no automatic installation',
    'rollback conflict refuses to overwrite changed managed entries')
row('addressed.py', 'hooks|sessions.json|projects|*/|.jsonl',
    'listening reads a hook note or an explicitly bound session transcript',
    'read-only evidence; next hook or session binding replaces source state',
    'lease_ttl for hook evidence and addressed.busy_window for transcript mtime',
    'stale or unknown bindings preserve existing lease/probe behavior')


# Noise-cut receipts share existing state files; they never require a service or live manifest change.
row('addressed.py', 'unconfirmed_at', 'sidecar delivery fails confirmation',
    'ignored after confirm_timeout; newest 50 ids/timestamps carried across re-arm',
    'addressed.confirm_timeout; legacy ids use bus timestamp or original lease start',
    'normal listening evidence applies again; timeout never creates liveness')
row('addressed.py', 'unacked once keys', 'deadline or four-hour UNACKED alert succeeds',
    'retained through addressed.window_hours, then age GC', 'at most two UNACKED alerts per row; human one',
    'unregistered and exhausted rows appear only in hourly lead backlog digest')
row('reconcile.py', 'hold-text fired pairs', 'ignored free-text row/PR pair first classified',
    'permanent warning receipt in fired; no behavioral latch', 'one per row/PR in the bus history',
    'structured holds continue to count normally')
row('loop.py', 'compared_prior', 'pending findings compared to prior.md before publication',
    'pending.json removed after bus summary, receipt and prior append succeed', 'loop.max_age per retry',
    'failed cycles retry the same publication id and frozen findings')

row('addressed.py', 'reconcile/state.json', 'combined sweep reads remote cadence', 'reconcile updates last_run', 'reconciler.interval_seconds', 'local sweep runs before remote calls')

row('bus.py', 'aliases.json', 'read shared transport aliases', 'reader takes the current address book', 'no persisted cache', 'invalid alias input fails the command')

# Import directories and archive roots are read-only configuration, not blocking latches.
row('paths.py', 'src|coordkit|current', 'package import or explicit home', 'process exit', 'read-only install lookup', 'operator repairs missing installation')
row('manifest.py', 'state/refs', 'manifest ref-root default', 'manifest replacement', 'read-only archive boundary', 'CLI and MCP refuse files outside roots')

row('tail.py', 'writer:read', 'read complete rows', 'checkpoint atomically replaced', 'hint only; rebuilt on replacement or truncation', 'read error to caller')
row('bus.py', 'append.offset.json|tail.offset.json', 'append or incremental read', 'count and byte boundary checkpoint', 'invalid hint falls back to scan', 'malformed lines reported without moving physical positions')
row('hooks.py', 'writer:reconcile|.claude/settings.json', 'explicit hooks install or update', 'receipt roll-forward or conflict', 'pending_after recovered on retry', 'hook_rollback_conflict preserves user edits')
row('services.py', 'writer:install|writer:uninstall|launchd|.plist|.service|.timer|systemd/user|.config', 'explicit lifecycle apply', 'down disables timers and removes owned units', 'persistent until uninstall', 'doctor compares disk and loaded configuration')
row('selfupdate.py', 'update.lock|releases|writer:shim|coordkit/cli.py|update-policy.json|coordkit|.current-|current/bin/coordkit|update-state.json|writer:check|bin|.json|bin/coordkit|install-manifest.json|rollback|writer:register|writer:swap|lifecycle.json|manifests.json|writer:apply|current', 'explicit update or manifest registration', 'atomic swap or restore prior pointer', 'network 10s; smoke 30s; failed staging removed', 'update fails with rollback receipt; user conflicts preserved')
row('wake.py', '.json|.wait.lock|state|attempts.jsonl|lanes.json|state/cursors|state/wakes|.lock|writer:change|writer:notify', 'addressed post or retire command', 'attempt result recorded or explicit unretire', 'wake_debounce_minutes; pending attempts hold window', 'failed or unavailable wake remains inspectable in bus state')
row('sessions.py', 'sessions', 'Claude Code writes <config>/sessions/<pid>.json per running session (read only here)', 'Claude Code removes it at exit; dead pids are skipped', 'read per command; no cache', 'ambiguous or missing lane is refused, never guessed')
row('peerwake.py', 'state/wakes/peer|helper.json|.json|pending|claimed|done|results.jsonl|writer:beat|writer:claim|writer:ack|writer:wake', 'held lane wake with a live helper in its profile', 'helper acks sent or failed; request moves pending -> claimed -> done', 'helper heartbeat fresh 15 min; one request per wake key', 'unclaimed requests stay in pending/ and results.jsonl keeps every ack')
row('wake.py', 'state/wakes/scan.json|writer:scan', 'scan of rows written outside bus.append', 'checkpoint advances past complete rows; replaced or truncated log restarts at end', 'one scan interval', 'missed rows are only those written before the first scan')

row('mcp_install.py', 'mcp-service.json|mcp/stdout.log|mcp/stderr.log|writer:main', 'explicit mcp install', 'mcp uninstall or lifecycle down', 'persistent service; token remains private until operator removes it', 'secret validation and manager failure')
row('lifecycle.py', 'mcp-service.json', 'MCP installation receipt', 'explicit uninstall or down', 'persistent managed service', 'doctor loaded-job drift')
row('selfupdate.py', 'mcp-service.json', 'registered managed MCP service', 'explicit uninstall', 'update only reconciles owned jobs', 'service manager failure restores active release pointer')
row('identity.py', 'state|questions|sessions.json', 'explicit session binding lookup', 'read-only snapshot', 'bounded transcript prefix for delivery', 'unknown identity produces no delivery')
row('routing.py', 'state|addressed|register.json|register.lock|hooks|sessions.json|projects|.json|.jsonl|writer:store_identity', 'first local post/wait/ack or registered hook heartbeat', 'explicit retire or manual removal; locks release', 'persistent identity; refresh throttled by heartbeat_every', 'deaf sweep as for any registered name; hook fails open')
row('state.py', 'writer:write_json', 'atomic JSON replacement', 'rename or failed-temp cleanup', 'bounded caller operation; new files private', 'caller receives write or serialization failure')
row('state.py', 'writer:_replace_json|writer:update_json', 'hashed settings transformation under sibling lock', 'replace and receipt finalization or one re-merge then conflict', 'two attempts; kernel releases the lock on exit', 'settings_update_conflict preserves the latest edit')
row('hooks.py', 'writer:prepare|writer:finish|writer:finish_recovery', 'settings transaction saves migration intent', 'receipt finalized under the same settings lock', 'one re-merge before explicit conflict', 'owned-entry or receipt conflict leaves concurrent settings intact')
row('deliver.py', 'writer:prepare|writer:finish', 'delivery install or uninstall settings transaction', 'receipt prepared before rename and finalized under lock', 'one re-merge before explicit conflict', 'concurrent managed edits refuse rollback without replacing settings')

row('deliver.py', '.lock|writer:stop_hook', 'known session requests Stop', 'clean stop resets counter; kernel unlocks on exit', 'two blocks per session per ten minutes; five second hook deadline', 'errors and exhausted loop budget allow Stop')
row('routing.py', '.wait|.wait.lock|state/cursors', 'Stop inspects waiter evidence', 'read-only; no cursor or lease renewal', 'waiter-refreshed cursor within deliver.waiter_fresh_seconds or held kernel lock', 'missing waiter blocks Stop within the loop budget')

row('deliver.py', 'writer:prepare_recovery', 'retry an install with durable receipt and unchanged original settings', 'complete owned settings transform under the sibling lock', 'same original receipt; one re-merge on unrelated edit', 'receipt or managed-entry conflicts fail without overwriting edits')
# Relay state is local to one durable site; it never carries reader cursors.
row('relay.py', 'relay|state.sqlite3', 'configured sink or relay runner opens the local outbox',
    'rows marked sent on server acceptance; checkpoint advances only after durable apply/queue',
    'sent rows pruned below scan checkpoint; skipped audit IDs and suppression count persist; SQLite busy timeout 30s',
    'relay status/doctor expose pending rows, gaps and reconnect errors')
row('relay.py', 'run.lock|writer:run', 'one foreground relay runner acquires the kernel lock',
    'SIGINT/SIGTERM or failure closes socket/database/lock and restores signal handlers',
    '75s frame deadline; reconnect backoff capped at approximately 61s',
    'second runner refuses ownership; auth/retention failures exit with status error')
row('relay.py', 'status.json|writer:record', 'runner records connection, backlog and frame freshness',
    'replaced on state change or after 10s; disconnected in finally', '90s stale receipt fails health',
    'doctor relay check is red when disconnected/stale or an error persists')
row('relay.py', 'service.json|writer:main|stdout.log|stderr.log', 'explicit relay install --apply records ownership before manager mutation',
    'relay uninstall or lifecycle down removes the managed service and receipt',
    'persistent service; logs belong to host rotation policy',
    'manager errors fail command; install preview performs no mutations')
row('lifecycle.py', 'relay/service.json', 'relay installation receipt includes the service in managed templates',
    'explicit relay uninstall or down', 'persistent opt-in ownership receipt',
    'doctor compares installed service with manifest template')
row('selfupdate.py', 'relay/service.json', 'update reconciles explicitly installed relay service',
    'explicit relay uninstall or down', 'only owned services may be reinstalled',
    'manager failure triggers existing update rollback')
