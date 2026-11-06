"""Bounded subprocess calls with typed failures and JSON envelope handling."""
import json
import os
import signal
import subprocess
import time


class ToolError(RuntimeError):
    def __init__(self, tool, code, detail, category="tool_failure"):
        self.tool, self.exit_code, self.detail, self.category = tool, code, detail, category
        super().__init__(f"{category}: {tool}: {detail}")


def run(argv, *, timeout=60, stdin=None, cwd=None):
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd,
                                   start_new_session=True)
    except OSError as exc:
        raise ToolError(argv[0], 6, str(exc), "dependency_unavailable") from exc
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # The CLI may own children: stop the entire owned process group before reporting a timeout.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            # The group already exited between communicate's deadline and killpg.
            process.wait()
        process.communicate()
        raise ToolError(argv[0], 6, f"timeout after {timeout}s", "tool_timeout") from exc
    except BaseException:
        # A loop's whole-cycle deadline can interrupt a nested tool call.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.wait()
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def is_schema(doc, version):
    """True only for an integer schema_version equal to the one this consumer was written against.

    bool is an int subclass in Python, so True would otherwise satisfy version 1."""
    value = doc.get("schema_version") if isinstance(doc, dict) else None
    return type(value) is int and value == version


def call_json(argv, *, timeout=60, retries=2, sleep=time.sleep, schema_version=None):
    for attempt in range(retries + 1):
        try:
            result = run(argv, timeout=timeout)
        except ToolError as exc:
            if exc.category != "tool_timeout" or attempt == retries:
                raise
            sleep(2 ** attempt)
            continue
        if result.returncode == 6 and attempt < retries:
            sleep(2 ** attempt)
            continue
        if result.returncode:
            category = "lease_held" if result.returncode == 13 else "tool_failure"
            try:
                envelope = json.loads(result.stdout)
            except ValueError:
                envelope = None
            if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
                category = envelope["error"].get("code", category)
            raise ToolError(argv[0], result.returncode, (result.stderr + result.stdout).strip(), category)
        try:
            doc = json.loads(result.stdout)
        except ValueError as exc:
            raise ToolError(argv[0], 6, "invalid JSON envelope", "protocol_error") from exc
        if not isinstance(doc, dict):
            raise ToolError(argv[0], 6, "expected JSON object", "protocol_error")
        if doc.get("ok") is False:
            error = doc.get("error") or {}
            category = error.get("code", "tool_failure") if isinstance(error, dict) else "tool_failure"
            raise ToolError(argv[0], 13 if category == "lease_held" else 6, str(error), category)
        if schema_version is not None and not is_schema(doc, schema_version):
            # Fail closed: a producer that changed its envelope must not feed partial data to the caller.
            raise ToolError(argv[0], 2, f"unsupported envelope schema_version: {doc.get('schema_version')!r}", "unsupported_schema")
        return doc.get("result", doc.get("data", doc))
    raise AssertionError("unreachable")


def parse_ts(text):
    import datetime as dt
    value = dt.datetime.fromisoformat(text.replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
