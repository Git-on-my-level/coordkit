"""Decision briefs: the only shape in which coordkit may ask the operator anything.

A brief reads well on a phone: a plain title (<= 12 words, ending "?"), a short situation and impact, 2-4 options
each with its consequence and whether it can be undone, one recommendation with its reason, and what happens if
nobody answers. Ids, paths, SHAs and refs never appear in the body; they move to the collapsed details.
`AnxClient.ask` files nothing that `validate` refuses.
"""
import re

MAX_TITLE_WORDS = 12
# Ordinary words written with a slash; every other word/word token counts as a path.
BENIGN_SLASH = '|'.join(['and/or', 'either/or', 'yes/no', 'on/off', 'read/write', 'input/output', 'client/server',
                         'TCP/IP', 'I/O', 'true/false', 'pass/fail', 'start/stop', 'before/after', 'he/she',
                         'his/her', 'w/o', 'n/a', 'A/B', 'open/close', 'up/down', 'in/out', 'buy/sell', 'P/L'])
# Body jargon: moved to details by `scrub`, an error in a title. PR numbers (#123) stay: they name the thing.
JARGON = re.compile(
    r'(?<![\w/])(?:/(?:Users|Volumes|tmp|var|opt|private|etc)/[^\s;,]*[^\s;,.]|~/[^\s;,]*[^\s;,.])'  # absolute and home paths
    r'|\b(?:card|event|path|ask|exec):[\w./:-]+'                                # ANX and coordkit refs
    r'|\bexec-[a-z]+(?:-[a-z]+){2,}\b'                                          # agentctl execution ids
    r'|\bev_[0-9a-f]{6,}\b|\bq-[0-9a-f]{6}\b|\bunknown-[0-9a-f]{6,}\b'          # anx events, question ids, unbound askers
    r'|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{6,40}\b'                     # SHAs, bus and hex ids (needs a digit and a letter)
    r'|\bbus [0-9a-f]{6}\b|\(bus [0-9a-f]{6}\)'                                 # bus row ids
    r'|`[^`]+`'                                                                 # inline code
    r'|\b[\w.-]+(?:/[\w.-]+){2,}\b'                                             # relative paths: a/b/c
    r'|\b(?!(?:' + BENIGN_SLASH + r')\b)[A-Za-z_][\w.-]*/[\w.-]+\b'             # any other word/word: a path (fail closed)
    r'|(?<![\w/])\.{1,2}/[\w./-]*[\w-]'                                         # ./x and ../x
    r'|\b(?!(?:e\.g|i\.e|etc|vs|a\.m|p\.m|U\.S)\b)(?=[\w-]*[A-Za-z_])[\w-]+\.[A-Za-z][A-Za-z0-9+]{0,11}\b(?!\.\d)'  # any name.ext, any case (fail closed)
    r'|(?<![\w.])\.[A-Za-z_][\w.-]*'                                            # hidden files: .env, .gitignore
    r'|(?<![\w/])/[A-Za-z_][\w.-]*'                                             # one-component absolute: /tmp
    r'|\b[A-Za-z_][\w.-]*/(?=[\s,;:).]|$)|(?<!\S)(?:\.{1,2}|~)/(?=[\s,;:).]|$)'  # directories: src/, ./, ../, ~/
    r'|\b[A-Za-z]:\\\S*|\b\w+\\\w[\w\\.-]*'                                    # Windows paths
    r'|(?<![\w-])--[A-Za-z][\w-]*'                                              # command-line flags
    r'|\$[A-Z_][A-Z0-9_]*\b|\$\{[^}]+\}'                                        # environment variables
    r'|\b[A-Za-z_][\w.]*\([^()\s]*\)'                                           # calls: validate(), foo(arg)
    r'|\b\w+::\w+'                                                              # foo::bar
    r'|\b[a-z][a-z0-9]*_[a-z0-9_]+\b'                                           # snake_case identifiers
    r'|\b0x[0-9A-Fa-f]+\b'                                                      # hex literals
    r'|\b(?:id|qid|event_id|run_id|exec_id|ask_id|sha)[:=]\S+'                   # prefixed ids
    r'|\b(?=[0-9A-Fa-f]*\d)(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{6,40}\b'         # hex ids in any case
    r'|\b(?:Makefile|Dockerfile|Procfile|Gemfile|Rakefile|Jenkinsfile|Brewfile|Vagrantfile|Podfile|Justfile)\b')  # files
# Residual, accepted: forms that collide with ordinary language (kebab-case names like follow-up, plain
# numbers, letter-only hex words like decade, non-ASCII names, and a benign pair such as client/server used as a path)
# are not blocked. Tier 1 authors every brief that reaches the operator, and ANX's own lint flags jargon.



