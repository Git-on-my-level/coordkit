from pathlib import Path
from unittest.mock import patch
from coordkit import __version__, cli
from coordkit.manifest import ManifestError, discover, duration, load
from coordkit.anxcli import AnxClient, CardUnresolved
from coordkit.actl import AgentctlClient
from coordkit.process import ToolError
from tests.support import BASE, FakeCase


class ManifestTests(FakeCase):
    def test_manifest_fixtures(self):
        directory=Path(__file__).parent/'fixtures/manifests'
        self.assertEqual(load(directory/'valid.toml').project.name,'sample')
        self.assertEqual(load(directory/'shadow.toml').mode,'shadow')
        for fixture in directory.glob('missing-*.toml'):
            with self.subTest(fixture=fixture.name),self.assertRaisesRegex(ManifestError,'missing_key:'):
                load(fixture)

    def test_defaults_and_discovery(self):
        self.assertEqual(self.manifest.anx.lead_actor, "ck-sample-lead")
        self.assertEqual(self.manifest.paths.bus_log, self.root / ".coordkit/bus.jsonl")
        self.assertEqual(discover(self.root / "nested"), self.file)
        self.assertEqual(duration("2h"), 7200)

    def test_required_keys(self):
        for line, error in [('name = "sample"', 'project.name'), ('git_repo = "owner/repo"', 'project.git_repo'),
                            ('workspace = "anx"', 'anx.workspace'), ('board = "ck-sample"', 'anx.board'),
                            ('implement = "pool:implement"', 'agentctl.pools.implement')]:
            with self.subTest(error=error):
                self.file.write_text(BASE.replace(line, ""))
                with self.assertRaisesRegex(ManifestError, "missing_key: " + error):
                    load(self.file)

    def test_shadow_example(self):
        self.file.write_text(BASE + '\n[paths]\nbus_log = "/read/only/bus.jsonl"\nmode = "shadow"\n')
        cfg = load(self.file)
        self.assertEqual(cfg.mode, "shadow")
        with self.assertRaisesRegex(ManifestError, "shadow_mode_read_only"):
            cfg.writable()

    def test_invalid_types_ranges_unknown(self):
        for change in ('idle_hours = "three"', 'idle_hours = -1', 'mispelt = 1', 'mirror = 1', 'text_cap = 0'):
            self.file.write_text(BASE.replace('live_check = false', 'live_check = false\n' + change))
            with self.subTest(change=change), self.assertRaises(ManifestError):
                load(self.file)

    def test_mcp_post_as_is_a_list_of_names(self):
        self.file.write_text(BASE + '\n[mcp]\npost_as = ["relay", "push-lane"]\n')
        self.assertEqual(load(self.file).mcp.post_as, ['relay', 'push-lane'])
        for bad in ('"relay"', '["../x"]'):
            self.file.write_text(BASE + f'\n[mcp]\npost_as = {bad}\n')
            with self.assertRaises(ManifestError):
                load(self.file)

    def test_retired_responsiveness_table_keeps_installed_manifests_loadable(self):
        self.file.write_text(BASE + '\n[responsiveness]\nenabled = false\ntick = "10s"\nack_deadline = "2m"\n')
        manifest = load(self.file)
        self.assertEqual(manifest.project, self.manifest.project)
        self.assertFalse(hasattr(manifest, 'responsiveness'))

    def test_minimum_version_wins_over_forward_compatible_table_tolerance(self):
        future = BASE + '\n[future_delivery]\nenabled = true\n'
        self.file.write_text('minimum_coordkit_version = "999.0.0"\n' + future)
        expected = f'manifest_requires_newer_coordkit: minimum=999.0.0 running={__version__}'
        with self.assertRaisesRegex(ManifestError, expected):
            load(self.file, allow_unknown_tables=True)
        self.file.write_text(f'minimum_coordkit_version = "{__version__}"\n' + future)
        with self.assertRaisesRegex(ManifestError, 'unknown_key: future_delivery'):
            load(self.file)
        self.assertEqual(load(self.file, allow_unknown_tables=True).minimum_coordkit_version, __version__)

    def test_wake_scan_alone_tolerates_unknown_top_level_tables(self):
        self.file.write_text(BASE + '\n[future_delivery]\nenabled = true\n')
        with patch('coordkit.wake.scan', return_value=0) as scan:
            self.assertEqual(cli.main(['--manifest', str(self.file), 'wake-scan', '--dry-run']), 0)
        scan.assert_called_once()
        self.assertTrue(scan.call_args.kwargs['dry_run'])
        with self.assertRaisesRegex(ManifestError, 'unknown_key: future_delivery'):
            load(self.file)

    def test_wake_scan_tolerates_unknown_arrays_of_tables(self):
        self.file.write_text(BASE + '\n[[future_delivery]]\nname = "worker"\n')
        with patch('coordkit.wake.scan', return_value=0) as scan:
            self.assertEqual(cli.main(['--manifest', str(self.file), 'wake-scan', '--dry-run']), 0)
        scan.assert_called_once()
        self.assertEqual(load(self.file, allow_unknown_tables=True).project.name, 'sample')
        with self.assertRaisesRegex(ManifestError, 'unknown_key: future_delivery'):
            load(self.file)
        # An unknown array holding a scalar is not a table array.
        self.file.write_text('future_delivery = ["worker"]\n' + BASE)
        with self.assertRaisesRegex(ManifestError, 'unknown_key: future_delivery'):
            load(self.file, allow_unknown_tables=True)

    def test_table_tolerance_does_not_hide_scalars_or_known_schema_errors(self):
        for suffix in ('\nmisspelt = 1\n', '\n[bus]\nmispelt = 1\n'):
            self.file.write_text(BASE + suffix)
            with self.subTest(suffix=suffix), self.assertRaises(ManifestError):
                load(self.file, allow_unknown_tables=True)
        self.file.write_text('minimum_coordkit_version = 1\n' + BASE)
        with self.assertRaisesRegex(ManifestError, 'invalid_type: minimum_coordkit_version'):
            load(self.file, allow_unknown_tables=True)

    def test_launchd_required_at_install(self):
        with self.assertRaisesRegex(ManifestError, "missing_key: launchd.path"):
            self.manifest.validate_launchd()
        self.manifest.launchd.path = [str(Path.home() / '.local/bin'), '/usr/bin']
        self.manifest.validate_launchd()
        self.manifest.launchd.path.append('relative')
        with self.assertRaises(ManifestError):
            self.manifest.validate_launchd()


