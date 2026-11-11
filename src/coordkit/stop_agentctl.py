"""Read-only, best-effort collection guard for explicitly session-labelled work."""
import datetime as dt
import hashlib
import os
from pathlib import Path
import re
import shlex
import time

from . import process

TIMEOUT = .75  # shared by inventory pages and the live process snapshot
MAX_PAGES = 3
PAGE_SIZE = 200
TERMINAL = {'completed', 'failed', 'cancelled', 'orphaned'}
NONTERMINAL = {'created', 'starting', 'running', 'waiting', 'attention'}
EXECUTION = re.compile(r'exec-[a-z]+(?:-[a-z]+){5}')


def session_label(session):
    """Explicit caller label, never a cwd or delegated child's native session."""
    if not isinstance(session, str) or not session.strip() or len(session) > 256:
        return None
    return 'coordkit.caller.' + hashlib.sha256(('claude-code\0' + session).encode()).hexdigest()


def live_waiters(timeout):
    """Only an actual per-execution await process with content collection counts."""
    result = process.run(['ps', '-U', str(os.getuid()), '-o', 'stat=,args='], timeout=timeout)
    if result.returncode:
        raise ValueError('process snapshot unavailable')
    waiting = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or 'Z' in fields[0] or 'T' in fields[0]:
            continue
        try:
            args = shlex.split(fields[1])
        except ValueError:
            continue
        # Do not match shell command strings, ps itself, result reads, or an
        # execution ID mentioned in a delegate prompt. Common flags may precede await.
        if not args or Path(args[0]).name != 'agentctl' or '--content' not in args:
            continue
        # The inventory uses the default journal. An explicit alternate scope
        # cannot prove collection of that inventory, even with the same ID.
        if any(arg.split('=', 1)[0] in ('--journal', '--config', '--config-bundle', '--profile')
               for arg in args):
            continue
        index = 1
        while index < len(args) and args[index].startswith('--'):
            if args[index] in ('--output',):
                index += 2
            elif args[index] == '--json':
                index += 1
            else:
                break
        if (index + 1 < len(args) and args[index] == 'await'
                and EXECUTION.fullmatch(args[index + 1]) and '--no-acknowledge' not in args):
            waiting.add(args[index + 1])
    return waiting


def block_reason(session):
    """On any inventory/probe error leave the existing Stop behaviour intact."""
    label = session_label(session)
    if label is None:
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
        argv = ['agentctl', 'recent', '--output', 'json', '--limit', str(PAGE_SIZE),
                '--label', label, '--since', since.isoformat()]
        items, cursor = {}, None
        for _ in range(MAX_PAGES):
            page = process.call_json(argv + (['--cursor', cursor] if cursor else []),
                                     timeout=remaining(), retries=0, schema_version=1)
            for item in page['executions']:
                # Recheck scope locally as well as asking the owning CLI to filter.
                if (label not in item['labels'] or not EXECUTION.fullmatch(item['id'])
                        or not since <= process.parse_ts(item['created_at']) <= now):
                    continue
                state = item['state']
                if (state in TERMINAL and item.get('unreconciled') is True) or state in NONTERMINAL:
                    items[item['id']] = state
            if not page.get('has_more'):
                break
            cursor = page['next_cursor']
        waiting = live_waiters(remaining()) if any(s in NONTERMINAL for s in items.values()) else set()
        pending = [(key, state) for key, state in items.items() if state in TERMINAL or key not in waiting]
        if not pending:
            return None
        actions = []
        for key, state in pending[:5]:
            if state in TERMINAL:
                actions.append(f'collect: `agentctl result {key} --content`')
            else:
                actions.append(f'arm a background waiter: `agentctl await {key} --content`')
        more = f'; +{len(pending) - 5} more' if len(pending) > 5 else ''
        return 'agentctl work needs collection: ' + '; '.join(actions) + more
    except TimeoutError:
        raise  # The outer hook alarm must abort all remaining state access.
    except (process.ToolError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