def _plain(match):
    token = match.group(0)
    if token.startswith('card:'):
        return f'the {token[5:].rstrip(".")} card'
    if token.startswith(('/', '~')):
        return '(path in details)'
    return ''


def scrub(text):
    """(text with jargon replaced by plain words or removed, whitespace tidied; [removed tokens])."""
    found = [m.group(0) for m in JARGON.finditer(text or '')]
    clean = JARGON.sub(_plain, text or '')
    clean = re.sub(r'\(\s*\)|\[\s*\]', '', clean)
    clean = re.sub(r'\s+([,.;:?!])', r'\1', re.sub(r'[ \t]+', ' ', clean)).strip()
    return clean, found


def sentences(text):
    return [s for s in re.split(r'(?<=[.!?])\s+', (text or '').strip()) if s.strip()]


def validate(brief):
    """[error] for a brief dict; empty when it may reach the operator."""
    errors = []
    if not isinstance(brief, dict):
        return ['no brief']
    title = (brief.get('title') or '').strip()
    if not title:
        errors.append('no title')
    else:
        if len(title.split()) > MAX_TITLE_WORDS:
            errors.append(f'title over {MAX_TITLE_WORDS} words')
        if not title.endswith('?'):
            errors.append('title does not end with "?"')
        if len(title) > 160:
            errors.append('title over 160 characters')
        if JARGON.search(title):
            errors.append('title contains ids or paths')
    asker = (brief.get('asker') or '').strip()
    if not asker or asker.startswith('unknown'):
        errors.append('unknown asker (session not bound to a bus name)')
    situation = (brief.get('situation') or '').strip()
    if not situation:
        errors.append('no situation')
    elif len(sentences(situation)) > 3:
        errors.append('situation over 3 sentences')
    if not (brief.get('impact') or '').strip():
        errors.append('no impact')
    options = brief.get('options') or []
    if not 2 <= len(options) <= 4:
        errors.append('needs 2-4 options')
    for option in options:
        if not isinstance(option, dict) or not (option.get('action') or '').strip() or not (option.get('consequence') or '').strip():
            errors.append('each option needs an action and a consequence'); break
        if len(option['action']) > 240 or len(option['consequence']) > 240:
            errors.append('option over 240 characters'); break
        if not isinstance(option.get('reversible'), bool):
            errors.append('each option must say whether it can be undone (reversible: true/false)'); break
    if not (brief.get('recommendation') or '').strip():
        errors.append('no recommendation')
    if not (brief.get('why') or '').strip():
        errors.append('no reason for the recommendation')
    if not (brief.get('if_no_answer') or '').strip():
        errors.append('no default (what happens if nobody answers)')
    for field in ('situation', 'impact', 'recommendation', 'why', 'if_no_answer'):
        if JARGON.search(brief.get(field) or ''):
            errors.append(f'{field} contains ids or paths (move them to details)')
    if any(JARGON.search(o.get('action', '') + ' ' + o.get('consequence', '')) for o in options if isinstance(o, dict)):
        errors.append('an option contains ids or paths (move them to details)')
    return errors


def clean(brief):
    """Move jargon out of every body field into details; titles are not rewritten (validate refuses them)."""
    out = dict(brief)
    moved = []
    for field in ('situation', 'impact', 'recommendation', 'why', 'if_no_answer'):
        out[field], found = scrub(out.get(field) or '')
        if re.match(r'[a-z]+(?=[\s,;:]|$)', out[field]):  # a plain first word, never a code token like core.worktree
            out[field] = out[field][:1].upper() + out[field][1:]
        moved += found
    options = []
    for option in out.get('options') or []:
        action, a = scrub(option.get('action', ''))
        consequence, c = scrub(option.get('consequence', ''))
        options.append(dict(option, action=action, consequence=consequence))
        moved += a + c
    out['options'] = options
    if moved:
        refs = ', '.join(dict.fromkeys(moved))
        out['details'] = ((out.get('details') or '') + f'\nReferences: {refs}').strip()
    return out


def anx_args(brief):
    """`anx ask` arguments for a validated brief (title first)."""
    args = [brief['title'], '--situation', brief['situation'], '--impact', brief['impact'],
            '--recommend', brief['recommendation'], '--why', brief['why'], '--if-no-answer', brief['if_no_answer']]
    for option in brief['options']:
        undo = option.get('reversible')
        tail = '' if undo is None else (' Can be undone.' if undo else ' Cannot be undone.')
        args += ['--alternative', f"{option['action']}={option['consequence']}{tail}"]
    if brief.get('by'):
        args += ['--by', brief['by']]
    for item in brief.get('evidence') or []:
        args += ['--evidence', f"{item['label']}={item['url']}"]
    if brief.get('details'):
        args += ['--details', brief['details'][:4000]]
    return args


