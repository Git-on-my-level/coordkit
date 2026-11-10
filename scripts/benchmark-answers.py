"""Synthetic, transport-faked answer sweep benchmark; never contacts live tools.

Run from the repository root: PYTHONPATH=src:. python3 scripts/benchmark-answers.py
Uses the unittest fixture and cleans its private temporary state on exit.
"""
import contextlib
import cProfile
import io
import json
import time
from unittest.mock import patch
from coordkit import answers, bus, loop_answers
from coordkit.state import write_json
from tests.support import FakeCase

case = FakeCase()
case.setUp()
try:
    manifest = case.manifest
    manifest.bus.mirror = False
    bus.configure(manifest)
    items, rows = [], []
    for i in range(40):
        item = {'ask_id': f'event:perf-{i}', 'subject_ref': 'card:perf',
                'answer': {'text': f'Legacy ruling {i}'}, 'answer_unread': True}
        items.append(item)
        loop_answers.remember(manifest, item['ask_id'], 'card:perf', 'audit', 'cycle')
        rows.append({'id': 'answer-' + item['ask_id'], 'frm': 'coordkit', 'to': 'coordinator',
                     'kind': 'decision', 'subject': 'card:perf', 'text': item['answer']['text'],
                     'origin': 'coordkit', 'ts': '2026-01-01T00:00:00Z'})
    bus.append(rows, project=False)
    for i in range(1500):
        write_json(answers.home(manifest)/f'retained-{i:04}.json', {'phase': 'read', 'bus_id': f'old-{i}'})
    with patch.object(answers.AnxClient, 'call', return_value={'items': items}), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        answers.consume(manifest)  # warmup: establish legacy-delivered receipts
        results = []
        for _ in range(3):
            profile = cProfile.Profile()
            start = time.perf_counter()
            profile.runcall(answers.consume, manifest)
            elapsed = time.perf_counter() - start
            reads = sum(stat.callcount for stat in profile.getstats()
                        if getattr(stat.code, 'co_name', '') == 'read_json')
            results.append({'seconds': round(elapsed, 4), 'read_json_calls': reads})
    print(json.dumps({'probe': '40 unchanged response-less unread items; 1500 retained read receipts',
                      'sweeps': results}, sort_keys=True))
finally:
    case.doCleanups()
