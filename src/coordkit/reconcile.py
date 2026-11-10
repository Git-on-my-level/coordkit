"""Bounded reconciliation: prove liveness, repair safe drift, ask for decisions."""
import argparse
import fcntl
import hashlib
import json
import re
from pathlib import Path
import time

from . import bus, deadletters, lane, loop, loop_answers, escalation
from .stock import held, report
from .actl import AgentctlClient
from .anxcli import AnxClient, CardUnresolved, past_promises
from .manifest import ManifestError, duration
from .merge import gh
from .mirror import card_for
from .process import ToolError, parse_ts, run
from .state import read_json, write_json


MISSING_CARD_TTL = 6 * 3600
MISSING_CARD_REVERIFY = 3600
MISSING_CARD_LIMIT = 2000


def card_cache_scope(manifest):
    return [manifest.anx.workspace, manifest.anx.lead_actor, manifest.anx.base_url]


def missing_card_key(manifest, ref):
    return json.dumps([*card_cache_scope(manifest), ref], separators=(',', ':'))


def prune_missing_cards(missing, scope, at):
    # Legacy ref-only entries have no trustworthy workspace identity: discard.
    for key, entry in list(missing.items()):
        if entry.get('scope') != scope or entry['expires_at'] <= at:
            del missing[key]
    oldest = sorted(missing, key=lambda key: (missing[key]['created_at'], key))
    for key in oldest[:max(0, len(missing)-MISSING_CARD_LIMIT)]:
        del missing[key]


def card_ref(card):
    return card.get('ref') or 'card:'+card['handle']