def option(action, consequence, reversible=None):
    return {'action': action, 'consequence': consequence, 'reversible': reversible}


def pr_evidence(manifest, *texts):
    repo = getattr(manifest.project, 'git_repo', '')
    prs = sorted({m for t in texts for m in re.findall(r'(?:#|\bPR )(\d{3,6})\b', t or '')})
    return [{'label': f'PR #{pr}', 'url': f'https://github.com/{repo}/pull/{pr}'} for pr in prs] if repo else []


# -- machine asks: one plain template per check kind --------------------------------------------------------------
_NOTHING = 'Nothing happens: the work stays as it is and coordkit asks again only if it changes.'
TEMPLATES = {
    'lanes': ('Relaunch, hold or cancel {subject}, which cannot recover?', [
        option('Relaunch the lane', 'Work resumes, but side effects it already made may repeat.', True),
        option('Hold it for a human look', 'The lane stays stopped until someone checks what it did.', True),
        option('Cancel it and return the card', 'The work goes back to ready for someone else.', True)]),
    'review': ('Close {subject} now that its PR has merged?', [
        option('Close the card', 'The card is marked done; the merge stands.', True),
        option('Keep it open and check the merge', 'The card stays in review until someone verifies it.', True)]),
    'loops': ('Restart {subject}, which stopped reporting?', [
        option('Restart it now', 'The loop runs one cycle and resumes its schedule.', True),
        option('Pause the loop', 'No more cycles until someone resumes it; its checks stop.', True),
        option('Leave it for now', 'It stays overdue and nothing it checks is watched.', True)]),
    'loop_failure': ('Restore {subject}, which keeps failing?', [
        option('Restore it now', 'Someone fixes the failure; the loop resumes its schedule.', True),
        option('Pause the loop', 'No more failing cycles; its checks stop until resumed.', True)]),
    'rulings': ('Chase, reassign or withdraw an unacknowledged ruling?', [
        option('Chase the owner again', 'The owner is woken once more; nothing else changes.', True),
        option('Reassign it to the lead', 'The lead becomes responsible for acting on it.', True),
        option('Withdraw the ruling', 'The instruction no longer applies.', True)]),
    'blockers': ('Unblock, drop or keep waiting on {subject}?', [
        option('Assign someone to unblock it', 'The named owner works it next.', True),
        option('Drop the blocker', 'The blocked work proceeds without it or is abandoned.', False),
        option('Keep waiting', 'Nothing changes; the work stays blocked.', True)]),
    'holds': ('Lift or keep the merge hold on {subject}?', [
        option('Lift the hold', 'The PR can merge once its other checks pass.', True),
        option('Keep the hold', 'The PR stays blocked until the holder lifts it.', True)]),
    'answers': ('Re-answer an ask whose answer could not be applied?', [
        option('Answer it again with one clear option', 'Coordkit applies the new answer.', True),
        option('Abandon the old answer', 'The original ask stays unresolved.', True)]),
    'mirror': ('Repair the coordkit sync into ANX that keeps failing?', [
        option('Repair it now', 'Pending updates replay into ANX without loss.', True),
        option('Pause the sync', 'ANX stops reflecting bus updates until resumed.', True)]),
    'git': ('Repair a shared git repo pointing at the wrong tree?', [
        option('Unset the setting after a backup', 'Every worktree uses its own files again.', True),
        option('Leave it and investigate first', 'Work in that repo may keep writing to the wrong tree.', True)]),
    'drift': ('Reapply coordkit settings that drifted from the plan?', [
        option('Reapply the planned settings', 'The installed jobs and hooks match the plan again.', True),
        option('Keep the drift and update the plan', 'The current settings become the intended ones.', True)]),
    'stock': ('Triage a backlog of open obligations over its limit?', [
        option('Triage the oldest items now', 'The backlog shrinks; someone spends time on it.', True),
        option('Raise the limit', 'Fewer alerts; old items stay open.', True)]),
    'blocker_mirror': ('Decide on work a lane reports as blocked?', None),
    'question_outage': ('Answer a lane question that could not reach ANX?', None),
}


SUBJECT_DEFAULT = {'lanes': 'a lane', 'review': 'the review card', 'loops': 'a scheduled loop', 'loop_failure': 'a scheduled loop', 'blockers': 'an old blocker', 'holds': 'a PR'}


