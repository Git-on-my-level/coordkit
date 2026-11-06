"""Recorded lead-hook transformation with an independent, reversible receipt."""
from collections import Counter
import copy
import json
from .paths import env_args, python_env
from pathlib import Path
import shlex

from .manifest import ManifestError
from .state import read_json, write_json, update_json


def receipt_path(manifest):
    return manifest.paths.root/'hook-rollback.json'


def entries(doc):
    """Ignore ordering and unrelated settings; retain event and matcher boundaries."""
    result = Counter()
    for event, groups in doc.get('hooks',{}).items():
        for group in groups:
            metadata = {k:v for k,v in group.items() if k != 'hooks'}
            for hook in group.get('hooks',[]):
                result[json.dumps([event,metadata,hook],sort_keys=True)] += 1
    return result


def command(manifest, verb):
    # perl alarm bounds the lead hooks at 5s like the incumbent's `timeout 5` (fail open: a killed ask-guard prints
    # no decision, so AskUserQuestion proceeds; a killed Stop pending only loses its summary line).
    base = ['perl','-e','alarm 5; exec @ARGV',*env_args(),manifest.launchd.python,
            '-m','coordkit.cli','--manifest',str(manifest.file),'wait',verb]
    return shlex.join(base+(['--brief'] if verb == 'pending' else []))


def transformation(manifest, original, fresh_ok=False):
    """Swap the incumbent lead_wait pending/ask-guard pair for coordkit's. fresh_ok: a config dir with no incumbent
    pair gets coordkit's pair added instead; never a second copy."""
    doc = copy.deepcopy(original)
    doc.setdefault('hooks', {})
    pending = guard = 0
    for event, groups in doc['hooks'].items():
        for group in groups:
            kept = []
            for hook in group.get('hooks',[]):
                value = hook.get('command','')
                if 'lead_wait.py' in value and 'pending --brief' in value:
                    if event != 'UserPromptSubmit':
                        raise ManifestError('unexpected_incumbent_pending_event')
                    pending += 1
                    continue
                if 'lead_wait.py' in value and 'ask-guard' in value:
                    if event != 'PreToolUse' or group.get('matcher') != 'AskUserQuestion':
                        raise ManifestError('unexpected_incumbent_guard_event')
                    guard += 1
                    hook = dict(hook,command=command(manifest,'ask-guard'))
                kept.append(hook)
            group['hooks'] = kept
    fresh = fresh_ok and (pending,guard) == (0,0)
    if (pending,guard) != (1,1) and not fresh:
        raise ManifestError('incumbent_hook_count: expected one pending and one ask-guard')
    if fresh:
        present = [h.get('command','') for groups in doc['hooks'].values() for g in groups for h in g.get('hooks',[])]
        if any('coordkit.cli' in c and (' wait ask-guard' in c or ' wait pending' in c) for c in present):
            raise ManifestError('lead_hooks_already_present')
        doc['hooks'].setdefault('PreToolUse',[]).append({'matcher':'AskUserQuestion','hooks':[
            {'type':'command','command':command(manifest,'ask-guard')}]})
    doc['hooks'].setdefault('Stop',[]).append({'hooks':[
        {'type':'command','command':command(manifest,'pending')}]})
    return doc


def install(manifest, target, backup, fresh_ok=False):
    """Runbook transaction: persist independent recovery data before editing hooks."""
    manifest.writable()
    target, backup = Path(target).resolve(), Path(backup).resolve()
    if receipt_path(manifest).exists():
        raise ManifestError('hook_receipt_exists: retain original rollback evidence')
    original = backup.read_bytes()
    if target.read_bytes() != original:
        raise ManifestError('hooks_changed_since_backup')
    before = json.loads(original)
    expected_receipt = None
    def transform(current):
        if current != before:
            raise ManifestError('hooks_changed_since_backup')
        return transformation(manifest, current, fresh_ok)
    def prepare(current, after):
        nonlocal expected_receipt
        if read_json(receipt_path(manifest)) != expected_receipt:
            raise ManifestError('hook_receipt_exists: retain original rollback evidence')
        expected_receipt = {'target':str(target),'backup':str(backup),
                            'before':current,'after':after,'restored':False}
        write_json(receipt_path(manifest), expected_receipt)
    update_json(target, transform, before_replace=prepare)