class ClientTests(FakeCase):
    def test_resolve_and_identity(self):
        card = AnxClient(self.manifest).resolve_card('card:sample')
        self.assertEqual(card['handle'], 'sample')
        args = self.calls()[0]['args']
        self.assertEqual(args[:5], ['--workspace', 'anx', '--as', 'ck-sample-lead', '--json'])

    def test_unresolved(self):
        for response in ({'exit': 9}, {'json': {'ok': True, 'result': {}}}):
            self.respond({'tool': 'anx', 'contains': ['get'], **response})
            with self.assertRaises(CardUnresolved) as caught:
                AnxClient(self.manifest).resolve_card('missing')
            self.assertEqual(caught.exception.exit_code, 9)

    def test_lease_held_and_network(self):
        for code, category in ((13, 'lease_held'), (6, 'tool_failure')):
            self.respond({'tool': 'anx', 'exit': code, 'stderr': category})
            with self.assertRaises(ToolError) as caught:
                AnxClient(self.manifest).call('lease', 'show', retries=0)
            self.assertEqual(caught.exception.exit_code, code)
            self.assertEqual(caught.exception.category, category)

    def test_bad_envelope(self):
        self.respond({'tool': 'anx', 'stdout': 'not-json'})
        with self.assertRaisesRegex(ToolError, 'protocol_error'):
            AnxClient(self.manifest).call('orient')

    def test_recent(self):
        self.assertEqual(AgentctlClient(self.manifest).recent(nonterminal=True), [])
        self.assertIn('nonterminal', self.calls()[0]['args'])

    def test_shadow_refuses_write(self):
        self.manifest.mode = 'shadow'
        with self.assertRaises(ManifestError):
            AnxClient(self.manifest).note('card:x', 'test')
        self.assertEqual(self.calls(), [])