def subject_words(card):
    """'card:loop-audit' -> 'the loop-audit card'; '' when there is no usable subject."""
    handle = (card or '').removeprefix('card:')
    return f'the {handle} card' if handle and len(handle) <= 40 and not JARGON.search(handle) else ''


def machine(kind, *, situation, impact, recommend, why, asker='coordkit', actor='lead', options=None, title=None,
            evidence=None, if_no_answer=None, by=None, details='', subject=''):
    """A brief for an automated check. The original recommendation becomes the reason; the first option is recommended."""
    default_title, default_options = TEMPLATES.get(kind, (None, None))
    options = options or default_options
    rec, why_raw = (options[0]['action'] if options else recommend), f'{recommend} {why}'.strip()
    plain, refs = scrub(situation)
    situation = ' '.join(sentences(plain)[:3]) or plain
    details = (details + ('\nReferences: ' + ', '.join(dict.fromkeys(refs)) if refs else '')).strip()
    if default_title and '{subject}' in default_title:
        default_title = default_title.format(subject=subject or SUBJECT_DEFAULT.get(kind, 'it'))
        if len(default_title.split()) > MAX_TITLE_WORDS:
            default_title = TEMPLATES[kind][0].format(subject=SUBJECT_DEFAULT.get(kind, 'it'))
    brief = {'title': title or default_title or 'Decide on an automated coordkit finding?', 'situation': situation,
             'impact': impact, 'options': options or [], 'recommendation': rec, 'why': why_raw,
             'if_no_answer': if_no_answer or _NOTHING, 'by': by or '', 'evidence': evidence or [],
             'details': f'check={kind}\n{details}'.strip(), 'asker': asker, 'actor': actor, 'kind': kind}
    return clean(brief)


def blocker_title(row):
    """Deterministic title for a lead-owned blocker (reconcile correlates ANX answers back by it)."""
    need = scrub(row.get('needs') or row.get('text') or '')[0].rstrip('.?!: ')
    words = need.split()[:MAX_TITLE_WORDS - 2]
    return f"Unblock {row.get('frm', 'a lane')}: {' '.join(words) or 'decide how'}?"


YES_NO = re.compile(r"^\W*(approve|may|should|shall|can|could|do|does|is|are|ship|merge|deploy|proceed|go|ok|okay|"
                    r"rerun|retry|skip|keep|use|switch|enable|disable|roll|restart|delete|accept|allow)\b[^?]*\?", re.I | re.S)


def auto_brief(manifest, record):
    """A draft brief from a captured question. It validates only when the capture already holds a decision; otherwise
    tier 1 must author one (`coordkit question brief QID --from-file`). The raw capture always goes to details."""
    asker = record.get('asker') or ''
    raw = record.get('question') or ''
    text = re.sub(r'\*\*[^*]*\*\*', '', raw)                      # markdown bold labels ("**Decision for you:**")
    text = re.sub(r'^[A-Za-z ]{1,20}:\s+', '', scrub(text)[0])     # a modal header ("Repro: ...")
    asks = [x for x in sentences(text) if x.rstrip().endswith('?')]
    title = asks[0].strip() if asks else ''
    labels = [str(o).replace('(Recommended)', '').replace('(recommended)', '').strip() for o in record.get('options') or []]
    labels = [l for l in labels if l][:4]
    if len(labels) >= 2:
        options = [option(l, f'{asker} goes ahead with this choice.') for l in labels]
    elif YES_NO.match(text.strip()):
        rec_label = (record.get('recommendation') or '').replace('(Recommended)', '').replace('(recommended)', '').strip()
        options = [option(rec_label[:240] or 'Yes', f'{asker} goes ahead.'),
                   option('No', f'{asker} does not go ahead and leaves things as they are.', True)]
    else:
        options = []
    rec = (record.get('recommendation') or '').replace('(Recommended)', '').replace('(recommended)', '').strip()
    return clean({
        'title': title, 'situation': ' '.join(sentences(f'{asker} is asking: {text}')[:3]),
        'impact': f'{asker} is waiting on this answer before it continues that work.', 'options': options,
        'recommendation': rec, 'why': f'{asker} recommends it.' if rec else '',
        'if_no_answer': f'Nothing happens: {asker} keeps waiting and does not act.',
        'evidence': pr_evidence(manifest, raw), 'asker': asker, 'actor': asker,
        'details': f"question {record.get('qid', '')} from {asker} ({record.get('capture', '')}); raw: {raw[:1500]}"})