def reconcile(manifest, dry_run=False, at=None):
    if not dry_run:
        manifest.writable()
    started = time.monotonic()
    at=time.time() if at is None else at
    state_path=manifest.paths.root/'reconcile/state.json'
    state=read_json(state_path, {'fired':[], 'dead':{}, 'loops':{}})
    # Upgrade legacy fired lists once, then keep bounded timestamped receipts.
    if isinstance(state['fired'], list):
        state['fired'] = dict.fromkeys(state['fired'], at)
    max_age = duration(manifest.reconciler.state_max_age)
    state['fired'] = {key:stamp for key,stamp in state['fired'].items() if key.startswith('hold-text:') or at-stamp < max_age}
    state['dead'] = {key:stamp for key,stamp in state['dead'].items() if at-stamp < max_age}
    fired = state['fired']
    state['last_run'] = at
    state['started_at'] = at
    state.setdefault('blockers', {})
    state.setdefault('holds', {})
    # One durable receipt per orphan, independent of fired's rolling retention.
    orphan_notified = state.setdefault('orphan_notified', {})
    orphan_batches = state.setdefault('orphan_batches', {})
    orphan_pending = state.setdefault('orphan_pending', {})
    missing = state.setdefault('missing_cards', {})
    cache_scope = card_cache_scope(manifest)
    prune_missing_cards(missing, cache_scope, at)
    unresolved = state.setdefault('unresolved_obligations', {})
    if not dry_run:
        write_json(state_path, state)
    client, actl=AnxClient(manifest), AgentctlClient(manifest)
    findings=False
    def action(check,key,detail,perform,ask=False):
        nonlocal findings
        if key in fired:
            return
        print(f'RECONCILE {check} {"would_fix" if dry_run else "fix"} {detail}')
        if not dry_run:
            try:
                perform()
            except (ToolError, ManifestError) as exc:
                if not ask:
                    raise
                # An undeliverable escalation (the bus itself failing, no relay, or
                # an unroutable retired target) is retried next run, never lost.
                print(f'RECONCILE {check} escalation_failed {detail}: {exc}')
                findings=True
                return
            fired[key] = at
            write_json(state_path,state)
        findings=True
    def notify(argv, manifest):
        from .addressed import digest_only, to_lead_digest
        target = argv[argv.index('--to')+1]
        if digest_only(manifest, target) and argv[argv.index('--kind')+1] != 'decision':
            to_lead_digest(manifest, 'reconcile', argv[-1], at=at)
        else:
            bus.main(argv, manifest)

    lookups, resolutions = {}, {}
    def resolve_card(ref):
        cache_key = missing_card_key(manifest, ref)
        had_missing = cache_key in missing
        if ref not in lookups:
            cached = missing.get(cache_key)
            if cached and at-cached['checked_at'] < MISSING_CARD_REVERIFY:
                lookups[ref] = CardUnresolved(ref, 'not_found (cached)')
            else:
                # A failed retry must not turn a transport/auth error into a new
                # proof of absence. Only a fresh not_found repopulates the cache.
                missing.pop(cache_key, None)
                try:
                    lookups[ref] = client.resolve_card(ref)
                except ToolError as exc:
                    lookups[ref] = exc
                    # CardUnresolved wraps protocol/auth errors too. Only a confirmed
                    # not_found is evidence safe to retain across passes.
                    cause = exc
                    while cause is not None:
                        if isinstance(cause, ToolError) and cause.category == 'not_found':
                            missing[cache_key] = {'scope': cache_scope, 'ref': ref,
                                'created_at': cached['created_at'] if cached else at,
                                'checked_at': at, 'expires_at': at + MISSING_CARD_TTL}
                            prune_missing_cards(missing, cache_scope, at)
                            if not dry_run:
                                write_json(state_path, state)
                            break
                        cause = cause.__cause__
        result = lookups[ref]
        if isinstance(result, ToolError):
            raise result
        cleared = False
        for key in list(unresolved):
            if unresolved[key]['card'] == ref:
                del unresolved[key]
                cleared = True
        if (cleared or had_missing) and not dry_run:
            write_json(state_path, state)
        return result

    def obligation_card(row):
        # Mapping may need agentctl; share it too, but cache actual card reads by
        # reference so different owners of the same card cannot multiply calls.
        key = (row['owner'], row.get('subject'))
        try:
            if key not in resolutions:
                try:
                    resolutions[key] = card_for(manifest, *key, resolve=resolve_card)
                except ToolError as exc:
                    resolutions[key] = exc
            if isinstance(resolutions[key], ToolError):
                raise resolutions[key]
            if unresolved.pop(row['id'], None) is not None and not dry_run:
                write_json(state_path, state)
            return resolutions[key]
        except ToolError as exc:
            ref = next((ref for ref, result in lookups.items() if result is exc), None)
            cached = missing.get(missing_card_key(manifest, ref)) if ref else None
            record = {'card': ref, 'owner': row['owner'], 'detail': str(exc), 'scope': cache_scope}
            if cached:
                record['checked_at'] = cached['checked_at']
            if unresolved.get(row['id']) != record:
                unresolved[row['id']] = record
                if not dry_run:
                    write_json(state_path, state)
            return manifest.escalation.card
    def ask(check,key,card,situation,recommend):
        action(check,key,card,lambda:escalation.ask(manifest,card,kind=check,situation=situation,
            impact='An unresolved coordination obligation can strand work or lose a follow-up.',
            recommend=recommend,why='The reconciler cannot safely decide acceptance or ownership on behalf of the lead.'),ask=True)
    bus.configure(manifest)
    if not dry_run:
        from .answers import consume
        consume(manifest)
        loop_answers.drain(manifest)
    rows=bus.read()
    active=actl.recent(nonterminal=True)
    card_result=client.call('cards','list','--board',manifest.anx.board)
    cards=card_result.get('cards',[])
    if card_result.get('has_more') or card_result.get('truncated') or card_result.get('next_cursor'):
        raise ToolError('anx',6,'active-card snapshot truncated','protocol_error')
    owner_labels={label for e in active for label in e.get('labels',[]) if e.get('liveness') not in ('exited','lost','orphaned')}
    for agent in manifest.agents:
        if agent.label in owner_labels:
            owner_labels.add(agent.actor)
    known={a.label for a in manifest.agents}|{a.actor for a in manifest.agents}
    local_plans=[]
    for path in (manifest.paths.out_dir/'req').glob('*.plan.json'):
        plan=read_json(path)
        known.add(plan['label'])
        local_plans.append(plan)
        try:
            if lane.recover(manifest,plan,dry_run):
                findings=True
        except ToolError as exc:
            ask('lanes', f'lane:{plan["key"]}:{plan.get("started",0)}:{exc.category}', plan.get('card'),
                f'Lane {plan["label"]} cannot recover: {exc}',
                'Inspect this execution and reconcile its effects before releasing or relaunching it.')
    for card in cards:
        ref=card_ref(card)
        if card.get('column_key',card.get('placement')) not in ('in_progress','review'):
            continue
        lease=client.call('lease','show',ref).get('lease') or {}
        owner=lease.get('owner','').removeprefix('actor:')
        key=f"{ref}:{lease.get('epoch',0)}:{owner}"
        if not owner or owner in client.identities() or owner in owner_labels:
            state['dead'].pop(key,None)
        elif owner in known:
            since=state['dead'].setdefault(key,at)
            if at-since >= duration(manifest.reconciler.stale_lease_grace):
                def release(ref=ref,owner=owner,key=key,lease=lease):
                    # Recheck immediately before mutation; unknown liveness still holds ownership.
                    owner_names={owner}|{agent.label for agent in manifest.agents if agent.actor == owner}
                    if any(owner_names.intersection(e.get('labels',[])) for e in actl.recent(nonterminal=True)):
                        raise ToolError('agentctl',6,'owner became active during reconciliation','owner_live')
                    client.note(ref,f'Reconciler: owner {owner} absent beyond stale lease grace.')
                    client.call('lease','release',ref,'--owner',lease['owner'],
                                '--handoff-to','actor:'+manifest.anx.lead_actor,write=True)
                    state['dead'].pop(key,None)
                action('leases','lease:'+key,ref,release)
        else:
            print(f'RECONCILE leases unknown_owner {ref} {owner}')
        prs=list(card.get('prs',[]))
        prs += [pr for plan in local_plans if plan.get('card') == ref for pr in plan.get('owns',[])]
        for related in card.get('related_refs',[]):
            raw=related if isinstance(related,str) else related.get('ref','')
            match=re.search(re.escape(manifest.project.git_repo)+r'(?:#|/pull/)([0-9]+)',raw)
            if match:
                prs.append(match[1])
        prs += [p for row in rows if row.get('subject') == ref for p in row.get('prs',[])]
        if card.get('column_key',card.get('placement')) == 'review':
            for pr in sorted(set(map(str,prs))):
                info=json.loads(gh(manifest,'pr','view',pr,'--json','state,mergeCommit'))
                if info.get('state') == 'MERGED':
                    ask('review',f'merged:{ref}:{pr}',ref,f'PR {pr} merged while {ref} remains in review.',
                        'Verify the merge evidence and resolve this card as the lead.')
    print('RECONCILE leases checked active lane cards')
    orient=client.call('orient')
    overdue=set()
    scope={card_ref(card) for card in cards}
    scope.update('card:'+manifest.loop.receipt_prefix+name for name in manifest.loop.loops)
    for card in past_promises(orient):
        ref=card_ref(card)
        if ref not in scope or not ref.removeprefix('card:').startswith(manifest.loop.receipt_prefix):
            continue
        name = ref.removeprefix('card:').removeprefix(manifest.loop.receipt_prefix)
        if (manifest.paths.root/'loops'/name/'STOP').exists():
            continue
        overdue.add(ref)
        mark=card.get('promise',{}).get('at') or card.get('past_promise_at') or 'overdue'
        entry=state['loops'].setdefault(ref,{'mark':mark,'since':at})
        if entry['mark'] != mark:
            entry={'mark':mark,'since':at}; state['loops'][ref]=entry
        try:
            since = parse_ts(mark).timestamp()
            # Unknown or future promise clocks still make progress from first observation.
            since = min(since, entry.setdefault('since', at))
        except (ValueError, TypeError):
            since = entry.setdefault('since', at)
        settings, _ = loop.config(manifest, name)
        missed = max(0, at-since) / duration(settings.default_cadence)
        token=ref+':'+mark
        def wake(ref=ref,card=card,token=token):
            notify(['post','--from','coordkit','--to',card.get('owner','lead').removeprefix('actor:'),
                      '--kind','handoff','--subject',ref,'--evidence','card:'+ref.removeprefix('card:'),
                      '--id','reconcile-loop-'+hashlib.sha256(token.encode()).hexdigest()[:24],
                      '--force',f'Loop receipt past promise: {ref}; run one cycle and record evidence.'],manifest)
        action('loops','wake:'+token,ref,wake)
        if missed >= settings.fail_report_after:
            ask('loops','loop-ask:'+token,ref,f'{ref} has missed its receipt for {missed:.1f} cadences.',
                'Inspect the loop runner and restore cadence receipts.')
    state['loops']={ref:value for ref,value in state['loops'].items() if ref in overdue}
    print('RECONCILE loops checked cadence receipts')
    status=actl.call('anx','status')
    for letter in deadletters.ours(manifest, status.get('dead_letters',[])):
        if letter.get('reason') == 'card_unresolved':
            key='dead-letter:'+letter['execution_id']+':'+letter.get('phase','')
            action('dead_letters',key,letter['execution_id'],lambda key=key,letter=letter:notify(
                ['post','--from','coordkit','--to','lead','--kind','fyi','--force','--id',hashlib.sha256(key.encode()).hexdigest(),
                 f'agentctl card_unresolved dead letter: {letter["execution_id"]} phase={letter.get("phase", "")}'],manifest))
    print('RECONCILE dead_letters checked agentctl spool')
    print('RECONCILE review checked merged PRs')
    # Adoption baseline (one per project, set on the first live run): rows from before coordkit took over a
    # shared bus are the incumbent's history. Rulings, blockers and holds raise asks/wakes only from it on
    adopted = state.setdefault('obligations_since', at)
    # coordkit's own rows always count; other writers' rows (an incumbent still posting) only after adoption.
    since_adoption = lambda row: bus.adopted_row(row, adopted)
    derived = bus.closure_view(rows, at)
    rulings = [row for row in derived['open'] if row.get('kind') == 'ruling']
    blockers = [row for row in derived['open'] if row.get('kind') == 'blocker']
    open_obligations = {row['id'] for row in rulings + blockers}
    # Group the complete current orphan set, including already notified members:
    # grouping only pending rows would shrink/repost the batch on the next pass.
    from .addressed import digest_only, to_lead_digest, prune_orphan_digests
    eligible_orphans = [row for row in derived['orphaned'] if since_adoption(row)]
    groups = {}
    for row in eligible_orphans:
        groups.setdefault(row['fallback'], []).append(row)
    pending_batches = []
    for target, members in sorted(groups.items()):
        members.sort(key=lambda row: row['id'])
        content = [[row['kind'], row['id'], row['owner'],
                    ' '.join((row.get('subject') or '').split()), row['ts']] for row in members]
        digest = hashlib.sha256(json.dumps([target, content, manifest.reconciler.orphan_batch_lines],
                                          separators=(',', ':')).encode()).hexdigest()
        if orphan_batches.get(target, {}).get('digest') == digest:
            continue
        attempt = orphan_pending.get(target, {})
        batch_at = attempt['at'] if attempt.get('digest') == digest else at
        lines = [f'Orphan backlog for {target}: {len(members)} items; batch {digest}.']
        lines += [f'- {kind} {rid}: owner {owner}; age={int(max(0, batch_at - parse_ts(stamp).timestamp()) // 60)}m'
                  + (f'; subject {subject[:120]}' if subject else '')
                  for kind, rid, owner, subject, stamp in content[:manifest.reconciler.orphan_batch_lines]]
        omitted = max(0, len(members) - manifest.reconciler.orphan_batch_lines)
        lines.append(f'{omitted} more. Inspect bus open-rulings --orphaned and '
                     'bus open-blockers --orphaned. Reassign, answer or close them.')
        pending_batches.append((target, members, digest, '\n'.join(lines), batch_at))
    findings = findings or bool(pending_batches)
    # This is a recipient cursor, deliberately separate from the legacy orphan-ID
    # cursor. Persist before attempting delivery so an abort cannot starve others.
    pending_batches.sort(key=lambda batch: (batch[0] <= state.get('orphan_recipient_cursor', ''), batch[0]))
    for target, members, digest, text, batch_at in pending_batches[:manifest.reconciler.orphan_ask_cap]:
        state['orphan_recipient_cursor'] = target
        findings = True
        print(f'RECONCILE orphans {"would_fix" if dry_run else "fix"} {target} count={len(members)}')
        if dry_run:
            continue
        # Freeze displayed age before publication: a delayed retry must reuse the
        # same text as well as the same post ID, including after a publish/receipt crash.
        orphan_pending[target] = {'digest': digest, 'at': batch_at}
        write_json(state_path, state)
        key = 'orphan-batch:' + target
        post_id = 'orphan-' + hashlib.sha256((target + ':' + digest).encode()).hexdigest()[:24]
        try:
            if digest_only(manifest, target):
                to_lead_digest(manifest, key, text, row={'id': post_id}, at=at,
                               receipt={'target': target, 'digest': digest,
                                        'members': [{'id': row['id'], 'kind': row['kind']} for row in members]})
                # Queuing is not delivery. The flush receipts only rendered batches.
                continue
            else:
                bus.main(['post', '--from', 'coordkit', '--to', target, '--kind', 'ask',
                          '--topic', 'coordkit-orphan', '--id', post_id, '--key', key,
                          '--no-auto-default', '--no-mirror', '--force', text], manifest)
        except (ToolError, ManifestError) as exc:
            print(f'RECONCILE orphans escalation_failed {target}: {exc}')
            continue
        # Atomic batch + member receipts only after delivery. A crash before this
        # write retries the same post ID (or replaces the same digest entry).
        orphan_batches[target] = {'digest': digest, 'id': post_id, 'at': at}
        orphan_pending.pop(target, None)
        for row in members:
            orphan_notified[row['id']] = {'kind': row['kind'], 'fallback': target, 'at': at}
        write_json(state_path, state)
    awaiting_orphans = [row for row in eligible_orphans
                        if orphan_notified.get(row['id'], {}).get('fallback') != row['fallback']]
    summary = ''
    if eligible_orphans:
        findings = findings or bool(awaiting_orphans)
        summary = (f'Orphan backlog: {len(awaiting_orphans)} awaiting fallback; '
                   f'per-pass recipient cap={manifest.reconciler.orphan_ask_cap}; '
                   'sample IDs: ' + ', '.join(row['id'] for row in awaiting_orphans[:6]) +
                   '. Inspect bus open-rulings --orphaned and open-blockers --orphaned.')
        print('RECONCILE orphans ' + summary)
    # Refresh the replaceable snapshot after the derived set drains too. Empty text
    # drops the prior count so a close, reassignment, or revival cannot flush stale IDs.
    if not dry_run:
        for target in orphan_pending.keys() - groups.keys():
            orphan_pending.pop(target)
        prune_orphan_digests(manifest, groups)
        to_lead_digest(manifest, 'orphan-backlog', summary if awaiting_orphans else '', at=at)
    if eligible_orphans and not dry_run:
        # Once-only receipt retention deliberately survives closed rows and fired GC.
        print(f'RECONCILE orphans count={len(eligible_orphans)} awaiting_fallback={len(awaiting_orphans)}')
    closed = False
    for key in list(unresolved):
        if key not in open_obligations:
            del unresolved[key]
            closed = True
    if closed and not dry_run:
        write_json(state_path, state)
    # Recheck missing cards hourly even when their action receipts have fired.
    # Mapping failures (card=None) retry mapping; action receipts still suppress
    # repeated asks/wakes. Per-pass lookups dedupe shared refs across all rows.
    open_rows = {row['id']: row for row in rulings + blockers}
    for key, record in list(unresolved.items()):
        due = (record.get('scope') != cache_scope or record.get('card') is None
               or 'checked_at' not in record or at-record['checked_at'] >= MISSING_CARD_REVERIFY)
        if key in unresolved and key in open_rows and due:
            obligation_card(open_rows[key])
    for row in rulings:
        if not since_adoption(row):
            continue
        if 'ruling:'+row['id'] not in fired and at-parse_ts(row['ts']).timestamp() >= duration(manifest.reconciler.ruling_ask_after):
            card=obligation_card(row)
            ask('rulings','ruling:'+row['id'],card,f'Ruling {row["id"]} has not been acknowledged by {row["owner"]}.',
                'Obtain the owner acknowledgement or reassign this ruling.')
    def wake_obligation(kind, key, row, card, recommendation):
        action(kind, 'wake:'+key, card, lambda:notify(
            ['post','--from','coordkit','--to',row.get('owner') or row['frm'], '--kind','handoff',
             '--subject',card,'--evidence','bus:'+row['id'],'--no-mirror', '--force',
             '--id','wake-'+hashlib.sha256(key.encode()).hexdigest()[:24], recommendation],manifest))
    open_ids = set()
    for row in blockers:
        if not since_adoption(row) or at-parse_ts(row['ts']).timestamp() < duration(manifest.reconciler.blocker_ask_after):
            continue
        key = row['id']; open_ids.add(key)
        if 'wake:blocker:'+key in fired and 'blocker:'+key in fired:
            continue
        passes = state['blockers'].get(key, 0)+1
        # A delivered wake needs no card lookup while waiting for the ask threshold.
        if 'wake:blocker:'+key in fired and passes < manifest.loop.fail_report_after:
            state['blockers'][key] = passes
            if not dry_run:
                write_json(state_path, state)
            continue
        card = obligation_card(row)
        recommendation = f'Answer or disposition blocker {key} with a decision --re {key} and a reason.'
        wake_obligation('blockers', 'blocker:'+key, row, card, recommendation)
        state['blockers'][key] = passes
        if not dry_run:
            write_json(state_path, state)
        if state['blockers'][key] >= manifest.loop.fail_report_after:
            ask('blockers', 'blocker:'+key, card, f'Blocker {key} remains open past its maximum age.', recommendation)
    state['blockers'] = {key:value for key,value in state['blockers'].items() if key in open_ids}
    def warn_hold_text(pr, row):
        key = f'hold-text:{row.get("id", "")}:{pr}'
        if key not in fired:
            # Classifying holds also happens in the stock pass below. Share durable receipts.
            print(f'MERGE_HOLD_TEXT_IGNORED {row.get("id", "")} by={row.get("frm", "")} pr={pr} '
                  '(free text no longer holds; post `bus post --kind hold --pr N[,N]`)')
            fired[key] = at

    active_holds = held(rows, warn_hold_text)
    active_pairs = {(pr,poster) for pr,poster,_ in active_holds}
    latest_holds = {(str(pr),row['frm']):row for row in rows if row.get('kind') == 'hold' for pr in row.get('prs',[])}
    card_handles = {card.get('handle') for card in cards if not card.get('resolution')}
    for pair, row in latest_holds.items():
        pr, poster = pair
        handle = f'hold-pr-{pr}-{poster}'
        if pair not in active_pairs and handle in card_handles:
            action('holds', 'hold-clear:'+row['id']+':'+pr, handle,
                lambda handle=handle:client.call('cards','resolve','card:'+handle,
                    '--reason','Released on the authoritative bus',
                    '--body',f"Hold released on the authoritative bus (last hold {row['id']}).",write=True))
    hold_ids = set()
    for pr, poster, row in active_holds:
        if not since_adoption(row) or at-parse_ts(row['ts']).timestamp() < duration(manifest.merge.max_hold_age):
            continue
        key = pr+':'+poster+':'+row['id']; hold_ids.add(key)
        card = 'card:hold-pr-'+pr+'-'+poster if row['kind'] == 'hold' else manifest.escalation.card
        recommendation = f'Inspect hold {row["id"]}; if safe, run coordkit bus post --from {poster} --to lead --kind unhold --pr {pr} with a reason.'
        wake_obligation('holds', 'hold:'+key, dict(row,owner=poster), card, recommendation)
        state['holds'][key] = state['holds'].get(key, 0)+1
        if state['holds'][key] >= manifest.loop.fail_report_after:
            ask('holds', 'hold:'+key, card, f'Hold by {poster} on PR {pr} exceeded max_hold_age.', recommendation)
    state['holds'] = {key:value for key,value in state['holds'].items() if key in hold_ids}
    if not dry_run:
        # Retain answer payloads before projecting so a bus mirror failure is restartable.
        pending_path=manifest.paths.root/'reconcile/answers.json'
        pending=read_json(pending_path,[])
        try:
            answers=client.call('await','--answers','--timeout','1s','--settle','0').get('answers',[])
        except ToolError as exc:
            if exc.exit_code != 8:
                raise
            answers=[]
        deferred = loop_answers.deferred(manifest)
        known_answers={item.get('ask_id') for item in pending}
        for item in [item for _, item in deferred] + answers:
            if item.get('ask_id') not in known_answers:
                pending.append(item)
                known_answers.add(item.get('ask_id'))
        for item in pending:
            item.setdefault('received_at', at)
        from . import briefs, questions as _questions
        question_asks = {r['anx_ask_id']: r for r in _questions.load_all(manifest).values() if r.get('anx_ask_id')}
        if pending:
            write_json(pending_path,pending)
        for received_path, _ in deferred:
            received_path.unlink(missing_ok=True)  # only after answers.json is durable
        for item in list(pending):
            from . import answers as answer_delivery
            try:
                item['ask_id'] = answer_delivery.reference(item['ask_id'])
            except (ValueError, KeyError, TypeError):
                answer_delivery.reject(manifest, item)
                pending.remove(item)
                write_json(pending_path, pending)
                continue
            answer = item.get('answer')
            if isinstance(answer, dict) and (answer.get('response_event_id') or answer.get('response_event_ref')):
                if answer_delivery.shared_answer(manifest, item, client):
                    pending.remove(item)
                    write_json(pending_path, pending)
                continue
            if answer_delivery.already_delivered(manifest, item) or any(row.get('id') == 'answer-'+item['ask_id'] for row in bus.read()):
                pending.remove(item)
                write_json(pending_path,pending)
                continue
            subject=item.get('subject_ref') or item.get('subject',{}).get('ref')
            if loop_answers.receive(manifest, item):
                pending.remove(item)
                write_json(pending_path, pending)
                continue
            if item['ask_id'] in question_asks:
                # A question's answer: hand it to the question ladder now (this read may have consumed it from the
                # shared answer stream), never a blocker correlation.
                if not dry_run:
                    record=question_asks[item['ask_id']]
                    if record['state'] in ('open','escalated'):
                        _questions.relay_anx_answer(manifest, record, item.get('answer') or item)
                    pending.remove(item)
                    write_json(pending_path,pending)
                continue
            candidates=[r for r in bus.unresolved_blockers(bus.read()) if r.get('subject') == subject and r.get('owner') == 'lead']
            if len(candidates) > 1 and item.get('title'):
                candidates=[r for r in candidates if item['title'] in (briefs.blocker_title(r), ('Coordkit: '+r['text'])[:99].rstrip('.?')+'?')]
            if len(candidates) != 1:
                print(f'RECONCILE answers ambiguous {item["ask_id"]}; retained for correlation')
                findings=True
                if at-item['received_at'] >= duration(manifest.reconciler.answer_max_age):
                    ask('answers', 'ambiguous:'+item['ask_id'], subject,
                        f'Answer {item["ask_id"]} cannot be correlated: {json.dumps(item,sort_keys=True)}',
                        f'Correlate the exact blocker, or run coordkit reconcile abandon-answer {item["ask_id"]} --reason REASON.')
                continue
            original=candidates[0] if candidates else None
            if original:
                answer=item.get('answer') or {}
                action('answers','answer:'+item['ask_id'],item['ask_id'],lambda item=item,answer=answer,original=original:notify(
                    ['post','--from','coordkit','--to',original['frm'],'--kind','decision','--subject',subject,
                     '--re',original['id'],'--rollback','Follow up on the original ANX ask to revise this answer.',
                     '--id','answer-'+item['ask_id'],'--force',str(answer.get('text') or answer.get('outcome') or 'ANX answer received')],manifest))
            pending.remove(item)
            write_json(pending_path,pending)
    print('RECONCILE rulings checked acknowledgements')
    cursor=manifest.paths.root/'state/mirror.cursor'
    start=int(cursor.read_text()) if cursor.exists() else 0
    if manifest.bus.mirror and start < len(bus.read()):
        def remirror():
            # Owner wakes deliberately suppress projection; replay must reset that mode.
            bus.configure(manifest)
            lock_path=manifest.paths.root/'state/bus.lock'
            lock_path.parent.mkdir(parents=True,exist_ok=True)
            with lock_path.open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                bus.append([])
        mirror_key='mirror:'+str(len(bus.read()))
        fired.pop(mirror_key, None)  # the cursor, rather than a fired-once key, proves projection completion
        try:
            action('mirror',mirror_key,f'cursor={start}',remirror)
            state['mirror_failures'] = 0
            fired.pop('mirror-failure', None)
        except (ToolError, OSError, ValueError, SystemExit) as exc:
            findings = True
            state['mirror_failures'] = state.get('mirror_failures', 0)+1
            print(f'RECONCILE mirror failed consecutive={state["mirror_failures"]}: {exc}')
            if state['mirror_failures'] >= manifest.loop.fail_report_after:
                ask('mirror','mirror-failure',None,
                    f'Bus mirror failed {state["mirror_failures"]} consecutive reconciliations at cursor {start}.',
                    'Repair ANX projection; retained bus rows will be replayed without dropping obligations.')
        fired.pop(mirror_key, None)
    else:
        state['mirror_failures'] = 0
        fired.pop('mirror-failure', None)
    dead_path=manifest.paths.root/'state/mirror-dead-letters.jsonl'
    dead=len(dead_path.read_text().splitlines()) if dead_path.exists() else 0
    if dead > state.get('mirror_dead_seen', 0):
        ask('mirror', f'mirror-dead:{dead}', None,
            f'{dead - state.get("mirror_dead_seen", 0)} bus rows could not be projected into ANX because their card does not exist; they were set aside so the rest kept syncing.',
            'Read the mirror dead-letter list; create or map the missing cards, or accept the gap.')
        if not dry_run:
            state['mirror_dead_seen'] = dead
    print('RECONCILE mirror checked projection lag')
    for gitdir in manifest.reconciler.shared_git_dirs:
        # Read the file, not `git -C`: a set core.worktree redirects git -C itself.
        found = run(['git','config','--file',str(manifest.path(gitdir))+'/config','--get','core.worktree'])
        if found.returncode == 0 and found.stdout.strip():
            value = found.stdout.strip()
            ask('git', 'core-worktree:'+hashlib.sha256((gitdir+value).encode()).hexdigest(), None,
                f'core.worktree={value} is set in the shared repo {gitdir}; every linked worktree now reads and writes that one tree.',
                f'Back up {gitdir}/config, then `git config --file {gitdir}/config --unset core.worktree`; find who exported GIT_DIR/GIT_WORK_TREE into a nested git init.')
        elif found.returncode not in (0, 1):
            ask('git', 'core-worktree-unknown:'+gitdir, None, f'Cannot read core.worktree in {gitdir}: {found.stderr.strip()}',
                'Check the shared repo path in [reconciler].shared_git_dirs.')
    print('RECONCILE git checked shared repo core.worktree')
    from .lifecycle import installation_drift
    for detail in installation_drift(manifest):
        ask('drift', 'drift:'+hashlib.sha256(detail.encode()).hexdigest(), None,
            detail, 'Compare jobs, registered lead hooks and ANX loop cards/cadences to coordkit up --plan; repair the intended configuration explicitly.')
    stocks = report(manifest, 'RECONCILE', at, since=state['obligations_since'], hold_warning=warn_hold_text, persist=not dry_run)
    for kind, item in stocks.items():
        if item['count'] >= item['threshold']:
            ask('stock', 'stock:'+kind, None,
                f'{kind} stock count={item["count"]} reached threshold={item["threshold"]}.',
                f'Inspect coordkit obligations report and disposition the oldest {kind} obligations.')
        else:
            fired.pop('stock:'+kind, None)
    if not dry_run:
        state['last_completed'] = at + time.monotonic() - started
        write_json(state_path,state)
    return 5 if findings else 0


