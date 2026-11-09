"""ANX is the durable obligation store; all calls have explicit identity."""
import os
import re
from .process import ToolError, call_json


class CardUnresolved(ToolError):
    def __init__(self, card, detail):
        super().__init__("anx", 9, f"{card}: {detail}", "card_unresolved")


class AnxClient:
    def __init__(self, manifest, actor=None):
        self.manifest = manifest
        self.actor = actor or manifest.anx.lead_actor

    def call(self, *args, write=False, **kwargs):
        read_only = (args[:2] in (("cards", "get"), ("cards", "list"), ("lease", "show"), ("inbox", "list"))
                     or args[:1] in (("orient",), ("context",)) or args[:2] == ("bridge", "status") or args[:2] == ("await", "--answers"))
        if write or (self.manifest.mode == "shadow" and not read_only):
            self.manifest.writable()
        cfg = self.manifest.anx
        argv = [os.environ.get("ANX", "anx"), "--workspace", cfg.workspace, "--as", self.actor, "--json"]
        if cfg.base_url:
            argv += ["--base-url", cfg.base_url]
        return call_json([*argv, *map(str, args)], **kwargs)

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
            if evidence.startswith(("/", "./", "../", "~")) or self.manifest.path(evidence).exists():
                evidence = "path:" + str(self.manifest.path(evidence))
            args += ["--evidence", evidence]
        if met:
            args += ["--met"]
        if next_in:
            args += ["--next", next_in]
        return self.call(*args, write=True)

    def ask(self, card, brief, *, again=False):
        """The one path to the operator: files only a brief that passes briefs.validate."""
        from . import briefs
        errors = briefs.validate(brief)
        if errors:
            raise ToolError("anx", 6, "brief_refused: " + "; ".join(errors), "brief_refused")
        return self.call("ask", *briefs.anx_args(brief)[:1], "--subject-ref", card, *briefs.anx_args(brief)[1:],
                         *(["--again"] if again else []), write=True)


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
