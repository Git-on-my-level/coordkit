"""ANX obligation projection. The append-only bus remains authoritative on failure."""
from .actl import AgentctlClient
from .anxcli import AnxClient, CardUnresolved
from .process import ToolError
import sys


def card_for(manifest, label, subject=None):
    client = AnxClient(manifest)
    if subject and (subject.startswith('card:') or subject.startswith(manifest.loop.receipt_prefix)):
        card = subject if subject.startswith('card:') else f'card:{subject}'
        client.resolve_card(card)
        return card
    # A subject can name a train or a PR, not necessarily a card. Prefer the owner's
    # explicit execution linkage, including a recent terminal run for its handoff.
    actl = AgentctlClient(manifest)
    try:
        active = [e for e in actl.recent(nonterminal=True) if label in e.get('labels', [])]
        executions = active or [e for e in actl.recent(label=label) if label in e.get('labels', [])][:1]
    except ToolError as exc:
        # Decoupling (addressed-bus brief section 5): without agentctl installed there is no
        # execution linkage; resolve the subject or label card instead. Other failures still raise.
        if exc.category != 'dependency_unavailable':
            raise
        executions = []
    matches = {tag.removeprefix('anx.card.') for execution in executions
               for tag in execution.get('labels', []) if tag.startswith('anx.card.')}
    if len(matches) > 1:
        raise CardUnresolved(label, 'ambiguous execution card linkage')
    card = f'card:{next(iter(matches)) if matches else subject or label}'
    client.resolve_card(card)
    return card


def mirror(manifest, row, rows):
    kind = row['kind']
    if kind in ('fyi', 'msg') or row.get('copy_of'):
        return
    # A ruling without an owner, or an ack of one, has no card to note:
    # skip it loudly instead of failing the whole projection.
    if kind == 'ruling' and not row.get('owner'):
        print(f"MIRROR_SKIPPED ruling {row.get('id')} has no owner", file=sys.stderr)
        return
    if kind == 'ack':
        original = next((r for r in rows if r.get('id') == row.get('re')), None)
        if original is None or (original.get('kind') == 'ruling' and not original.get('owner')):
            print(f"MIRROR_SKIPPED ack {row.get('id')} of an unknown or ownerless ruling", file=sys.stderr)
            return
    client = AnxClient(manifest)
    if kind in ('hold', 'unhold'):
        for pr in row['prs']:
            if kind == 'hold':
                client.call('cards', 'create', '--board', manifest.anx.board, '--handle',
                            f"hold-pr-{pr}-{row['frm']}", '--title', f"Hold PR {pr}: {row['text']}",
                            '--column', 'blocked', '--request-key', f"ck-{manifest.project.name}-hold-{pr}-{row['frm']}", write=True)
            else:
                posters = {r['frm'] for r in rows if r.get('kind') == 'hold' and pr in r.get('prs', [])
                           and (r['frm'] == row['frm'] or row['frm'] == 'lead')}
                for poster in sorted(posters):
                    client.call('cards', 'resolve', f'card:hold-pr-{pr}-{poster}', '--reason', 'unhold', write=True)
        return
    if kind == 'ack':
        original = next(r for r in rows if r.get('id') == row['re'])
        if original.get('kind') != 'ruling':
            return  # handoff/ask pickups live on the bus; no ruling card to note
        card = card_for(manifest, original['owner'], original.get('subject'))
        client.note(card, f"acked ruling {row['re']}", evidence=f"bus:{row['id']}", met=True)
        return
    if kind == 'ruling':
        card = card_for(manifest, row['owner'], row.get('subject'))
        client.note(card, f"ack ruling {row['id']}: {row['text']}", next_in='30m')
        return
    if kind == 'blocker' and row['owner'] == 'lead':
        card = card_for(manifest, row['frm'], row.get('subject'))
        # The lead owns this blocker on the bus; only the lead decides whether the operator is needed,
        # so the projection records it on the card instead of asking the operator directly.
        client.note(card, f"blocked ({row['frm']}): {row.get('needs') or row['text']}"[:400], evidence=f"bus:{row['id']}")
        return
    original = next((r for r in rows if r.get('id') == row.get('re')), {})
    subject = row.get('subject') or original.get('subject')
    label = row.get('owner') or (original.get('frm', row['frm']) if kind == 'decision'
                                 else row['to'] if kind == 'ask' else row['frm'])
    card = card_for(manifest, label, subject)
    if kind == 'handoff' and not card.removeprefix('card:').startswith(manifest.loop.receipt_prefix):
        client.call('cards', 'move', card, '--column', 'review', write=True)
    client.note(card, f"{kind} {row['id']}: {row.get('needs') or row['text']}", evidence=row.get('evidence'))