def main(argv,manifest):
    parser=argparse.ArgumentParser(prog='coordkit reconcile')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('command', nargs='?', choices=['run','abandon-answer','forget-missing'], default='run')
    parser.add_argument('answer_id', nargs='?')
    parser.add_argument('--all', action='store_true', dest='all_missing')
    parser.add_argument('--reason')
    args=parser.parse_args(argv)
    try:
        if args.command == 'forget-missing':
            manifest.writable()
            if bool(args.answer_id) == args.all_missing or args.dry_run or args.reason:
                parser.error('forget-missing requires exactly one card ref or --all; no --dry-run/--reason')
            ref = 'card:'+args.answer_id.removeprefix('card:') if args.answer_id else None
            directory=manifest.paths.root/'reconcile'; directory.mkdir(parents=True,exist_ok=True)
            with (directory/'run.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                state_path=directory/'state.json'
                state=read_json(state_path, {'fired':{}, 'dead':{}, 'loops':{}})
                missing = state.setdefault('missing_cards', {})
                removed = 0
                for key, entry in list(missing.items()):
                    if args.all_missing or entry.get('ref', key) == ref:
                        del missing[key]
                        removed += 1
                # Retain the diagnostic until a real successful lookup, but force
                # even handled rows to retry on the next pass, without re-asking.
                for record in state.get('unresolved_obligations', {}).values():
                    if args.all_missing or record.get('card') == ref:
                        record.pop('checked_at', None)
                write_json(state_path, state)
            print(f'RECONCILE forget_missing removed={removed}')
            return 0
        if args.all_missing:
            parser.error('--all is only supported by forget-missing')
        if args.command == 'abandon-answer':
            manifest.writable()
            if not args.answer_id or not args.reason or args.dry_run:
                parser.error('abandon-answer requires answer_id and --reason; no --dry-run')
            directory=manifest.paths.root/'reconcile'; directory.mkdir(parents=True,exist_ok=True)
            with (directory/'run.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                path=directory/'answers.json'
                pending=read_json(path,[])
                answer_ref = loop_answers.reference(args.answer_id)
                for item in pending:
                    if loop_answers.reference(item['ask_id']) == answer_ref:
                        AnxClient(manifest).note(item.get('subject_ref') or manifest.escalation.card,
                            f'Abandoned answer {args.answer_id}: {args.reason}')
                write_json(path,[item for item in pending if loop_answers.reference(item['ask_id']) != answer_ref])
            return 0
        if args.dry_run:
            return reconcile(manifest,True)
        manifest.writable()
        directory=manifest.paths.root/'reconcile'; directory.mkdir(parents=True,exist_ok=True)
        with (directory/'run.lock').open('a') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Separate receipt avoids overwriting the active reconciler's state snapshot.
                write_json(directory/'skip.json', {'at':time.time(), 'reason':'run_lock_held'})
                print('RECONCILE skipped run_lock_held')
                return 4
            state_path=directory/'state.json'
            state=read_json(state_path, {'fired':{}, 'dead':{}, 'loops':{}})
            state['last_run']=time.time()
            write_json(state_path,state)
            return reconcile(manifest)
    except (ToolError,OSError,ValueError,SystemExit) as exc:
        print(f'RECONCILE tool failure {exc}')
        return 6
