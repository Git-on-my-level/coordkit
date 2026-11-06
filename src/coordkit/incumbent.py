"""Frozen pure incumbent queries, for read-only shadow comparison.

No incumbent I/O, command entry points, or mutation functions are included.
shadow checks semantic source fingerprints before using these independent answers.
"""
import re

BUS_NAMES = ['BLOCKER_CLOSERS', 'open_rulings', 'open_blockers']
BLOCKER_CLOSERS = ('decision', 'answer')

def open_rulings(rows):
    """Rulings that name an owner and have no ack from that owner yet."""
    acked = {r.get('re') for r in rows if r.get('kind') == 'ack'}
    return [r for r in rows if r.get('kind') == 'ruling' and r.get('owner') and (r.get('id') not in acked)]

def open_blockers(rows):
    """Blockers with no later decision/answer that names them with --re."""
    closed = {r.get('re') for r in rows if r.get('kind') in BLOCKER_CLOSERS and r.get('re')}
    return [r for r in rows if r.get('kind') == 'blocker' and r.get('id') not in closed]

MERGE_NAMES = ['HOLD_TOKEN', 'NO_MERGE', 'NO_MERGE_OBJECT', 'RELEASE', 'NO_MERGE_DISCLAIMER', 'PR_MENTION', 'GO_WORD', 'GO_WINDOW', 'free_hold_asserts', '_IGNORING', 'warn_ignored', 'holds_on']
HOLD_TOKEN = re.compile('(?<![\\w-])(?:hold|holding|held(?!\\s+(?:units?|rows?|batch|backlog|retr(?:y|ies)|jobs?|uploads?|attempts?|owners?)))(?![\\w-])(?!=)', re.IGNORECASE)

NO_MERGE = re.compile("(?<![\\w-])(?:(?:do(?:es)?\\s+not|don\\'t|dont|never|no)\\s+merg(?:e|es|ing))(?![\\w-])", re.IGNORECASE)

NO_MERGE_OBJECT = re.compile('\\s{0,4}(?:before|until|after|unless)\\s+#?(\\d{3,7})(?!\\d)', re.IGNORECASE)

RELEASE = re.compile('(?<![\\w-])(?:unhold(?:s|ed|ing)?|un-held|off hold|(?:hold|holds)\\s+(?:is\\s+|was\\s+|has\\s+been\\s+)?(?:released|lifted|cleared|dropped|withdrawn)|(?:releas(?:e|es|ed|ing)|lift(?:s|ed)?|clear(?:s|ed)?)\\s+(?:the\\s+|this\\s+)?hold|supersedes?\\s+(?:the\\s+)?hold)(?![\\w-])', re.IGNORECASE)

NO_MERGE_DISCLAIMER = re.compile('(?<![\\w-])no\\s+merges?\\s*(?:/|,|\\bor\\b|\\band\\b)\\s*deploy(?:s|ment)?(?:/(?!(?:hold|held|holding|merge)(?![\\w-]))[\\w-]+)*(?:\\s+by\\s+(?:me|this\\s+lane|the\\s+lane))?\\s*(?=[.;,)\\n]|$)', re.IGNORECASE)

PR_MENTION = re.compile('(?<![A-Za-z0-9])(?:PRs?|pull[- ]requests?|#)\\s*#?(\\d{3,7})(?!\\d)', re.IGNORECASE)

GO_WORD = re.compile('(?<![\\w-])(?:go|golive|merge|merges|merged|merging|ship|ships|proceed|approved)(?![\\w-])', re.IGNORECASE)

GO_WINDOW = 16

def free_hold_asserts(number, text):
    """True when free text asserts a hold on PR `number`: hold/held vocabulary or an explicit merge
    refusal, minus the ordering-constraint targets (NO_MERGE_OBJECT) and whole-post disclaimers."""
    excluded = set()
    for m in NO_MERGE.finditer(text):
        obj = NO_MERGE_OBJECT.match(text, m.end())
        if obj:
            excluded.add(obj.group(1))
    body = NO_MERGE_DISCLAIMER.sub(' ', RELEASE.sub(' ', text))
    return (HOLD_TOKEN.search(body) or NO_MERGE.search(body)) is not None and number not in excluded

_IGNORING = set()

def warn_ignored(row):
    """Free-text hold vocabulary from a non-lead poster: a printed warning, never exit 4."""
    rid = row.get('id') or ''
    if rid in _IGNORING:
        return
    _IGNORING.add(rid)
    print(f"MERGE_HOLD_TEXT_IGNORED {rid} by={row.get('frm', '')} (free text no longer holds; post `bus post --kind hold --pr N[,N]`)", flush=True)

def holds_on(number, rows):
    """Unresolved holds naming PR `number`: {poster: latest hold row}. A hold is either structured
    (kind=hold with the PR in `prs` or named in the text) or lead free text asserting one. Resolution,
    in bus order: a later kind=unhold from the same holding poster, or any lead post that releases
    (explicit language) or authorizes the merge (a go/merge word next to the PR mention); a lead
    release clears every poster's hold. Non-lead free text never holds or releases; it warns.
    Incidental mentions resolve nothing."""
    open_holds = {}
    for row in rows:
        poster = row.get('frm')
        if poster in ('', 'all'):
            continue
        kind = row.get('kind')
        text = row.get('text') or ''
        mentions = [m for m in PR_MENTION.finditer(text) if m.group(1) == number]
        covers = number in {str(p) for p in row.get('prs') or []}
        if kind in ('hold', 'unhold'):
            if covers or mentions:
                if kind == 'hold':
                    open_holds[poster] = row
                else:
                    open_holds.pop(poster, None)
            continue
        if not mentions:
            continue
        if poster != 'lead':
            if free_hold_asserts(number, text):
                warn_ignored(row)
            continue
        if free_hold_asserts(number, text):
            open_holds[poster] = row
            continue
        go = any((GO_WORD.search(text[max(0, m.start() - GO_WINDOW - 1):m.start()]) for m in mentions))
        if not RELEASE.search(text) and (not go):
            continue
        open_holds.clear()
    return open_holds

BUS_SHA = '2f31ab1ca80806e081fa73f47f94980f43da67059e77dc10b180cfb7b3a0260c'

MERGE_SHA = 'aaaf716b78dc3a68a27dec0cc4b5ea7493eca8043b20da307896640fcb6580b0'
