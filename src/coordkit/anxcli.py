"""ANX is the durable obligation store; all calls have explicit identity."""
import os
import re
from .process import ToolError, call_json


class CardUnresolved(ToolError):
    def __init__(self, card, detail):
        super().__init__("anx", 9, f"{card}: {detail}", "card_unresolved")


def is_typed_ref(ref):
    """ANX's own rule (validateTypedRef): a nonblank prefix and a nonblank value around the first colon."""
    prefix, sep, value = ref.strip().partition(":")
    return bool(sep and prefix.strip() and value.strip())


class AnxClient:
    def __init__(self, manifest, actor=None):
        self.manifest = manifest
        self.actor = actor or manifest.anx.lead_actor

    def call(self, *args, write=False, **kwargs):
        answer_poll = args[:1] == ('await',) and '--answers' in args
        read_only = (args[:2] in (("cards", "get"), ("cards", "list"), ("lease", "show"), ("inbox", "list"))
                     or args[:1] in (("orient",), ("context",)) or args[:2] == ("bridge", "status") or answer_poll)
        if write or (self.manifest.mode == "shadow" and not read_only):
            self.manifest.writable()
        cfg = self.manifest.anx
        argv = [os.environ.get("ANX", "anx"), "--workspace", cfg.workspace, "--as", self.actor, "--json"]
        if cfg.base_url:
            argv += ["--base-url", cfg.base_url]
        result = call_json([*argv, *map(str, args)], **kwargs)
        if answer_poll and self.manifest.mode == 'live' and isinstance(result, dict):
            # A shared consuming stream needs one receipt boundary for every reader,
            # including lead waiters and future integrations, before returning data.
            from . import loop_answers
            from . import answers
            remaining = []
            for item in result.get('answers', []):
                try:
                    if not answers.owns(self, item):
                        continue
                    ask_id = item.get('ask_id') or item.get('event_ref') or item.get('id')
                    item = dict(item, ask_id=answers.reference(ask_id))
                except (ValueError, TypeError, AttributeError):
                    answers.reject(self.manifest, item)
                    continue
                if answers.already_delivered(self.manifest, item):
                    continue
                remaining.append(item)
                loop_answers.defer(self.manifest, item)
                answer = item.get('answer')
                if isinstance(answer, dict) and (answer.get('response_event_id') or answer.get('response_event_ref')):
                    answers.shared_answer(self.manifest, item, self)
                else:
                    loop_answers.receive(self.manifest, item)
            result['answers'] = remaining
        return result

    def identities(self):
        """Names this actor is held under: its --as name and the principal id ANX records on leases
        it takes implicitly (for example the creator lease from `cards create`)."""
        if not hasattr(self, "_identities"):
            principal = (self.call("orient").get("principal") or {}).get("id")
            self._identities = {self.actor} | ({principal} if principal else set())
        return self._identities

    def resolve_card(self, card):
        handle = card.removeprefix("card:")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", handle):
            raise CardUnresolved(card, "invalid handle")
        try:
            result = self.call("cards", "get", f"card:{handle}")
        except ToolError as exc:
            # Network errors remain tool failures, not evidence of an absent card.
            if exc.exit_code == 6 and exc.category not in ("not_found", "card_unresolved"):
                raise
            raise CardUnresolved(card, str(exc)) from exc
        if not isinstance(result, dict) or not (result.get("card") or result.get("id") or result.get("handle")):
            raise CardUnresolved(card, "empty card lookup")
        return result.get("card", result)

    def cadence(self, card):
        # `cards get` omits cadence; ANX reports it under `context <card>` as work.cadence.
        result = self.call("context", card)
        work = result.get("work") if isinstance(result, dict) else None
        return (work or {}).get("cadence") or {}

    def note(self, card, text, *, evidence=None, met=False, next_in=None):
        args = ["note", card, text]
        if evidence:
            evidence = str(evidence)
            # An explicit path wins; then an explicit typed ref, even if a file of that name exists.
            if evidence.startswith(("/", "./", "../", "~")) or (
                    not is_typed_ref(evidence) and self.manifest.path(evidence).exists()):
                evidence = "path:" + str(self.manifest.path(evidence))
            # A SHA needs a digit and a letter; a numeric run id is not a commit.
            elif re.fullmatch(r"(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{7,40}", evidence):
                evidence = "commit:" + evidence.lower()
            if is_typed_ref(evidence):
                args += ["--evidence", evidence]
            else:
                # ANX rejects untyped refs; keep the value in the text so a bus row never blocks projection.
                args[2] = f"{text} (evidence: {evidence})"
        if met:
            args += ["--met"]
        if next_in:
            args += ["--next", next_in]
        return self.call(*args, write=True)

    def ask(self, card, brief, *, again=False, supersedes=None):
        """The one path to the operator: files only a brief that passes briefs.validate."""
        from . import briefs
        errors = briefs.validate(brief)
        if errors:
            raise ToolError("anx", 6, "brief_refused: " + "; ".join(errors), "brief_refused")
        return self.call("ask", *briefs.anx_args(brief)[:1], "--subject-ref", card, *briefs.anx_args(brief)[1:],
                         *(["--again"] if again else []),
                         *(["--supersedes", supersedes] if supersedes else []), write=True)


def ask_reference(result):
    """Prefer a wrapped response's reference; fall back to compatibility top-level fields."""
    if not isinstance(result, dict):
        return None
    for candidate in (result.get('body'), result):
        if isinstance(candidate, dict):
            ask = candidate.get('ask') if isinstance(candidate.get('ask'), dict) else {}
            ref = candidate.get('ask_id') or candidate.get('id') or ask.get('id') or candidate.get('event_ref')
            if ref:
                return ref
    return None


def past_promises(orient):
    """Normalize bounded orient rollups and the flat fixture/older CLI form."""
    found = {}
    def walk(value):
        if isinstance(value, dict):
            if value.get('state') == 'past_promise' and value.get('ref'):
                found[value['ref']] = value
            for child in value.values():
                if isinstance(child, (dict,list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(orient)
    for item in orient.get('past_promise', []):
        if isinstance(item,dict):
            ref=item.get('ref') or 'card:'+item.get('handle','')
            found[ref]=dict(item,ref=ref)
    return list(found.values())
