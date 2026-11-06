"""The agentctl dead-letter spool is host-wide and capped at 256, shared by every project.

Only this project's letters are its business. Entries recorded in the activation baseline
predate the project's runs; newer entries are attributed by the request key agentctl
recorded at delegation. An entry that cannot be attributed counts as ours."""
import hashlib
import json
from .actl import AgentctlClient
from .state import read_json, write_json


def fingerprint(row):
    return hashlib.sha256(json.dumps({k:v for k,v in row.items() if k != 'attempts'},sort_keys=True).encode()).hexdigest()


def ours(manifest, letters, include_baseline=False):
    known = set() if include_baseline else set(
        read_json(manifest.paths.root/'dead-letter-baseline.json', {}).get('fingerprints', []))
    path = manifest.paths.root/'state'/'dead-owners.json'
    cached = read_json(path, {})
    owners, actl, out = {}, AgentctlClient(manifest), []
    dispositioned = set((read_json(manifest.paths.root/'dead-letter-dispositions.json', {}) or {}))
    for row in letters:
        if fingerprint(row) in known or fingerprint(row) in dispositioned:
            continue
        execution = row.get('execution_id') or ''
        verdict = cached.get(execution)
        if verdict is None:
            # Definite answers are cached; each status call costs seconds on a busy host.
            verdict = actl.owns(execution) if execution else None
        if verdict is not None:
            owners[execution] = verdict
        if verdict is not False:
            out.append(row)
    if manifest.mode != 'shadow' and owners != cached:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, owners)  # pruned to the letters still listed
    return out


# Audited disposition: a named exit for one attributed dead letter. The letter
# stays in the spool and in the audit file; only attribution skips it. Keyed by fingerprint, so a changed or
# new letter for the same execution is visible again. Reopen with reopen().
DISPOSITIONS = 'dead-letter-dispositions.json'


def dispositions(manifest):
    return read_json(manifest.paths.root/DISPOSITIONS, {}) or {}


def disposition(manifest, execution_id, reason, by, letters):
    """Record a reasoned disposition for every current letter of this execution attributed to us."""
    import time
    manifest.writable()
    if len((reason or '').strip()) < 10:
        raise ValueError('disposition_reason_required: say why (at least 10 characters)')
    matching = [row for row in letters if row.get('execution_id') == execution_id]
    if not matching:
        raise ValueError(f'disposition_not_a_dead_letter: {execution_id} is not in the spool')
    # Positive ownership only: a letter whose owner is unknown (status error, no request key) counts as
    # ours for doctor's fail-closed check, but must never be dispositioned as ours.
    # Positive, unambiguous ownership: the execution's request key is one this project issued (a lane plan
    # key, its -rN/-ac continuations, or a configured loop's key). A shared prefix ("ck-example" vs
    # "ck-example-tools") is not enough.
    try:
        key = ((AgentctlClient(manifest).status(execution_id) or {}).get('delegation') or {}).get('request_key')
    except Exception:
        key = None
    if not key or not issued(manifest, key):
        raise ValueError(f'disposition_not_ours: {execution_id} (request key {key!r}) was not issued by this project')
    attributed = ours(manifest, matching, include_baseline=True)
    if not attributed:
        raise ValueError(f'disposition_not_ours: {execution_id} belongs to another project')
    data = dispositions(manifest)
    for row in attributed:
        data[fingerprint(row)] = {'execution_id': execution_id, 'reason': reason.strip(), 'by': by,
                                  'at': time.time(), 'letter': row}
    write_json(manifest.paths.root/DISPOSITIONS, data)
    return len(attributed)


def reopen(manifest, execution_id):
    manifest.writable()
    data = dispositions(manifest)
    kept = {k: v for k, v in data.items() if v.get('execution_id') != execution_id}
    write_json(manifest.paths.root/DISPOSITIONS, kept)
    return len(data) - len(kept)


def issued(manifest, key):
    """True when this project issued the request key."""
    import re
    for path in (manifest.paths.out_dir/'req').glob('*.plan.json'):
        plan = read_json(path, {}) or {}
        base = plan.get('key')
        if base and (key == base or re.fullmatch(re.escape(base) + r'(-r\d+|-ac)+', key)):
            return True
    prefix = manifest.agentctl.request_key_prefix
    for name in manifest.loop.loops:
        handle = manifest.loop.receipt_prefix + name
        if re.fullmatch(re.escape(f'{prefix}-{handle}-') + r'[0-9TZ.a-f-]+', key):
            return True
    return False
