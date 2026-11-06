"""Read-only obligation stock used by reconciliation."""
import time
from . import bus, deadletters
from .actl import AgentctlClient
from .anxcli import AnxClient
from .merge import holds_on, PR_MENTION
from .process import parse_ts, ToolError
from .state import read_json


def held(rows, warn=None):
    """Active holds per PR. holds_on ignores rows that neither cover nor name its PR, so each PR is given
    only its own rows (in bus order): one indexing pass avoids PRs x rows regex scans."""
    by_pr = {}
    for row in rows:
        named = {str(pr) for pr in row.get('prs', [])}
        named.update(m.group(1) for m in PR_MENTION.finditer(row.get('text') or ''))
        for pr in named:
            by_pr.setdefault(pr, []).append(row)
    return [(pr, poster, row) for pr in sorted(by_pr) for poster, row in
            holds_on(pr, by_pr[pr], **({'warn': lambda row, pr=pr: warn(pr, row)} if warn else {})).items()]


def timestamp(row, fallback):
    raw = row.get('ts') or row.get('created_at') or row.get('acquired_at')
    return parse_ts(raw).timestamp() if raw else fallback


def stock(manifest, at=None, since=None, hold_warning=None):
    """since is the adoption baseline (reconcile's obligations_since; read from its state when not given):
    bus-derived groups count only rows that coordkit wrote or that arrived after it, so a shared bus's
    history is never counted as current stock."""
    at = time.time() if at is None else at
    if since is None:
        since = read_json(manifest.paths.root/'reconcile/state.json', {}).get('obligations_since')
    bus.configure(manifest)
    rows = bus.read()
    groups = {kind: [] for kind in vars(manifest.limits)}
    groups['rulings'] = bus.open_rulings(rows)
    groups['blockers'] = bus.open_blockers(rows)
    groups['holds'] = [row for _, _, row in held(rows, hold_warning)]
    closed = {row.get('re') for row in rows if row.get('kind') in ('ack', 'answer', 'decision')}
    groups['asks'] = [row for row in rows if row.get('kind') == 'ask' and row['id'] not in closed]
    root = manifest.paths.root/'state'
    def cursor(path):
        return int(path.read_text()) if path.exists() else 0
    groups['mirror'] = rows[cursor(root/'mirror.cursor'):] if manifest.bus.mirror else []
    groups['harvest'] = [row for row in rows[cursor(root/'harvest.cursor'):] if row.get('kind') in ('lesson', 'debt')]
    # Only obligations count: unacked, unreplied wake-kind rows. fyi/msg traffic past a cursor that `coordkit wait`
    # never advances would otherwise grow without bound.
    from .addressed import WAKE_KINDS, ack_state, replies
    by_re = replies(rows)
    for label in ['lead', *[agent.label for agent in manifest.agents]]:
        groups['inbox'] += [row for row in rows[cursor(bus.cursor_path(label, 'inbox')):] if bus.for_lane(row, label)
                            and row.get('kind') in WAKE_KINDS and ack_state(row, by_re, manifest) is None]
    for kind in ('rulings', 'blockers', 'holds', 'asks', 'harvest', 'inbox'):
        groups[kind] = [row for row in groups[kind] if bus.adopted_row(row, since)]
    client = AnxClient(manifest)
    snapshot = client.call('cards', 'list', '--board', manifest.anx.board)
    if snapshot.get('has_more') or snapshot.get('truncated') or snapshot.get('next_cursor'):
        raise ToolError('anx', 6, 'stock card snapshot truncated', 'protocol_error')
    scope = {card.get('ref') or 'card:'+card['handle'] for card in snapshot.get('cards', [])}
    scope.add(manifest.escalation.card)
    inbox = client.call('inbox', 'list', '--status', 'open')
    if inbox.get('next_cursor') or inbox.get('has_more') or inbox.get('truncated'):
        raise ToolError('anx', 6, 'ask stock snapshot truncated', 'protocol_error')
    groups['asks'] += [item for item in inbox.get('items', [])
                       if (item.get('subject_ref') or item.get('subject', {}).get('ref')) in scope]
    for card in snapshot.get('cards', []):
        lease = client.call('lease', 'show', card.get('ref') or 'card:'+card['handle']).get('lease') or {}
        if lease.get('owner'):
            groups['leases'].append(lease)
    spool = AgentctlClient(manifest).call('anx', 'status')
    groups['dead_letters'] = deadletters.ours(manifest, spool.get('dead_letters', []))
    result = {}
    for kind, items in groups.items():
        count = len(items)
        if kind == 'dead_letters':
            # Listed-but-unattributed entries are already in items; unlisted ones are unknown.
            listed = len(spool.get('dead_letters', []))
            total = spool.get('dead_letter_count', listed)
            if total < listed:
                raise ValueError('invalid dead letter count')
            count += total - listed
        unknown = any(not any(row.get(key) for key in ('ts', 'created_at', 'acquired_at')) for row in items)
        result[kind] = dict(count=count, oldest=max([max(0, at-timestamp(row, at)) for row in items], default=0),
                            threshold=getattr(manifest.limits, kind), age_unknown=unknown or count > len(items))
    return result


def report(manifest, prefix='STATUS', at=None, since=None, hold_warning=None):
    result = stock(manifest, at, since, hold_warning)
    for kind, item in result.items():
        print(f'{prefix} stock {kind} count={item["count"]} oldest={item["oldest"]:.0f}s threshold={item["threshold"]}'
              + (' age=unknown' if item['age_unknown'] else ''))
    return result
