"""Agentctl wrapper: selectors come exclusively from manifest pools."""
import json
import os
import socket
from .manifest import ManifestError, SELECTOR_KEYS, duration, parse_selector
from .process import ToolError, call_json, is_schema, run


# The agentctl envelope version this client was written against. agentctl prints
# schema_version 1 on every success and error document.
AGENTCTL_SCHEMA_VERSION = 1


def subscription_key(entry):
    """An execution may need several distinct callback filter sets."""
    return json.dumps([entry['execution_id'], sorted(set(entry.get('kinds', ['terminal', 'attention'])))],
                      separators=(',', ':'))


class AgentctlClient:
    def __init__(self, manifest):
        self.manifest = manifest

    @property
    def binary(self):
        return os.environ.get("AGENTCTL", "agentctl")

    def call(self, *args, **kwargs):
        kwargs.setdefault("schema_version", AGENTCTL_SCHEMA_VERSION)
        return call_json([self.binary, *map(str, args), "--output", "json"], **kwargs)

    def recent(self, *, nonterminal=False, since=None, label=None):
        # Unfiltered history is fleet-wide and takes minutes to page on a busy host; callers
        # narrow by state, creation window or label.
        args = ["recent", "--limit", "200"]
        if nonterminal:
            args += ["--state", "nonterminal"]
        if since:
            args += ["--since", since]
        if label:
            args += ["--label", label]
        rows, cursor = [], None
        for _ in range(100):
            result = self.call(*args, *(["--cursor", cursor] if cursor else []))
            page = result.get("executions") if isinstance(result, dict) else None
            if not isinstance(page, list):
                raise ToolError(self.binary, 6, "missing executions list", "protocol_error")
            rows.extend(page)
            if not result.get("has_more"):
                return rows
            next_cursor = result.get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        raise ToolError(self.binary, 6, "incomplete execution listing", "protocol_error")

    def cancel(self, execution):
        self.manifest.writable()
        return self.call("cancel", execution)

    def status(self, execution):
        return self.call("status", execution)

    def owns(self, execution):
        """True when this project's request-key prefix launched the execution, None when unknown.

        The agentctl dead-letter spool is fleet-wide, so other projects' letters must not count here."""
        try:
            result = self.status(execution)
        except ToolError:
            return None
        key = ((result or {}).get("delegation") or {}).get("request_key")
        if not key:
            return None
        return key.startswith(self.manifest.agentctl.request_key_prefix + "-")

    def subscribe(self, execution, *, kinds=('terminal', 'attention')):
        self.manifest.writable()
        path = self.manifest.paths.events
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        return self.call("subscribe", "create", "--execution", execution, "--destination", "file",
                         "--target", path, *[arg for kind in kinds for arg in ("--kind", kind)], "--ttl", "72h")

    def lane_subscriptions(self):
        """Snapshot execution callbacks for this host-local event sink, including expired rows.

        Expired subscriptions still belong in a move inventory: moving must renew their TTL.
        A different host must list and recreate callbacks through its own agentctl authority.
        """
        result = self.call('subscribe', 'list')
        rows = result.get('subscriptions') if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > 4096:
            raise ToolError(self.binary, 6, 'invalid or oversized subscription list', 'protocol_error')
        entries = {}
        for row in rows:
            destination, filters = row.get('destination', {}), row.get('filter', {})
            if (destination.get('kind') != 'file' or destination.get('path') != str(self.manifest.paths.events)
                    or row.get('state') == 'cancelled'):
                continue
            executions = filters.get('execution_ids', [])
            if not isinstance(executions, list) or not executions:
                raise ToolError(self.binary, 6, 'non-execution callback requires explicit migration', 'protocol_error')
            for execution in executions:
                item = dict(execution_id=execution, subscription_id=row.get('id'),
                                          source_host=socket.gethostname(), authority_host=row.get('origin_host_id'),
                                          target=destination['path'], expires_at=row.get('expires_at'),
                                          kinds=filters.get('kinds', []), state=row.get('state'), ttl='72h')
                key = subscription_key(item)
                previous = entries.get(key, {})
                if (item['state'] == 'active', item['expires_at'] or '') >= (
                        previous.get('state') == 'active', previous.get('expires_at') or ''):
                    entries[key] = item
        return list(entries.values())

    def await_result(self, execution, timeout="30m", ignore_attention=False, call_timeout=None):
        self.manifest.writable()
        result = run([self.binary, "await", execution, "--content", "--timeout", timeout, *(["--ignore-attention"] if ignore_attention else [])],
                     timeout=call_timeout if call_timeout is not None else 60 + duration(timeout))
        if result.returncode:
            raise ToolError(self.binary, result.returncode, result.stderr + result.stdout)
        return result.stdout

    def selector(self, *, pool=None, selector=None):
        """Resolve a plan's selector: an explicit dict, a manifest pool, or a raw selector string."""
        if selector is not None:
            if not isinstance(selector, dict) or not selector:
                raise ManifestError('invalid_value: lane selector must be a nonempty table')
            value = selector
        else:
            configured = self.manifest.agentctl.pools.get(pool)
            if configured is None and isinstance(pool, str) and ':' in pool:
                configured = pool
            if configured is None:
                raise ManifestError(f"missing_key: agentctl.pools.{pool}")
            value = parse_selector(configured)
        out = {}
        for key, item in value.items():
            if key not in SELECTOR_KEYS:
                raise ManifestError(f'invalid_value: unknown agentctl selector key {key}')
            if not isinstance(item, str) or not item or item != item.strip() or len(item) > 256 \
                    or any(ord(ch) < 32 for ch in item):
                raise ManifestError(f'invalid_value: agentctl selector {key}')
            out[key] = item
        if not out:
            raise ManifestError('invalid_value: empty agentctl selector')
        if 'pool' in out and len(out) > 1:
            raise ManifestError('invalid_value: agentctl pool selectors cannot combine with other keys')
        return out

    @staticmethod
    def selector_settings(selector, access):
        """Attach launch settings the selected harness can honour.

        OpenCode has no reviewed read-only recipe; asking for one is refused by
        agentctl, so coordkit refuses before a launch instead of silently granting
        a wider permission mode. Families are resolved by agentctl, so this is only
        asserted for an explicit OpenCode harness or model.
        """
        out = dict(selector)
        opencode = (out.get('harness') == 'opencode'
                    or str(out.get('model', '')).startswith('opencode/'))
        if opencode and access == 'read_only':
            raise ToolError('agentctl', 2, 'opencode selectors do not support read_only access; use access = "coding"',
                            'plan_refused')
        out['settings'] = {'access': access}
        return out


    def delegate(self, prompt, run_dir, *, key, pool="plan", selector=None, label=None, plan=False, detach=False, access="read_only", timeout=None, card=None, owns=(), wakeable=False, cwd=None, call_timeout=None, timeout_override=None):
        self.manifest.writable()
        resolved = self.selector(pool=pool, selector=selector)
        request = {"schema_version": 1, "request_key": key,
                   "selector": self.selector_settings(resolved, access)}
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps(request) + "\n")
        argv = [self.binary, "delegate", "--request-file", str(request_path), "--prompt-stdin",
                "--cwd", str(cwd or self.manifest.file.parent), "--plan" if plan else "--detach" if detach else "--wait"]
        if not plan and not detach:
            argv += ["--content"]
        if label:
            argv += ["--label", label]
        if label and wakeable:
            from .wake import WAKE_LABEL
            argv += ["--label", WAKE_LABEL + label]
        if card:
            argv += ["--label", "anx.card." + card.removeprefix("card:")]
        for pr in owns:
            argv += ["--label", f"pr-{pr}"]
        if detach:
            argv += ["--output", "json"]
        timeout = timeout_override or self.manifest.agentctl.timeout or timeout
        if timeout:
            argv += ["--timeout", timeout]
        result = run(argv, timeout=call_timeout if call_timeout is not None else 300 if plan or detach else duration(timeout) + 120 if timeout else None,
                     stdin=prompt, cwd=cwd or self.manifest.file.parent)
        if result.returncode:
            raise ToolError(self.binary, result.returncode, result.stderr + result.stdout)
        if not result.stdout.strip():
            raise ToolError(self.binary, 6, "empty delegation result", "protocol_error")
        if plan:
            try:
                doc = json.loads(result.stdout)
            except ValueError as exc:
                raise ToolError(self.binary, 6, "invalid delegate plan JSON", "protocol_error") from exc
            native = doc.get("result", {}).get("native", {}) if isinstance(doc, dict) else {}
            if not isinstance(doc, dict) or doc.get("ok") is not True or not native or not is_schema(doc, AGENTCTL_SCHEMA_VERSION):
                raise ToolError(self.binary, 6, "missing successful native plan", "protocol_error")
            if access == "read_only" and native.get("permissions") != "read_only":
                raise ToolError(self.binary, 2, "delegate plan did not grant read_only access", "plan_refused")
        return result.stdout