class EvidenceTests(FakeCase):
    def test_local_evidence_gets_typed_path(self):
        findings = self.root/'findings.md'
        findings.write_text('evidence')
        AnxClient(self.manifest).note('card:x','receipt',evidence=str(findings),met=True)
        args=self.calls()[0]['args']
        self.assertEqual(args[args.index('--evidence')+1],'path:'+str(findings))
        self.assertIn('--met',args)


    def test_typed_refs_preserved(self):
        AnxClient(self.manifest).note('card:x','receipt',evidence='event:receipt',met=True)
        self.assertIn('event:receipt',self.calls()[0]['args'])

    def test_bare_commit_sha_gets_typed_commit_ref(self):
        AnxClient(self.manifest).note('card:x','handoff',evidence='ABCDEF0123456789ABCDEF0123456789ABCDEF01')
        args=self.calls()[0]['args']
        self.assertEqual(args[args.index('--evidence')+1],'commit:abcdef0123456789abcdef0123456789abcdef01')

    def test_untyped_evidence_moves_into_text(self):
        AnxClient(self.manifest).note('card:x','handoff',evidence='data/missing/README.md')
        args=self.calls()[0]['args']
        self.assertNotIn('--evidence',args)
        self.assertEqual(args[args.index('note')+2],'handoff (evidence: data/missing/README.md)')

    def test_evidence_forms_match_anx_typed_ref_rule(self):
        spaced = self.root/'meeting notes.md'
        spaced.write_text('evidence')
        repo = 'repo:workspace:handoffs/a--meeting notes.md@' + '0'*40 + '#sha256:' + 'a'*64
        cases = [('bus:2e1688', 'bus:2e1688'), ('event:x', 'event:x'),
                 ('https://example.com/pr/1', 'https://example.com/pr/1'),
                 (repo, repo), (str(spaced), 'path:'+str(spaced)),
                 ('path:', None), ('path:  ', None), (':x', None), ('word', None)]
        for given, expected in cases:
            with self.subTest(given=given):
                calls = len(self.calls())
                AnxClient(self.manifest).note('card:x', 'n', evidence=given)
                args = self.calls()[calls]['args']
                if expected is None:
                    self.assertNotIn('--evidence', args)
                    self.assertEqual(args[args.index('note')+2], f'n (evidence: {given})')
                else:
                    self.assertEqual(args[args.index('--evidence')+1], expected)

    def test_empty_evidence_is_omitted(self):
        for given in (None, ''):
            calls = len(self.calls())
            AnxClient(self.manifest).note('card:x', 'n', evidence=given)
            args = self.calls()[calls]['args']
            self.assertNotIn('--evidence', args)
            self.assertEqual(args[args.index('note')+2], 'n')

    def test_false_success_not_found_envelope_refuses_card(self):
        self.respond({'tool':'anx','json':{'ok':False,'error':{'code':'not_found'}}})
        with self.assertRaises(CardUnresolved):
            AnxClient(self.manifest).resolve_card('missing')

    def test_shadow_cannot_bypass_write_flag(self):
        self.manifest.mode='shadow'
        with self.assertRaisesRegex(ManifestError,'shadow_mode_read_only'):
            AnxClient(self.manifest).call('cards','move','card:x','--column','review')
        self.assertEqual(self.calls(),[])


class ProcessTests(FakeCase):
    def test_timeout_and_missing_executable(self):
        import sys
        from coordkit.process import run
        with self.assertRaisesRegex(ToolError,'tool_timeout'):
            run([sys.executable,'-c','import time; time.sleep(30)'],timeout=0.05)
        with self.assertRaisesRegex(ToolError,'dependency_unavailable'):
            run([str(self.root/'absent')])

    def test_transient_retry_is_bounded(self):
        import subprocess
        from coordkit.process import call_json
        responses=[subprocess.CompletedProcess(['anx'],6,'','connection closed'),
                   subprocess.CompletedProcess(['anx'],0,'{"ok":true,"result":{"recovered":true}}','')]
        sleeps=[]
        with patch('coordkit.process.run',side_effect=responses) as called:
            self.assertEqual(call_json(['anx'],sleep=sleeps.append),{'recovered':True})
        self.assertEqual(called.call_count,2)
        self.assertEqual(sleeps,[1])

    def test_agent_required_fields_and_duration_errors(self):
        for before,after,error in [('label = "worker"','', 'agents.label'),
                                   ('actor = "ck-sample-worker"','', 'agents.actor')]:
            self.file.write_text(BASE.replace(before,after))
            with self.subTest(error=error),self.assertRaisesRegex(ManifestError,error):
                load(self.file)
        for extra in ('\n[lane]\nlease_ttl = "30s"\n','\n[loop]\ndefault_cadence = "bad"\n'):
            self.file.write_text(BASE+extra)
            with self.subTest(extra=extra),self.assertRaises(ManifestError):
                load(self.file)