def reverse(record, current):
    """Undo only changed hook entries; unrelated current settings survive rollback."""
    if current == record['after']:
        return copy.deepcopy(record['before'])
    before, after = entries(record['before']), entries(record['after'])
    remove, restore = after-before, before-after
    now = entries(current)
    # Retrying after a lost restoration receipt is safe. Partial or edited managed
    # entries require inspection, never a guessed overwrite of user settings.
    if all(now[key] == before[key] for key in remove.keys() | restore.keys()):
        return current
    if any(now[key] != after[key] for key in remove.keys() | restore.keys()):
        raise ManifestError('hook_rollback_conflict: managed hooks changed; inspect saved receipt')
    doc = copy.deepcopy(current)
    for key,count in remove.items():
        event,metadata,hook = json.loads(key)
        for group in doc['hooks'][event]:
            if {k:v for k,v in group.items() if k != 'hooks'} == metadata:
                while count and hook in group.get('hooks',[]):
                    group['hooks'].remove(hook)
                    count -= 1
    for key,count in restore.items():
        event,metadata,hook = json.loads(key)
        groups = doc['hooks'].setdefault(event,[])
        candidates = [g for g in groups if {k:v for k,v in g.items() if k != 'hooks'} == metadata]
        if len(candidates) > 1:
            raise ManifestError('hook_rollback_conflict: ambiguous original hook group')
        if not candidates:
            groups.append(dict(metadata,hooks=[]))
            candidates = [groups[-1]]
        candidates[0].setdefault('hooks',[]).extend([hook]*count)
    if entries(doc) != now-remove+restore:
        raise ManifestError('hook_rollback_verification_failed')
    return doc


def restore(manifest):
    path = receipt_path(manifest)
    if not path.exists():
        return
    record = read_json(path)
    if record.get('restored'):
        verify_restored(manifest,read_json(Path(record['target'])))
        return
    target = Path(record['target'])
    current = read_json(target)
    restored = reverse(record,current)
    verify_restored(manifest,restored)
    write_json(target,restored)
    if read_json(target) != restored:
        raise ManifestError('hook_rollback_verification_failed')
    record['restored'] = True
    write_json(path,record)


def verify_restored(manifest, doc):
    for key in entries(doc):
        event,metadata,hook = json.loads(key)
        argv = shlex.split(hook.get('command',''))
        if not any('coordkit' in token for token in argv) or 'wait' not in argv:
            continue
        for index,token in enumerate(argv):
            value = argv[index+1] if token == '--manifest' and index+1 < len(argv) else token.removeprefix('--manifest=') if token.startswith('--manifest=') else None
            if value and Path(value).expanduser().resolve() == manifest.file:
                raise ManifestError('hook_rollback_conflict: coordkit waiter hook remains '+event)


def drift(manifest):
    path = receipt_path(manifest)
    if not path.exists():
        return []
    record = read_json(path)
    if record.get('restored'):
        return []
    before, after = entries(record['before']), entries(record['after'])
    current = entries(read_json(Path(record['target'])))
    problems = []
    if any(current[key] != after[key] for key in (after-before).keys() | (before-after).keys()):
        problems.append('registered lead hook transformation drift')
    for key in current:
        event,metadata,hook = json.loads(key)
        value = hook.get('command','')
        if 'lead_wait.py' in value and ('pending' in value or 'ask-guard' in value):
            problems.append('incumbent lead hook remains '+event)
        if 'coordkit.cli' in value and 'wait' in value:
            verb = 'ask-guard' if 'ask-guard' in value else 'pending' if 'pending' in value else None
            if verb and value != command(manifest,verb):
                problems.append('lead hook command drift '+event)
    return problems


# -- question hooks (the question escalation contract) -------------------------------------------------
# One receipt per harness config dir; the same reverse() rolls them back, preserving unrelated hooks.
QUESTION_EVENTS = (('PreToolUse', 'AskUserQuestion', 'pretooluse'), ('Stop', None, 'stop'),
                   ('Notification', None, 'notification'))


def dir_tag(config_dir):
    return Path(config_dir).expanduser().name.lstrip('.') or 'root'


def question_receipt(manifest, config_dir):
    return manifest.paths.root/f'hook-questions-{dir_tag(config_dir)}.json'


