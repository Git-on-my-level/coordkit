"""Neutral defaults, compatibility spellings, and manifest-owned MCP boundaries."""
import json
from pathlib import Path
from unittest.mock import patch

from coordkit import addressed, bus, incumbent, mcp_server, questions, shadow
from coordkit.manifest import ManifestError, load
from tests.support import FakeCase


class PublicConfigTests(FakeCase):
    def test_neutral_defaults(self):
        # The shared historical suite declares a coordinator explicitly.
        self.file.write_text(self.file.read_text().replace('coordinator = "coordinator"', ''))
        m = load(self.file)
        self.assertEqual(m.obligations.coordinator, 'lead')
        self.assertEqual(m.questions.fallback_role, 'lead')
        self.assertEqual(m.questions.human_actor, m.anx.lead_actor)
        self.assertEqual(m.review.family, 'ledger')
        self.assertEqual(m.mcp.identity, 'ck-sample-mcp')
        self.assertEqual(m.mcp.train_state_dir, '')
        self.assertEqual(m.mcp.train_pattern, '')
        self.assertEqual(m.mcp.ref_roots, [str(m.paths.root/'state/refs')])

    def test_delivery_cli_old_and_new_keys_and_conflict(self):
        for key in ('cl2' + 'ctl', 'delivery_cli'):
            self.file.write_text(self.file.read_text().split('[questions]')[0] +
                                 f'\n[questions]\n{key} = "/example/bin/send"\n')
            self.assertEqual(load(self.file).questions.delivery_cli, '/example/bin/send')
        with self.file.open('a') as f:
            f.write('cl2' + 'ctl = "/different/send"\n')
        with self.assertRaisesRegex(ManifestError, 'conflicting'):
            load(self.file)

    def test_delivery_cli_aliases_keep_type_validation(self):
        legacy = 'cl2' + 'ctl'
        base = self.file.read_text().split('[questions]')[0]
        for fields in (f'{legacy} = 0', f'{legacy} = 0\ndelivery_cli = "/example/send"',
                       f'{legacy} = "/example/send"\ndelivery_cli = false'):
            self.file.write_text(base + '\n[questions]\n' + fields + '\n')
            with self.subTest(fields=fields), self.assertRaisesRegex(ManifestError, 'invalid_type'):
                load(self.file)

    def test_both_human_only_markers_guard(self):
        for marker in ('[human-only]', '[da' 'vid-only]'):
            self.assertTrue(questions.guarded(self.manifest, marker+' choose a color')[0])
        self.assertFalse(questions.guarded(self.manifest, 'choose a color')[0])

    def test_configured_identity_is_used_for_writes_and_across_projects(self):
        m = self.manifest
        m.mcp.identity = 'human-connector'
        m.bus.mirror = False
        other = self.root/'other.toml'
        other.write_text(self.file.read_text()+'\n[mcp]\nidentity = "different-connector"\n')
        m.mcp.projects = {'other': str(other)}
        for root in (m.paths.root, load(other).paths.root):
            home = root/'state/mcp'; home.mkdir(parents=True, exist_ok=True)
            (home/'writes-enabled').touch()
        result = mcp_server.call_tool(m, 'post', {'project':'other','to':'all','kind':'msg','text':'hello'})
        self.assertTrue(result['ok'])
        self.assertEqual(bus.read()[-1]['frm'], 'human-connector')

    def test_configured_roots_limit_refs_and_reject_symlink_escapes(self):
        project = self.root/'project'
        archive = project/'data/run-one'; archive.mkdir(parents=True)
        body = archive/'body'; body.write_text('evidence')
        self.manifest.mcp.ref_roots = [str(project/'data/run-*')]
        self.assertEqual(mcp_server.ref_file(self.manifest, {'ref':str(body)}), body)
        private = self.root/'private'; private.mkdir(); (private/'body').write_text('private')
        (project/'data/run-escape').symlink_to(private, target_is_directory=True)
        sibling = project/'data/other'; sibling.mkdir(); (sibling/'body').write_text('other')
        for path in (project/'data/run-escape/body', sibling/'body'):
            with self.assertRaisesRegex(ValueError, 'outside'):
                mcp_server.ref_file(self.manifest, {'ref':str(path)})

    def test_default_ref_root_does_not_inherit_bus_parent(self):
        self.manifest.paths.bus_log = self.root/'external/bus.jsonl'
        body = self.root/'external/state/refs/body'; body.parent.mkdir(parents=True); body.write_text('external')
        with self.assertRaisesRegex(ValueError, 'outside'):
            mcp_server.ref_file(self.manifest, {'ref':str(body)})

    def test_invalid_mcp_settings_are_rejected(self):
        for value in ('identity = "bad name"', 'train_pattern = "["', 'ref_roots = [1]'):
            self.file.write_text(self.file.read_text().split('[mcp]')[0]+'\n[mcp]\n'+value+'\n')
            with self.subTest(value=value), self.assertRaises(ManifestError):
                load(self.file)

    def test_custom_relay_and_lead_roles_have_no_incumbent_names(self):
        m = self.manifest
        m.addressed.relay = 'handoff-relay'
        m.addressed.roles = {'captain':['agent-a']}
        m.addressed.lead_escalations = False
        self.assertTrue(addressed.digest_only(m, 'handoff-relay'))
        self.assertFalse(addressed.digest_only(m, 'relay'))
        self.assertEqual(addressed.lead_names(m), {'lead','captain','agent-a'})

    def test_synthetic_fixtures_retain_wire_edge_cases(self):
        fixtures = Path(__file__).parent/'fixtures'
        rows = shadow.read_rows(fixtures/'synthetic-bus-tail.jsonl')
        self.assertEqual(len(rows), 80)
        for key in ('owner','copy_of','orig_kind','evidence','subject','state','re','ref'):
            self.assertTrue(any(key in r for r in rows), key)
        self.assertTrue(any('id' not in r for r in rows))
        self.assertTrue(any(len(r['text']) > 4000 for r in rows))
        self.assertTrue(any('[long-token' in r['text'] for r in rows))
        self.assertTrue(any(r.get('ref') and r['text'].endswith(' … [full: '+r['ref']+']') for r in rows))
        self.assertTrue(any(not r['text'].isascii() for r in rows))
        events = shadow.read_rows(fixtures/'synthetic-events-tail.ndjson')
        self.assertEqual(len(events),80)
        normalized = shadow.normalize_events(events)
        envelopes = [r for r, raw in zip(normalized, events) if isinstance(raw.get('event'), dict)]
        self.assertTrue(any(r.get('label') == 'worker-a' for r in envelopes))
        self.assertTrue(all(r.get('kind') != 'synthetic-kind' and r['ts'].endswith('Z') for r in envelopes))
        for key in ('event','previous_execution_id','attempt','rc','out','lane'):
            self.assertTrue(any(key in r for r in events),key)

    def test_parser_fingerprint_ignores_docs_but_detects_code_drift(self):
        original = Path(incumbent.__file__).read_text()
        source = self.root/'parser.py'
        source.write_text(original.replace('Incidental mentions resolve nothing.', 'A different docstring.'))
        self.assertEqual(shadow.protocol_hash(source, incumbent.MERGE_NAMES), incumbent.MERGE_SHA)
        source.write_text(original.replace('GO_WINDOW = 16', 'GO_WINDOW = 15'))
        self.assertNotEqual(shadow.protocol_hash(source, incumbent.MERGE_NAMES), incumbent.MERGE_SHA)

    def test_default_human_actor_tracks_adopted_lead_but_explicit_actor_is_preserved(self):
        self.manifest.paths.root.mkdir(parents=True)
        (self.manifest.paths.root/'lead.json').write_text(json.dumps({'actor':'adopted-lead'}))
        self.assertEqual(load(self.file).questions.human_actor, 'adopted-lead')
        with self.file.open('a') as stream:
            stream.write('\n[questions]\nhuman_actor = "ck-sample-lead"\n')
        self.assertEqual(load(self.file).questions.human_actor, 'ck-sample-lead')

    def test_empty_train_configuration_never_scans_manifest_directory(self):
        self.manifest.mcp.train_state_dir = ''
        with patch.object(mcp_server.shutil, 'which', return_value=None), patch.object(Path, 'glob') as scan:
            result = mcp_server.lane_rows(self.manifest, {})
        scan.assert_not_called()
        self.assertEqual(result['rows'], [])
