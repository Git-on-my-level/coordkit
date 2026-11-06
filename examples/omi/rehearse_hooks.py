"""Rehearsal of the example hook transactions on DISPOSABLE copies (the real settings files are only read).

Usage: python3.11 -I examples/omi/rehearse_hooks.py EVIDENCE_DIR
Copies ~/.claude/settings.json and ~/.claude-2/settings.json into EVIDENCE_DIR/rehearsal/, builds a live copy of
the example manifest rooted there, then installs and restores (1) the lead-hook transformation and (2) the question
hooks for both dirs, asserting each restore is byte-for-byte JSON-equal to the original and that unrelated hooks
survive.
"""
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
from coordkit import hooks  # noqa: E402
from coordkit.manifest import load  # noqa: E402

evidence = Path(sys.argv[1]).resolve()
work = evidence/'rehearsal'
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True)
dirs = {}
for name in ('.claude', '.claude-2'):
    d = work/name.lstrip('.')
    d.mkdir()
    shutil.copyfile(Path.home()/name/'settings.json', d/'settings.json')
    dirs[name] = d
text = (ROOT/'examples/omi/coordkit.toml').read_text()
text = text.replace('mode = "shadow"', 'mode = "live"', 1).replace('root = ".coordkit"', f'root = {json.dumps(str(work/"root"))}', 1)
text = re.sub(r'^config_dirs = .*$', 'config_dirs = ' + json.dumps([str(d) for d in dirs.values()]), text, flags=re.M)
manifest_file = work/'coordkit.toml'
manifest_file.write_text(text)
m = load(manifest_file)
results = {}

# (1) lead hooks: incumbent pending + ask-guard -> coordkit, on the copy of ~/.claude
lead = dirs['.claude']/'settings.json'
original = json.loads(lead.read_text())
backup = work/'lead-backup.json'
shutil.copyfile(lead, backup)
hooks.install(m, lead, backup)
after = json.loads(lead.read_text())
assert after != original, 'lead transformation changed nothing'
hooks.restore(m)
assert json.loads(lead.read_text()) == original, 'lead restore differs from the original'
results['lead_hooks'] = 'install+restore exact'

# (2) question hooks on both copies, through the same receipts `up --apply` and `down` use
originals = {k: json.loads((d/'settings.json').read_text()) for k, d in dirs.items()}
for k, d in dirs.items():
    b = work/f'{k.lstrip(".")}-qbackup.json'
    shutil.copyfile(d/'settings.json', b)
    hooks.install_questions(m, str(d), b)
assert hooks.question_drift(m) == [], hooks.question_drift(m)
for k, d in dirs.items():
    doc = json.loads((d/'settings.json').read_text())
    assert doc['hooks']['PreToolUse'][-1]['matcher'] == 'AskUserQuestion'
    for event, groups in originals[k].get('hooks', {}).items():
        for g in groups:
            assert g in doc['hooks'][event], f'unrelated hook lost in {k} {event}'
hooks.restore_questions(m)
for k, d in dirs.items():
    assert json.loads((d/'settings.json').read_text()) == originals[k], f'question restore differs in {k}'
results['question_hooks'] = 'install+restore exact for ' + ', '.join(dirs)
(evidence/'hook-rehearsal.json').write_text(json.dumps(results, indent=1))
print('HOOK_REHEARSAL_OK', json.dumps(results))
