"""Typed manifest contract; paths always belong to the manifest, never cwd."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
import re
import math
from .state import read_json
from .wait_policy import DEFAULT_BUS_KINDS
import sys
import tomllib


from .errors import ManifestError


def duration(value: str) -> int:
    match = re.fullmatch(r"(\d+)([smhd])", value) if isinstance(value, str) else None
    if not match or int(match[1]) == 0:
        raise ManifestError(f"invalid_duration: {value!r}")
    return int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]


@dataclass
class Project:
    name: str
    git_repo: str
    default_branch: str = "main"


@dataclass
class Anx:
    workspace: str = ""
    board: str = ""
    lead_actor: str = ""
    agent_prefix: str = ""
    base_url: str = ""
    # The reconciler already collects answers with `anx await --answers`; a bridge answer
    # job for the same agent would race it. Opt in only when the reconciler is not used.
    bridge_answers: bool = False


@dataclass
class Paths:
    root: Path
    bus_log: Path
    events: Path
    out_dir: Path


@dataclass
class Bus:
    wake_debounce_minutes: float = 5
    text_cap: int = 400
    idle_hours: float = 3
    live_check: bool = True
    mirror: bool = True
    mirror_kinds: list[str] = field(default_factory=lambda: ["ask", "ruling", "handoff", "hold", "unhold", "blocker", "decision", "lesson", "debt"])
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass
class Deliver:
    stop_requires_waiter: bool = True
    waiter_fresh_seconds: int = 180


@dataclass
class Accounts:
    facts_file: str = ""
    max_age: str = "2h"


@dataclass
class Agent:
    label: str
    actor: str


@dataclass
class UsageLane:
    name: str
    config_dir: str
    cwd: str
    model: str
    effort: str
    session_id: str = ""   # optional: pins the lane's current Claude Code session
    pane: str = ""
    resume: str = ""      # only "headless" permits Claude Code resume
    stop_requires_waiter: bool | None = None  # absent inherits [deliver]


@dataclass
class Agentctl:
    timeout: str = ""
    request_key_prefix: str = ""
    pools: dict[str, str] = field(default_factory=dict)


@dataclass
class Lane:
    initiative: str = ""
    lease_ttl: str = "2h"
    max_run_hours: float = 24
    hung_after: str = "2h"
    card_handoff: str = "lead"
    capacity_relaunch_max: int = 2
    capacity_fallback: list[str] = field(default_factory=list)
    autocontinue_hours: float = 6
    require_card: bool = True
    brief_check: str = ""


@dataclass
class Loop:
    max_age: str = "30m"
    default_cadence: str = "30m"
    fail_report_after: int = 3
    receipt_prefix: str = "loop-"
    # SPEC leaves per-loop configuration syntax open. Overrides inherit project defaults.
    prompt_template: str = ""
    transcript: str = ""
    progress_budget: str = "30m"
    stop_rule: str = "STOP file present"
    loops: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass
class Reconciler:
    interval_seconds: int = 300
    ruling_ask_after: str = "30m"
    stale_lease_grace: str = "10m"
    blocker_ask_after: str = "30m"
    answer_max_age: str = "1h"
    state_max_age: str = "7d"
    # Git directories shared by linked worktrees (object stores). A core.worktree in their
    # common config silently redirects every linked worktree to one tree.
    shared_git_dirs: list = field(default_factory=list)


@dataclass
class Obligations:
    # handoffs, asks and auditor flags each need an owner's pickup.
    ack_after: str = "30m"
    alert_after: str = "2h"
    realert_after: str = "6h"
    # Addressed asks get a per-recipient deadline (brief section 8.4): one unacked
    # for this long alerts, whatever the waiter state. Handoffs keep alert_after.
    ask_deadline: str = "15m"
    urgent_deadline: str = "5m"
    coordinator: str = ""
    flag_owner: str = ""
    flag_sources: list = field(default_factory=list)
    alert_command: list = field(default_factory=list)


@dataclass
class Addressed:
    # Listening evidence: a waiter lease, recent hook, or a busy bound transcript.
    lease_ttl: str = "90s"            # a lease not renewed for this long has lapsed
    activity_ttl: str = "15m"         # local post/ack listening evidence
    label_ttl: str = "24h"            # non-manifest roster labels expire
    busy_window: str = "10m"          # recent mtime of a transcript bound to this bus name
    heartbeat_every: str = "10s"      # waiter poll/renew interval
    iteration_deadline: str = "2m"    # hard deadline per waiter iteration (SIGALRM)
    rearm_grace: str = "3m"           # after a waiter exits (delivery/timeout) it counts as listening this long
    urgent_deadline: str = "5m"
    normal_deadline: str = "15m"
    realert_after: str = "1h"
    confirm_timeout: str = "90s"      # sidecar: ack or transcript growth within this window
    window_hours: float = 48
    deaf_backlog: str = "6h"          # open rows this old still escalate when their recipient is deaf
    sweep_interval: int = 60          # seconds between `coordkit sweep` launchd runs (installed by `up`)
    relay: str = ""                   # name or role that receives escalations as bus rows (live mode)
    relay_file: str = ""              # JSON-lines {at,text} file a human relay reads (pending-alerts)
    incumbent_waiters: bool = False   # pre-cutover: a `bus wait --to NAME` process counts as listening
    # Policy: false = escalations that would reach a lead/coordinator member go to an hourly digest
    lead_escalations: bool = True
    internal_senders: list[str] = field(default_factory=lambda: ["coordkit", "coordkit-report"])
    roles: dict = field(default_factory=dict)       # role -> ordered names or {members, human}
    agents: dict = field(default_factory=dict)      # name -> {role, runtime, adapter, fallback}


@dataclass
class Wait:
    kinds: str = DEFAULT_BUS_KINDS
    timeout_minutes: float = 55
    min_timeout_minutes: float = 0
    backoff: float = 1
    settle: float = 10
    debounce: float = 0
    ride_max: float = 900
    poll: float = 15
    ci_poll: float = 60
    anx_answers: bool = True
    anx_poll: float = 30
    ci: list[int] = field(default_factory=list)
    freezes: str = ""
    stale_alert_after: str = "1h"


@dataclass
class Merge:
    max_hold_age: str = "2h"
    max_minutes: float = 120
    poll_seconds: float = 30
    required_checks: list[str] = field(default_factory=list)
    allow_workflow_edits: bool = False
    # Merge only with a durable SHIP verdict for the exact head.
    require_review_ship: bool = False
    review_exempt_paths: list[str] = field(default_factory=list)


GUARDED_DEFAULT = [
    r"\bmerg(e|ing)\b", r"\bship(ping)?\b", r"\bdeploy", r"\bdelet(e|ing)\b|\bdrop (table|database)\b",
    r"\bcredential|\bsecret\b|\bpassword|\bapi[ _-]?key|\baccess token", r"\bspend|\bbudget\b|\$\d",
    r"\b(host|hosts|fleet|launchd|cluster|machine)\b",  # host or fleet change (only with an ACTION; see questions.guarded)
    r"\bprod(uction)? (user|data)|\buser data\b|\busers?'? (data|conversations|audio)|\b(owner|operator)'s \d+ conversations",
    r"\bproduct semantics|\bpricing\b",
]


@dataclass
class Questions:
    # Questions never stall silently.
    config_dirs: list = field(default_factory=list)   # harness config dirs whose sessions are managed
    fallback_role: str = "lead"                 # tier 1 when the asker declares no fallback
    human_actor: str = ""
    urgent_deadline: str = "15m"
    normal_deadline: str = "30m"
    guarded_patterns: list = field(default_factory=lambda: list(GUARDED_DEFAULT))
    modal_min_age: str = "10m"                         # sweep: pending AskUserQuestion this old
    notification_idle: str = "10m"
    prose_detector: str = "post"                       # log | post; heuristic capture of prose decision requests
    transcript_window_hours: float = 48
    delivery_cli: str = ""
    # the operator's own sessions are never captured: session ids, and bus names bound to a human's chat.
    exclude_sessions: list = field(default_factory=list)
    human_sessions: list = field(default_factory=list)


@dataclass
class Review:
    ledger: str = ""        # review ledger JSON lines
    family: str = "ledger"
    end_within_minutes: int = 90   # `review scan`: a started review with no end after this is unrecorded
    result_globs: list[str] = field(default_factory=list)  # `review scan`: result files that must be in the ledger


@dataclass
class Limits:
    rulings: int = 10
    blockers: int = 10
    holds: int = 10
    asks: int = 10
    leases: int = 20
    mirror: int = 20
    dead_letters: int = 1
    harvest: int = 50
    inbox: int = 100


@dataclass
class Escalation:
    card: str = ""
    ask_to: str = "lead"
    # Coordkit's own health findings (mirror, stock, drift, ...) go to this bus name, never to the operator.
    internal_to: str = ""
    internal_senders: list[str] = field(default_factory=list)


@dataclass
class Launchd:
    label_prefix: str = ""
    path: list[str] = field(default_factory=list)
    python: str = sys.executable


@dataclass
class Services:
    manager: str = ""


@dataclass
class RcHost:
    enabled: bool = False
    prefix: str = ""
    env: str = ""


@dataclass
class Shadow:
    lane_records: str = ""
    incumbent_bus_source: str = ""
    incumbent_merge_source: str = ""
    max_gap_seconds: int = 600


def _table(cls, data, section, required=()):
    if not isinstance(data, dict):
        raise ManifestError(f"invalid_type: {section} must be a table")
    names = {f.name for f in fields(cls)}
    for key in data:
        if key not in names:
            raise ManifestError(f"unknown_key: {section}.{key}")
    for key in required:
        if key not in data or data[key] == "":
            raise ManifestError(f"missing_key: {section}.{key}")
    # Dataclass annotations are postponed; these schema field names carry uniform types.
    lists = {"flag_sources", "alert_command", "shared_git_dirs", "mirror_kinds", "capacity_fallback", "required_checks", "path", "ci", "review_exempt_paths", "result_globs", "config_dirs", "guarded_patterns", "exclude_sessions", "human_sessions", "ref_roots", "internal_senders", "post_as"}
    maps = {"aliases", "pools", "loops", "roles", "agents", "projects"}
    booleans = {"stop_requires_waiter", "live_check", "mirror", "require_card", "anx_answers", "allow_workflow_edits", "enabled", "bridge_answers", "incumbent_waiters", "require_review_ship", "lead_escalations"}
    integers = {"waiter_fresh_seconds", "heartbeat_seconds", "text_cap", "capacity_relaunch_max", "fail_report_after", "interval_seconds", "max_gap_seconds", "sweep_interval", "end_within_minutes"} | {f.name for f in fields(Limits)}
    numbers = {"wake_debounce_minutes", "idle_hours", "max_run_hours", "autocontinue_hours", "timeout_minutes", "min_timeout_minutes", "backoff", "settle", "debounce", "ride_max", "poll", "ci_poll", "anx_poll", "max_minutes", "poll_seconds", "window_hours", "transcript_window_hours"}
    for key, value in data.items():
        ok = (isinstance(value, list) and all(type(x) is (int if key == "ci" else str) for x in value)) if key in lists else (
            isinstance(value, dict) if key in maps else type(value) is bool if key in booleans and cls is not Limits else
            type(value) is int if key in integers else type(value) in (int, float) if key in numbers else isinstance(value, str))
        if not ok:
            raise ManifestError(f"invalid_type: {section}.{key}")
        if key in (integers | numbers) and value < 0:
            raise ManifestError(f"invalid_value: {section}.{key} must be nonnegative")
        if key in {"aliases", "pools", "projects"} and not all(isinstance(v, str) and v for v in value.values()):
            raise ManifestError(f"invalid_type: {section}.{key} values must be nonempty strings")
    return cls(**data)


@dataclass
class Mcp:
    projects: dict[str, str] = field(default_factory=dict)
    train_state_dir: str = ""
    identity: str = ""
    # Lane names a connector caller may post as (behind the same bearer secret); empty keeps the fixed identity.
    post_as: list[str] = field(default_factory=list)
    train_pattern: str = ""
    ref_roots: list[str] = field(default_factory=list)


@dataclass
class Relay:
    url: str = ''
    site: str = ''
    secret_file: str = ''
    heartbeat_seconds: int = 50


@dataclass
class Manifest:
    file: Path
    project: Project
    anx: Anx
    paths: Paths
    bus: Bus
    deliver: Deliver
    agents: list[Agent]
    agentctl: Agentctl
    lane: Lane
    loop: Loop
    reconciler: Reconciler
    wait: Wait
    merge: Merge
    launchd: Launchd
    rc_host: RcHost
    limits: Limits = field(default_factory=Limits)
    escalation: Escalation = field(default_factory=Escalation)
    shadow: Shadow = field(default_factory=Shadow)
    obligations: Obligations = field(default_factory=Obligations)
    addressed: Addressed = field(default_factory=Addressed)
    review: Review = field(default_factory=Review)
    questions: Questions = field(default_factory=Questions)
    lanes: list[UsageLane] = field(default_factory=list)
    mcp: Mcp = field(default_factory=Mcp)
    services: Services = field(default_factory=Services)
    accounts: Accounts = field(default_factory=Accounts)
    relay: Relay | None = None
    adapters: dict = field(default_factory=dict)
    mode: str = "live"

    def path(self, value: str) -> Path:
        return (self.file.parent / Path(value).expanduser()).resolve()

    def writable(self):
        if self.mode == "shadow":
            raise ManifestError("shadow_mode_read_only")

    def validate_launchd(self):
        if not self.launchd.path:
            raise ManifestError("missing_key: launchd.path")
        if not all(Path(p).is_absolute() for p in self.launchd.path):
            raise ManifestError("invalid_value: launchd.path must be absolute")
        if str(Path.home() / ".local/bin") not in self.launchd.path:
            raise ManifestError("invalid_value: launchd.path must contain ~/.local/bin")
        if not Path(self.launchd.python).is_absolute():
            raise ManifestError("invalid_value: launchd.python must be absolute")


def discover(start: Path | None = None) -> Path:
    at = (start or Path.cwd()).resolve()
    for parent in (at, *at.parents):
        candidate = parent / "coordkit.toml"
        if candidate.is_file():
            return candidate
    raise ManifestError("manifest_not_found: coordkit.toml")


def load(path: str | Path | None = None) -> Manifest:
    file = Path(path).expanduser().resolve() if path else discover()
    try:
        with file.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"manifest_invalid: {file}: {exc}") from exc
    # TOML examples place mode under paths / rc_host; accept those spellings too.
    modes = [raw.pop("mode")] if "mode" in raw else []
    for section in ("paths", "rc_host"):
        if isinstance(raw.get(section), dict) and "mode" in raw[section]:
            modes.append(raw[section].pop("mode"))
    if len(set(map(str, modes))) > 1 or (modes and modes[0] not in ("live", "shadow")):
        raise ManifestError("invalid_value: mode")
    # Compatibility with installed manifests: the retired sidecar policy has no runtime consumer.
    responsiveness = raw.pop('responsiveness', {})
    if not isinstance(responsiveness, dict):
        raise ManifestError('invalid_type: responsiveness must be a table')
    allowed = {f.name for f in fields(Manifest)} - {"file", "mode"}
    for section in raw:
        if section not in allowed:
            raise ManifestError(f"unknown_key: {section}")
    project = _table(Project, raw.get("project", {}), "project", ("name", "git_repo"))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", project.name):
        raise ManifestError("invalid_value: project.name must be a slug")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", project.git_repo):
        raise ManifestError("invalid_value: project.git_repo must be owner/name")
    anx = _table(Anx, raw.get("anx", {}), "anx", ("workspace", "board") if "anx" in raw else ())
    anx.lead_actor = anx.lead_actor or f"ck-{project.name}-lead"
    anx.agent_prefix = anx.agent_prefix or f"ck-{project.name}"
    paths_raw = raw.get("paths", {})
    if not isinstance(paths_raw, dict) or set(paths_raw) - {"root", "bus_log", "events", "out_dir"}:
        raise ManifestError("invalid_value: paths")
    def resolve(value):
        if not isinstance(value, str) or not value:
            raise ManifestError("invalid_type: paths must be nonempty strings")
        return (file.parent / Path(value).expanduser()).resolve()
    root = resolve(paths_raw.get("root", ".coordkit"))
    paths = Paths(root, *[resolve(paths_raw[k]) if k in paths_raw else root / d for k, d in
                         (("bus_log", "bus.jsonl"), ("events", "events.ndjson"), ("out_dir", "runs"))])
    question_data = raw.get("questions")
    if isinstance(question_data, dict) and "cl2ctl" in question_data:
        question_data = dict(question_data)
        legacy_delivery_cli = question_data.pop("cl2ctl")
        if not isinstance(legacy_delivery_cli, str) or ("delivery_cli" in question_data and not isinstance(question_data["delivery_cli"], str)):
            raise ManifestError("invalid_type: questions.delivery_cli")
        if question_data.get("delivery_cli") and legacy_delivery_cli and question_data["delivery_cli"] != legacy_delivery_cli:
            raise ManifestError("invalid_value: conflicting questions delivery CLI keys")
        question_data["delivery_cli"] = question_data.get("delivery_cli") or legacy_delivery_cli
        raw["questions"] = question_data
    config = {name: _table(cls, raw.get(name, {}), name) for name, cls in
              (("deliver", Deliver), ("bus", Bus), ("agentctl", Agentctl), ("lane", Lane), ("loop", Loop), ("reconciler", Reconciler),
               ("wait", Wait), ("merge", Merge), ("launchd", Launchd), ("rc_host", RcHost), ("shadow", Shadow), ("limits", Limits), ("escalation", Escalation), ("obligations", Obligations), ("addressed", Addressed), ("review", Review), ("questions", Questions), ("mcp", Mcp), ("services", Services), ("accounts", Accounts))}
    if config["services"].manager not in ("", "launchd", "systemd"):
        raise ManifestError("invalid_value: services.manager")
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}', key) for key in config['mcp'].projects):
        raise ManifestError('invalid_value: mcp.projects names must be plain names')
    if not 1 <= config["shadow"].max_gap_seconds <= 600:
        raise ManifestError("invalid_value: shadow.max_gap_seconds must be 1..600")
    config['relay'] = None
    if 'relay' in raw:
        config['relay'] = _table(Relay, raw['relay'], 'relay', ('url', 'site', 'secret_file'))
        from .relay import validate
        validate(config['relay'])
    actl = config["agentctl"]
    if "agentctl" in raw and not actl.pools.get("implement"):
        raise ManifestError("missing_key: agentctl.pools.implement")
    if not all(re.fullmatch(r"pool:[a-zA-Z0-9_.-]+", v) for v in actl.pools.values()):
        raise ManifestError("invalid_value: agentctl.pools selectors must be pool:<name>")
    actl.request_key_prefix = actl.request_key_prefix or f"ck-{project.name}"
    launchd = config["launchd"]
    launchd.label_prefix = launchd.label_prefix or f"com.coordkit.{project.name}"
    launchd.path = [str(Path(p).expanduser()) for p in launchd.path]
    launchd.python = str(Path(launchd.python).expanduser())
    for section, keys in (("agentctl", ("timeout",)), ("lane", ("lease_ttl", "hung_after")), ("loop", ("default_cadence", "progress_budget", "max_age")),
                          ("reconciler", ("ruling_ask_after", "stale_lease_grace", "blocker_ask_after", "answer_max_age", "state_max_age")),
                          ("merge", ("max_hold_age",)), ("obligations", ("ack_after", "alert_after", "realert_after", "ask_deadline", "urgent_deadline")), ("wait", ("stale_alert_after",)),
                          ("addressed", ("lease_ttl", "busy_window", "activity_ttl", "label_ttl", "heartbeat_every", "iteration_deadline", "rearm_grace", "urgent_deadline",
                                         "normal_deadline", "realert_after", "confirm_timeout", "deaf_backlog")),
                          ("questions", ("urgent_deadline", "normal_deadline", "modal_min_age", "notification_idle")), ("accounts", ("max_age",))):
        for key in keys:
            value = getattr(config[section], key)
            if value or key != "timeout":
                try:
                    duration(value)
                except ManifestError as exc:
                    raise ManifestError(f"invalid_value: {section}.{key}: {exc}") from exc
    addressed = config["addressed"]
    name_ok = lambda v: isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", v) and v != "all"
    for role, spec in addressed.roles.items():
        if isinstance(spec, dict):
            if set(spec) - {'members', 'human'} or not isinstance(spec.get('human', False), bool):
                raise ManifestError(f"invalid_value: addressed.roles.{role} takes members and human boolean")
            members = spec.get('members')
        else:
            members = spec
        if not name_ok(role) or not isinstance(members, list) or not members or not all(map(name_ok, members)):
            raise ManifestError(f"invalid_value: addressed.roles.{role} must be a nonempty list of names")
    for name, spec in addressed.agents.items():
        if not name_ok(name) or not isinstance(spec, dict) or set(spec) - {"role", "runtime", "adapter", "fallback", "target", "remote", "human"} \
                or not all(isinstance(v, bool) if k == 'human' else isinstance(v, str) for k, v in spec.items()):
            raise ManifestError(f"invalid_value: addressed.agents.{name} takes role, runtime, adapter, fallback, target, remote strings and human boolean")
    questions = config["questions"]
    if questions.prose_detector not in ("log", "post"):
        raise ManifestError("invalid_value: questions.prose_detector must be log or post")
    for pattern in questions.guarded_patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ManifestError(f"invalid_value: questions.guarded_patterns {pattern!r}: {exc}") from exc
    if questions.delivery_cli and not Path(questions.delivery_cli).expanduser().is_absolute():
        raise ManifestError("invalid_value: questions.delivery_cli must be absolute")
    questions.delivery_cli = str(Path(questions.delivery_cli).expanduser()) if questions.delivery_cli else ""
    questions.human_actor = questions.human_actor or anx.lead_actor
    config["obligations"].coordinator = config["obligations"].coordinator or "lead"
    mcp = config["mcp"]
    mcp.identity = mcp.identity or f"ck-{project.name}-mcp"
    if not name_ok(mcp.identity):
        raise ManifestError("invalid_value: mcp.identity")
    if any(not name_ok(name) for name in mcp.post_as):
        raise ManifestError("invalid_value: mcp.post_as")
    try:
        re.compile(mcp.train_pattern)
    except re.error as exc:
        raise ManifestError(f"invalid_value: mcp.train_pattern: {exc}") from exc
    mcp.ref_roots = [str(resolve(p)) for p in mcp.ref_roots] if mcp.ref_roots else [str(root / "state/refs")]
    if config["lane"].max_run_hours <= 0:
        raise ManifestError("invalid_value: lane.max_run_hours must be positive")
    if not 60 <= duration(config["lane"].lease_ttl) <= 604800:
        raise ManifestError("invalid_value: lane.lease_ttl must be 60s..7d")
    if config["loop"].fail_report_after < 1 or config["bus"].text_cap < 1 or config["reconciler"].interval_seconds < 1:
        raise ManifestError("invalid_value: loop.fail_report_after, bus.text_cap or reconciler.interval_seconds")
    loop = config["loop"]
    for name, override in loop.loops.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", name) or not isinstance(override, dict):
            raise ManifestError(f"invalid_value: loop.loops.{name}")
        _table(Loop, override, f"loop.loops.{name}")
        if override.get("fail_report_after", 1) < 1:
            raise ManifestError(f"invalid_value: loop.loops.{name}.fail_report_after")
        for key in ("default_cadence", "progress_budget", "max_age"):
            if key in override:
                duration(override[key])
    agents_raw = raw.get("agents", [])
    if not isinstance(agents_raw, list):
        raise ManifestError("invalid_type: agents")
    agents = [_table(Agent, row, "agents", ("label", "actor")) for row in agents_raw]
    lanes_raw = raw.get("lanes", [])
    if not isinstance(lanes_raw, list):
        raise ManifestError("invalid_type: lanes must be an array of tables")
    lanes = [_table(UsageLane, row, "lanes", ("name", "config_dir", "cwd", "model", "effort")) for row in lanes_raw]
    if len({lane.name for lane in lanes}) != len(lanes):
        raise ManifestError("invalid_value: lanes.name duplicate")
    for lane, row in zip(lanes, lanes_raw):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", lane.name):
            raise ManifestError("invalid_value: lanes.name")
        if "pane" in row and (
                not lane.pane or any(ch.isspace() for ch in lane.pane)):
            raise ManifestError("invalid_value: lanes.pane must be nonempty without whitespace")
        if lane.resume not in ("", "headless"):
            raise ManifestError("invalid_value: lanes.resume must be headless or absent")
        lane.config_dir = str((file.parent / Path(lane.config_dir).expanduser()).resolve())
        lane.cwd = str((file.parent / Path(lane.cwd).expanduser()).resolve())
        if lane.session_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", lane.session_id):
            raise ManifestError("invalid_value: lanes.session_id")
    session_ids = [lane.session_id for lane in lanes if lane.session_id]
    if len(set(session_ids)) != len(session_ids):
        raise ManifestError("invalid_value: lanes.session_id duplicate")
    if not all(math.isfinite(getattr(config["wait"], k)) for k in ("min_timeout_minutes", "backoff")):
        raise ManifestError("invalid_value: wait policy must be finite")
    if config["wait"].backoff < 1:
        raise ManifestError("invalid_value: wait.backoff must be at least 1")
    if len({a.label for a in agents}) != len(agents) or any(a.label == "lead" for a in agents):
        raise ManifestError("invalid_value: agents.label duplicate or reserved lead")
    identity = read_json(root/'lead.json')
    if identity is not None:
        actor = identity.get('actor')
        if not isinstance(actor, str) or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', actor):
            raise ManifestError('invalid_lead_identity')
        if not raw.get("questions", {}).get("human_actor"):
            questions.human_actor = actor
        anx.lead_actor = actor
    if not isinstance(raw.get('adapters', {}), dict):
        raise ManifestError('invalid_type: adapters must be a table')
    return Manifest(file, project, anx, paths, adapters=raw.get('adapters', {}), agents=agents, lanes=lanes, mode=modes[0] if modes else "live", **config)
