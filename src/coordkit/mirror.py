"""ANX obligation projection. The append-only bus remains authoritative on failure."""
from .actl import AgentctlClient
from .anxcli import AnxClient, CardUnresolved
from .process import ToolError
import sys
import time


def card_for(manifest, label, subject=None, *, resolve=None):
    client = AnxClient(manifest)
    resolve = resolve or client.resolve_card
    if subject and (subject.startswith('card:') or subject.startswith(manifest.loop.receipt_prefix)):
        card = subject if subject.startswith('card:') else f'card:{subject}'
        resolve(card)
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
    resolve(card)
    return card


def board_write(client, row_id, what, *args, attempts=3, pause=1.0, missing_ok=False):
    """Board-column writes (move, resolve, hold create) are projections of bus state, which stays
    authoritative. A busy shared board answers them with a stale-token or reused-request conflict
    (409, retryable), so retry, then skip loudly: one conflicting row must never stall projection."""
    for attempt in range(attempts):
        try:
            client.call(*args, write=True)
            return True
        except ToolError as exc:
            if missing_ok and exc.category == 'not_found':
                print(f"MIRROR_BOARD_SKIPPED {what} {row_id}: card not found (never projected or already gone)", file=sys.stderr)
                return False
            if exc.category != 'conflict':
                raise
            if attempt + 1 < attempts:
                time.sleep(pause * (attempt + 1))
    print(f"MIRROR_BOARD_SKIPPED {what} {row_id}: board kept conflicting (x{attempts}); the bus row stands", file=sys.stderr)
    return False


def move_to_review(client, card, row_id, attempts=3, pause=1.0):
    """The column is cosmetic; the note is the record."""
    return board_write(client, row_id, f'move {card}', 'cards', 'move', card, '--column', 'review',
                       attempts=attempts, pause=pause)


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
                board_write(client, row.get('id'), f"hold PR {pr}", 'cards', 'create', '--board', manifest.anx.board,
                            '--handle', f"hold-pr-{pr}-{row['frm']}", '--title', f"Hold PR {pr}: {row['text']}",
                            '--body', row['text'], '--column', 'blocked',
                            '--request-key', f"ck-{manifest.project.name}-hold-{pr}-{row['frm']}")
            else:
                posters = {r['frm'] for r in rows if r.get('kind') == 'hold' and pr in r.get('prs', [])
                           and (r['frm'] == row['frm'] or row['frm'] == 'lead')}
                for poster in sorted(posters):
                    # ANX resolve needs evidence (--body or --resolution-ref); --reason only stamps the audit.
                    board_write(client, row.get('id'), f"unhold PR {pr}", 'cards', 'resolve', f'card:hold-pr-{pr}-{poster}',
                                '--reason', 'unhold', '--body', f"unhold {row.get('id')} ({row['frm']}): {row.get('text') or 'released'}"[:400],
                                missing_ok=True)  # a hold card that was never projected is already released
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
        move_to_review(client, card, row['id'])
    client.note(card, f"{kind} {row['id']}: {row.get('needs') or row['text']}", evidence=row.get('evidence'))
