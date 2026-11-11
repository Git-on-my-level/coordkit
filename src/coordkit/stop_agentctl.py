"""Read-only, best-effort collection guard for caller-attributed child work."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import time

from . import process

TIMEOUT = .75  # shared by help and the authoritative child query
CAPABILITY_TTL = 3600
CAPABILITY_TIMEOUT = .15
MAX_CHILDREN = 200
TERMINAL = {'completed', 'failed', 'cancelled', 'orphaned'}
NONTERMINAL = {'created', 'starting', 'running', 'waiting', 'attention'}
EXECUTION = re.compile(r'exec-[a-z]+(?:-[a-z]+){5}')


def children_supported(executable, cache_path, timeout):
    """Help is journal-free; cache both answers across cold hook processes.

    Binary metadata invalidates the cache immediately on replacement. The TTL
    also catches a wrapper whose target changed without replacing the wrapper.
    Failures other than a missing help topic never become negative cache entries.
    """
    resolved = shutil.which(executable)
    if resolved is None:
        raise FileNotFoundError(executable)
    stat = os.stat(resolved)
    binary = [str(Path(resolved).resolve()), stat.st_dev, stat.st_ino,
              stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    at = time.time()
    cached = None
    if cache_path is not None:
        try:
            with cache_path.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) <= 4096:
                cached = json.loads(raw)
        except TimeoutError:
            raise
        except (OSError, ValueError):
            pass
    if (isinstance(cached, dict) and cached.get('version') == 1
            and cached.get('binary') == binary and type(cached.get('supported')) is bool
            and type(cached.get('at')) in (int, float)
            and 0 <= at - cached['at'] < CAPABILITY_TTL):
        return cached['supported']
    try:
        help_doc = process.call_json([executable, 'help', 'children', '--output', 'json'],
                                     timeout=min(timeout, CAPABILITY_TIMEOUT), retries=0, schema_version=1)
        if help_doc.get('name') != 'children' or help_doc.get('side_effect_class') != 'read_only':
            raise ValueError('unsupported children help contract')
        supported = True
    except process.ToolError as exc:
        if exc.category != 'not_found':
            raise
        try:
            error = json.loads(exc.detail)['error']
            missing_topic = error.get('code') == 'not_found' and error.get('details', {}).get('topic') == 'children'
        except (ValueError, KeyError, TypeError, AttributeError):
            missing_topic = False
        if not missing_topic:
            raise
        supported = False
    if cache_path is not None:
        from .state import write_json
        write_json(cache_path, dict(version=1, binary=binary, at=at, supported=supported))
    return supported


def block_reason(session, cache_path=None):
    """On any inventory/probe error leave the existing Stop behaviour intact."""
    if not isinstance(session, str) or not session.strip() or len(session) > 256:
        return None
    deadline = time.monotonic() + TIMEOUT
    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise process.ToolError('agentctl', 6, 'collection guard deadline', 'tool_timeout')
        return budget
    try:
        now = dt.datetime.now(dt.timezone.utc)
        since = now - dt.timedelta(hours=24)
        executable = os.environ.get('AGENTCTL', 'agentctl')
        command = shlex.quote(executable)
        if not children_supported(executable, cache_path, remaining()):
            return None
        page = process.call_json([executable, 'children', '--output', 'json',
                                  '--provider', 'claude', '--native-session-id', session],
                                 timeout=remaining(), retries=0, schema_version=1)
        if (not process.is_schema(page, 1) or page.get('complete') is not True
                or page.get('provider') != 'claude' or page.get('parent_execution_id') is not None
                or not isinstance(page.get('executions'), list)
                or type(page.get('count')) is not int
                or page['count'] != len(page['executions']) or page['count'] > MAX_CHILDREN):
            raise ValueError('incomplete or incompatible children response')
        items = {}
        for item in page['executions']:
            if (not EXECUTION.fullmatch(item['id'])
                    or not since <= process.parse_ts(item['created_at']) <= now):
                continue
            state, reason = item['state'], item['reason']
            if ((state in TERMINAL and reason == 'terminal_uncollected')
                    or (state in NONTERMINAL and reason == 'running_without_known_waiter')):
                items[item['id']] = state
        # Children already accounts for exact process-held waiters and durable
        # subscriptions. Returned attention or integrity-conflicted work stays
        # actionable even when an await is visible; never second-guess it via ps.
        pending = list(items.items())
        if not pending:
            return None
        actions = []
        for key, state in pending[:5]:
            if state in TERMINAL:
                actions.append(f'collect: `{command} result {key} --content`')
            else:
                actions.append(f'arm a background waiter: `{command} await {key} --content`')
        more = f'; +{len(pending) - 5} more' if len(pending) > 5 else ''
        return 'agentctl work needs collection: ' + '; '.join(actions) + more
    except TimeoutError:
        raise  # The outer hook alarm must abort all remaining state access.
    except (process.ToolError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
