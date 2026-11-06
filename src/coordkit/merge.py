"""Hold classification and GitHub reads shared by reconciliation and review."""
import os
import re
from .process import ToolError, run

# Hold intent: a standalone hold/held/holding word (hyphenated compounds like cap-hold,
# hold-level and identifiers like failed_held/held= are excluded; the plural noun "holds"
# is behavioural prose — "1h holds", "controller holds on GateError" — not a hold on a PR,
# and neither is the batch pipeline's "held units/rows/batch" vocabulary).
HOLD_TOKEN = re.compile(r'(?<![\w-])(?:hold|holding|'
                        r'held(?!\s+(?:units?|rows?|batch|backlog|retr(?:y|ies)|jobs?|uploads?|attempts?|owners?)))'
                        r'(?![\w-])(?!=)', re.IGNORECASE)
# Explicit merge refusals; "(do not merge|no merge ...) before/until/after #N" holds the
# post's subject PR, not the N named as the ordering constraint, so those N are excluded.
NO_MERGE = re.compile(r'(?<![\w-])(?:(?:do(?:es)?\s+not|don\'t|dont|never|no)\s+merg(?:e|es|ing))(?![\w-])',
                      re.IGNORECASE)
NO_MERGE_OBJECT = re.compile(r'\s{0,4}(?:before|until|after|unless)\s+#?(\d{3,7})(?!\d)', re.IGNORECASE)
# Release language is matched first and stripped, so "hold released" and "supersedes hold"
# never count as hold intent. Ambiguous GO wording is deliberately NOT a release: say
# unhold/release naming the PR, or obtain a lead ruling naming it.
RELEASE = re.compile(r'(?<![\w-])(?:unhold(?:s|ed|ing)?|un-held|off hold|'
                     r'(?:hold|holds)\s+(?:is\s+|was\s+|has\s+been\s+)?(?:released|lifted|cleared|dropped|withdrawn)|'
                     r'(?:releas(?:e|es|ed|ing)|lift(?:s|ed)?|clear(?:s|ed)?)\s+(?:the\s+|this\s+)?hold|'
                     r'supersedes?\s+(?:the\s+)?hold)(?![\w-])', re.IGNORECASE)
# A whole-clause status disclaimer ("No merge/deploy.") is not a refusal.
# Action restrictions and conditional disclaimers still hold. Optional /IAM-style
# suffixes and "by me|this lane" must end the clause to count as a disclaimer.
NO_MERGE_DISCLAIMER = re.compile(r'(?<![\w-])no\s+merges?\s*(?:/|,|\bor\b|\band\b)\s*deploy(?:s|ment)?'
                                 r'(?:/(?!(?:hold|held|holding|merge)(?![\w-]))[\w-]+)*'  # "/IAM", never "/hold"
                                 r'(?:\s+by\s+(?:me|this\s+lane|the\s+lane))?\s*(?=[.;,)\n]|$)', re.IGNORECASE)


# A PR is named as `#123`, `PR 123`, `PRs 123, 456` (comma lists expand naturally).
PR_MENTION = re.compile(r'(?<![A-Za-z0-9])(?:PRs?|pull[- ]requests?|#)\s*#?(\d{3,7})(?!\d)', re.IGNORECASE)
# Authorization requires a go/merge verb immediately before the PR mention.
# A verb after a PR list, or a ruling that merely lists PRs, authorizes nothing.
GO_WORD = re.compile(r'(?<![\w-])(?:go|golive|merge|merges|merged|merging|ship|ships|proceed|approved)(?![\w-])',
                     re.IGNORECASE)
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


_IGNORING = set()  # bus ids already warned about in this process (the poll loop re-reads the bus)


def warn_ignored(row):
    """Free-text hold vocabulary from a non-lead poster: a printed warning, never exit 4."""
    rid = row.get('id') or ''
    if rid in _IGNORING:
        return
    _IGNORING.add(rid)
    print(f'MERGE_HOLD_TEXT_IGNORED {rid} by={row.get("frm", "")} '
          f'(free text no longer holds; post `bus post --kind hold --pr N[,N]`)', flush=True)


def holds_on(number, rows, warn=warn_ignored):
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
        covers = number in {str(p) for p in (row.get('prs') or [])}
        if kind in ('hold', 'unhold'):
            if covers or mentions:
                if kind == 'hold':
                    open_holds[poster] = row
                else:
                    if poster == 'lead':
                        open_holds.clear()
                    else:
                        open_holds.pop(poster, None)
            continue
        if not mentions:  # free text must NAME this PR: a post about another PR
            continue     # never held or released this one
        if poster != 'lead':
            if free_hold_asserts(number, text):
                warn(row)
            continue
        if free_hold_asserts(number, text):
            open_holds[poster] = row
            continue
        go = any(GO_WORD.search(text[max(0, m.start() - GO_WINDOW - 1):m.start()]) for m in mentions)
        if not RELEASE.search(text) and not go:
            continue
        open_holds.clear()
    return open_holds


def gh(manifest, *args):
    result = run([os.environ.get('GH','gh'), *map(str,args), '-R',manifest.project.git_repo],timeout=90)
    if result.returncode:
        raise ToolError('gh',6,result.stderr+result.stdout)
    return result.stdout
