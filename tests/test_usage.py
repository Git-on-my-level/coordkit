from datetime import datetime, timedelta, timezone
import json

from coordkit.manifest import load, ManifestError, UsageLane
from coordkit.usage import (collect, doctor_warnings, drift, encoded_cwd,
                            model_matches, parse_transcripts, transcript_dir)
from tests.support import FakeCase

AT = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)


class UsageTests(FakeCase):
    def lane(self, name='worker', config='account', cwd='/work/repo'):
        return UsageLane(name, str(self.root/config), cwd, 'family', 'high')

    def write_rows(self, lane, rows, nested='session.jsonl'):
        path = transcript_dir(lane)/nested
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
        return path

    def row(self, id='message-1', model='claude-family-4-7', usage=None, at=AT):
        return {'type': 'assistant', 'timestamp': at.isoformat(),
                'message': {'id': id, 'model': model, 'usage': usage}}

    def parse(self, lane):
        return parse_transcripts([transcript_dir(lane)], AT-timedelta(hours=1), AT)

    def test_dedupe_streaming_subagent_and_window(self):
        lane = self.lane()
        self.write_rows(lane, [self.row(usage={'input_tokens': 10, 'output_tokens': 2,
                                             'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 20}),
                               self.row(id='old', at=AT-timedelta(hours=2), usage={'input_tokens': 900}),
                               self.row(id='future', at=AT+timedelta(seconds=1))])
        self.write_rows(lane, [self.row(usage={'output_tokens': 5}),
                               self.row(id='sub', model='claude-other-1', usage={'input_tokens': 7})],
                        'session/subagents/agent.jsonl')
        usage = self.parse(lane)
        self.assertEqual((usage.turns, usage.input_tokens, usage.output_tokens), (2, 17, 5))
        self.assertEqual((usage.cache_read_input_tokens, usage.cache_creation_input_tokens, usage.max_context),
                         (100, 20, 130))
        self.assertEqual(set(usage.models), {'claude-family-4-7', 'claude-other-1'})
        self.assertEqual(usage.latest_model, 'claude-other-1')
        self.assertEqual(drift(lane, usage), ['claude-other-1'])

    def test_unscoped_lane_sharing_a_scoped_lane_directory_is_ambiguous(self):
        sid = '33333333-3333-4333-8333-333333333333'
        scoped = UsageLane('scoped', str(self.root/'account'), '/work/repo', 'family', 'high', sid)
        unscoped = self.lane('unscoped', 'account', '/work/repo')
        self.write_rows(scoped, [self.row(id='s', usage={'input_tokens': 2})], sid + '.jsonl')
        results = {lane.name: usage for lane, usage in collect([scoped, unscoped], AT)}
        self.assertIsNone(results['unscoped'])
        self.assertEqual(results['scoped'].input_tokens, 2)

    def test_context_now_and_compaction_boundaries(self):
        lane = self.lane()
        boundary = {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': (AT - timedelta(minutes=30)).isoformat(),
                    'compactMetadata': {'trigger': 'manual', 'preTokens': 543400, 'postTokens': 14580}}
        old = {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': (AT - timedelta(hours=5)).isoformat(),
               'compactMetadata': {'preTokens': 9, 'postTokens': 1}}
        self.write_rows(lane, [old, self.row(id='first', at=AT-timedelta(minutes=40), usage={'input_tokens': 10}),
                               boundary,
                               self.row(id='newest', at=AT-timedelta(minutes=5),
                                        usage={'input_tokens': 3, 'cache_read_input_tokens': 900, 'output_tokens': 7,
                                               'cache_creation_input_tokens': 50})])
        usage = self.parse(lane)
        self.assertEqual(usage.compactions, 1)
        self.assertEqual((usage.last_compact_pre, usage.last_compact_post), (543400, 14580))
        self.assertEqual(usage.context_now, 953)
        self.assertEqual(usage.last_compact_at, boundary['timestamp'])

    def test_missing_usage_and_malformed_tail(self):
        lane = self.lane()
        path = self.write_rows(lane, [self.row(), self.row(id='bad', usage='invalid'),
                                     self.row(id='negative', model='', usage={'input_tokens': -1}),
                                     {'type': 'assistant', 'message': {'id': 'no-time'}}])
        with path.open('a') as f:
            f.write('[1,2]\n{partial')
        usage = self.parse(lane)
        self.assertEqual(usage.turns, 3)
        self.assertEqual(usage.missing_usage, 2)
        self.assertEqual(usage.input_tokens, 0)
        self.assertEqual(usage.invalid_rows, 2)

    def test_missing_ids_fall_back_without_collapsing_turns(self):
        lane = self.lane()
        self.write_rows(lane, [self.row(id=None), self.row(id=None)])
        self.assertEqual(self.parse(lane).turns, 2)

    def test_matching_accounts_workspaces_and_ambiguity(self):
        lane, other = self.lane(), self.lane('other', 'second-account')
        self.assertEqual(encoded_cwd('/work/repo_name.v2'), '-work-repo-name-v2')
        self.write_rows(lane, [self.row(usage={'input_tokens': 1})])
        self.write_rows(other, [self.row(usage={'input_tokens': 2})])
        unrelated = self.lane('unrelated', cwd='/somewhere/else')
        self.write_rows(unrelated, [self.row(usage={'input_tokens': 99})])
        self.assertEqual([u.input_tokens for _, u in collect([lane, other], AT)], [1, 2])
        collision = self.lane('collision', cwd='/work/repo-name.v2')
        first = self.lane('first', cwd='/work/repo_name.v2')
        self.assertTrue(all(u is None for _, u in collect([first, collision], AT)))
        self.assertEqual(self.parse(self.lane('missing', 'missing')).turns, 0)

    def test_session_ids_attribute_lanes_sharing_one_workspace(self):
        sid_a, sid_b = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
        first = UsageLane('first', str(self.root/'account'), '/work/repo', 'family', 'high', sid_a)
        second = UsageLane('second', str(self.root/'account'), '/work/repo', 'family', 'high', sid_b)
        missing = UsageLane('missing', str(self.root/'account'), '/work/repo', 'family', 'high', 'no-such-session')
        self.write_rows(first, [self.row(id='a', usage={'input_tokens': 3})], sid_a + '.jsonl')
        self.write_rows(first, [self.row(id='a-sub', usage={'input_tokens': 4})], f'{sid_a}/subagents/agent.jsonl')
        self.write_rows(second, [self.row(id='b', usage={'input_tokens': 5})], sid_b + '.jsonl')
        results = {lane.name: usage for lane, usage in collect([first, second, missing], AT)}
        self.assertEqual((results['first'].turns, results['first'].input_tokens), (2, 7))
        self.assertEqual((results['second'].turns, results['second'].input_tokens), (1, 5))
        self.assertEqual(results['missing'].turns, 0)

    def test_session_id_manifest_validation(self):
        with self.file.open('a') as f:
            f.write('\n[[lanes]]\nname="worker"\nconfig_dir="account"\ncwd="repo"\nmodel="family"\neffort="high"\n'
                    'session_id="../escape"\n')
        with self.assertRaises(ManifestError):
            load(self.file)
        self.file.write_text(self.file.read_text().replace('session_id="../escape"', 'session_id="abc-123"'))
        self.assertEqual(load(self.file).lanes[0].session_id, 'abc-123')
        with self.file.open('a') as f:
            f.write('\n[[lanes]]\nname="other"\nconfig_dir="account"\ncwd="repo"\nmodel="family"\neffort="high"\n'
                    'session_id="abc-123"\n')
        with self.assertRaises(ManifestError):
            load(self.file)

    def test_model_alias_and_pinned_drift(self):
        self.assertTrue(model_matches('family', 'claude-family-4-7'))
        self.assertTrue(model_matches('claude-family-4-7', 'claude-family-4-7'))
        self.assertFalse(model_matches('family-4-7', 'claude-family-4-8'))
        self.assertFalse(model_matches('family', 'claude-familyish-4'))
        self.manifest.lanes = [self.lane()]
        self.write_rows(self.manifest.lanes[0], [self.row(model='claude-other-1')])
        self.assertIn('intended=family actual=claude-other-1', doctor_warnings(self.manifest, AT)[0])


    def test_manifest_roster_paths_and_validation(self):
        with self.file.open('a') as f:
            f.write('\n[[lanes]]\nname="worker"\nconfig_dir="account"\ncwd="repo"\nmodel="family"\neffort="high"\n')
        manifest = load(self.file)
        self.assertEqual(manifest.lanes[0].config_dir, str(self.root/'account'))
        self.assertEqual(manifest.lanes[0].cwd, str(self.root/'repo'))
        self.file.write_text(self.file.read_text().replace('effort="high"', 'effort=2'))
        with self.assertRaises(ManifestError):
            load(self.file)

    def test_roster_lane_may_pin_its_session_id(self):
        # Session pinning must be accepted by every command that loads the manifest.
        with self.file.open('a') as f:
            f.write('\n[[lanes]]\nname="worker"\nconfig_dir="account"\ncwd="repo"\nmodel="family"\neffort="high"\n'
                    'session_id="11111111-1111-4111-8111-111111111111"\n')
        self.assertEqual(load(self.file).lanes[0].session_id, '11111111-1111-4111-8111-111111111111')