def question_command(manifest, event, config_dir):
    # perl alarm bounds the hook at 5s (fail open: the CLI exits 0 on any error, and a killed hook
    # prints no decision, so the tool call proceeds).
    base = ['perl', '-e', 'alarm 5; exec @ARGV', *env_args(),
            manifest.launchd.python, '-m', 'coordkit.cli', '--manifest', str(manifest.file), 'question', 'hook', event,
            '--config-dir', str(Path(config_dir).expanduser())]
    return shlex.join(base)


def is_question_hook(command_text):
    return 'coordkit.cli' in command_text and ' question hook ' in f' {command_text} '


def question_transformation(manifest, original, config_dir):
    doc = copy.deepcopy(original)
    hooks = doc.setdefault('hooks', {})
    for groups in hooks.values():
        for group in groups:
            if any(is_question_hook(h.get('command', '')) for h in group.get('hooks', [])):
                raise ManifestError('question_hooks_present: inspect before installing again')
    for event, matcher, verb in QUESTION_EVENTS:
        group = {'hooks': [{'type': 'command', 'command': question_command(manifest, verb, config_dir)}]}
        if matcher:
            group = {'matcher': matcher, **group}
        hooks.setdefault(event, []).append(group)
    return doc


def install_questions(manifest, config_dir, backup):
    """Recorded transaction for one config dir: receipt first, then the settings write."""
    manifest.writable()
    target = (Path(config_dir).expanduser()/'settings.json').resolve()
    backup = Path(backup).resolve()
    receipt = question_receipt(manifest, config_dir)
    if receipt.exists() and not read_json(receipt).get('restored'):
        raise ManifestError('hook_receipt_exists: retain original rollback evidence')
    original = backup.read_bytes()
    if target.read_bytes() != original:
        raise ManifestError('hooks_changed_since_backup')
    before = json.loads(original)
    after = question_transformation(manifest, before, config_dir)
    write_json(receipt, {'target': str(target), 'backup': str(backup), 'before': before, 'after': after,
                         'restored': False})
    write_json(target, after)


def restore_questions(manifest):
    for receipt in sorted(manifest.paths.root.glob('hook-questions-*.json')):
        record = read_json(receipt)
        if record.get('restored'):
            continue
        target = Path(record['target'])
        restored = reverse(record, read_json(target))
        if any(is_question_hook(json.loads(k)[2].get('command', '')) for k in entries(restored)):
            raise ManifestError('hook_rollback_conflict: a question hook remains in '+str(target))
        write_json(target, restored)
        if read_json(target) != restored:
            raise ManifestError('hook_rollback_verification_failed')
        record['restored'] = True
        write_json(receipt, record)


def question_drift(manifest, include_pending=True):
    """Managed config dirs must carry exactly the three question hooks once coordkit is up: each event,
    the AskUserQuestion matcher and the exact command. Before `up --apply` installs them nothing is
    reported (up prints UP_DIFF and installs them under a receipt)."""
    problems = []
    if manifest.mode != 'live':
        return problems
    lifecycle = read_json(manifest.paths.root/'lifecycle.json', {}) or {}
    for config_dir in manifest.questions.config_dirs:
        receipt = read_json(question_receipt(manifest, config_dir), None)
        active = receipt is not None and not receipt.get('restored')
        if not active and (not lifecycle.get('jobs') or not include_pending):
            continue  # not installed yet: `up --apply` installs it (UP_DIFF), so it is not drift
        target = Path(config_dir).expanduser()/'settings.json'
        doc = read_json(target, {}) or {}
        for event, matcher, verb in QUESTION_EVENTS:
            want = question_command(manifest, verb, config_dir)
            found = [g for g in doc.get('hooks', {}).get(event, [])
                     if g.get('matcher') == matcher and any(h.get('command') == want for h in g.get('hooks', []))]
            if not found:
                problems.append(f'question hook missing or changed in {target}: {event}'
                                + (f' matcher={matcher}' if matcher else ''))
        stray = [json.loads(k)[2].get('command') for k in entries(doc) if is_question_hook(json.loads(k)[2].get('command', ''))
                 and json.loads(k)[2].get('command') not in {question_command(manifest, v, config_dir) for _, _, v in QUESTION_EVENTS}]
        if stray:
            problems.append(f'unexpected question hook in {target}')
    return problems


def pending_question_installs(manifest):
    """Config dirs whose question hooks `up --apply` still has to install."""
    out = []
    for config_dir in manifest.questions.config_dirs:
        receipt = read_json(question_receipt(manifest, config_dir), None)
        if receipt is None or receipt.get('restored'):
            out.append(config_dir)
    return out


