"""Route coordination findings to an agent, its digest, or the relay file."""
import re
from .manifest import ManifestError
from .process import ToolError


def validate(manifest):
    if not re.fullmatch(r'card:[A-Za-z0-9][A-Za-z0-9._-]{0,63}', manifest.escalation.card):
        raise ManifestError('missing or invalid escalation.card')
    if manifest.escalation.ask_to != 'lead':
        raise ManifestError('escalation.ask_to must be lead')
    if any(value <= 0 for value in vars(manifest.limits).values()):
        raise ManifestError('limits thresholds must be positive')


# Findings about coordkit itself go to its maintainer; coordination findings (rulings, blockers, holds, lanes,
# review) go to the lead, who owns coordination.
INTERNAL_KINDS = {'mirror', 'stock', 'drift', 'answers', 'loops', 'loop_failure', 'git'}


def ask(manifest, card, kind='finding', **payload):
    """A machine finding never goes to the operator: an agent can always act on it. It becomes an
    addressed bus ask (deadline, deaf escalation) to `[escalation] internal_to`, else `ask_to` (the lead), written
    as a plain brief. The agent decides whether the operator is needed and, if so, files its own brief."""
    from . import addressed, briefs
    validate(manifest)
    brief = briefs.machine(kind, evidence=briefs.pr_evidence(manifest, payload.get('situation', '')),
                           subject=briefs.subject_words(card),
                           **{k: v for k, v in payload.items() if k not in ('key', 'default', 'no_auto_default')})
    to = (manifest.escalation.internal_to if kind in INTERNAL_KINDS else '') or manifest.escalation.ask_to
    from .addressed import digest_only, to_lead_digest
    # Most important first: the bus shows the first 400 characters and spills the rest to a ref file.
    actions = ' / '.join(o['action'] for o in brief['options'][:4])
    consequences = ' '.join(f"{o['action']}: {o['consequence']}" for o in brief['options'][:4])
    # The reader is an agent, so it gets the raw situation with its exact ids and paths (scrubbing is for the operator's
    # briefs only), under the plain title and options.
    situation, recommend = payload.get('situation', ''), payload.get('recommend', '')
    text = (f"coordkit {kind}: {brief['title']} Recommended: {brief['recommendation']}. Options: {actions}."
            + (f' [{card}]' if card else '') + f" {situation} Next step: {recommend} {consequences} "
            f"{brief['if_no_answer']}")
    if digest_only(manifest, to):
        if manifest.mode == 'live':
            from .questions import queue_escalation
            queue_escalation(manifest, to, f'finding:{kind}', text, key=payload.get('key'),
                             default=payload.get('default'), no_auto_default=payload.get('no_auto_default', False))
        else:
            to_lead_digest(manifest, f'finding:{kind}', text)
        return {'routed_to': 'lead_digest'}
    try:
        extra = ['--force']
        for field in ('key', 'default'):
            if payload.get(field):
                extra += ['--'+field, payload[field]]
        if payload.get('no_auto_default'):
            extra += ['--no-auto-default']
        addressed.send(manifest, 'coordkit', to, text, kind='ask', topic=f'finding:{kind}', extra=extra)
        result = {'routed_to': to}
    except (SystemExit, Exception) as exc:  # the bus itself is failing (e.g. the mirror-failure finding): relay
        import sys
        print(f'ESCALATION_RELAYED {kind}: bus post failed ({exc!r}); {text[:200]}', file=sys.stderr)
        if not addressed.to_relay_file(manifest, f'ESCALATION {kind} for {to} (bus post failed): {text}'[:900]):
            raise ToolError('bus', 6, f'escalation undeliverable: bus post failed and no relay file: {exc!r}')
        result = {'routed_to': 'relay'}
    return result