def reconcile(manifest):
    """Roll managed commands forward; preserve original rollback and unrelated edits.

    A pending receipt bridges a crash between the receipt and settings writes.
    User changes to any managed entry are a conflict, never an overwrite.
    """
    from . import deliver
    import time
    manifest.writable()
    receipts = [receipt_path(manifest), *sorted(manifest.paths.root.glob('hook-questions-*.json')),
                *sorted(manifest.paths.root.glob('hook-delivery-*.json'))]
    changed = []
    for path in receipts:
        record = read_json(path)
        if not record or record.get('restored'):
            continue
        target = Path(record['target'])
        if record.get('pending_after'):
            pending = record['pending_after']
            original = copy.deepcopy(record)
            def prepare_recovery(before, after):
                if read_json(path) != original:
                    raise ManifestError('hook_receipt_conflict: migration changed ' + str(target))
            def finish_recovery(before, after):
                record['after'] = record.pop('pending_after')
                write_json(path, record)
            update_json(target, lambda current: reverse({'before': pending, 'after': original['after']}, current),
                        before_replace=prepare_recovery, after_replace=finish_recovery)
        current = read_json(target, {})
        before, after = entries(record['before']), entries(record['after'])
        now = entries(current)
        if any(now[key] != after[key] for key in (after-before).keys() | (before-after).keys()):
            raise ManifestError('hook_rollback_conflict: managed hooks changed ' + str(target))
        replacements = {}
        for key in after-before:
            event, metadata, hook = json.loads(key)
            old = hook.get('command', '')
            if ' deliver --hook' in old:
                new = deliver.command(manifest)
            elif is_question_hook(old):
                verb = next((v for e, m, v in QUESTION_EVENTS if e == event), None)
                if not verb:
                    raise ManifestError('unknown_question_hook_event: ' + event)
                new = question_command(manifest, verb, target.parent)
            elif ' wait pending' in old or ' wait ask-guard' in old:
                new = command(manifest, 'ask-guard' if 'ask-guard' in old else 'pending')
            else:
                continue
            if old != new:
                replacements[old] = new
        if not replacements:
            continue
        def transform(current):
            now = entries(current)
            if any(now[key] != after[key] for key in (after-before).keys() | (before-after).keys()):
                raise ManifestError('hook_rollback_conflict: managed hooks changed ' + str(target))
            updated = copy.deepcopy(current)
            for groups in updated.get('hooks', {}).values():
                for group in groups:
                    for hook in group.get('hooks', []):
                        if hook.get('command') in replacements:
                            hook['command'] = replacements[hook['command']]
            return updated
        # Keep unrelated user settings outside the owned receipt transformation.
        expected = transform(record['after'])
        original = copy.deepcopy(record)
        intent = copy.deepcopy(record)
        intent['pending_after'] = expected
        intent.setdefault('migrations', []).append({'at': time.time(), 'commands': replacements})
        expected_receipt = original
        def prepare(before_doc, after_doc):
            nonlocal expected_receipt
            if read_json(path) != expected_receipt:
                raise ManifestError('hook_receipt_conflict: migration changed ' + str(target))
            write_json(path, intent)
            expected_receipt = intent
        def finish(before_doc, after_doc):
            record.update(intent)
            record['after'] = record.pop('pending_after')
            write_json(path, record)
        update_json(target, transform, before_replace=prepare, after_replace=finish)
        changed.append(str(target))
    return changed


def main(argv, manifest):
    import argparse
    from . import deliver
    from .ops import parser_for
    parser = parser_for('hooks')
    opts = parser.parse_args(argv)
    opts.settings = Path(opts.settings).expanduser() if opts.settings else Path.home()/'.claude/settings.json'
    if opts.action == 'install':
        deliver.install(manifest, opts.settings)
    elif opts.action == 'uninstall':
        deliver.uninstall(manifest, opts.settings)
    else:
        record = read_json(deliver.receipt_path(manifest, opts.settings), {})
        if not record or record.get('restored'):
            print('HOOKS absent')
            return 0
        current = entries(read_json(opts.settings, {}))
        expected = entries(record['after']) - entries(record['before'])
        drift = any(current[key] != count for key, count in expected.items())
        print('HOOKS ' + ('drift' if drift else 'installed coordkit-owned'))
        return 1 if drift else 0
    print('HOOKS_OK ' + opts.action)
    return 0
