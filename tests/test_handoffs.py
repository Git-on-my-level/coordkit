"""A sender's successful local read must never stand in for a consumer's read."""
import contextlib
import hashlib
import io
import subprocess
import time
import os
from unittest.mock import patch

from coordkit import addressed, bus, mcp_server, obligations
from coordkit.manifest import ManifestError, load
from coordkit.refs import ref_file
from coordkit.process import ToolError
from tests.support import FakeCase


class HandoffTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / 'repository'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'base.txt').write_text('base\n')
        self.git('add', 'base.txt')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.path = self.repo / 'handoffs' / 'worker--task.md'
        self.path.parent.mkdir()
        self.path.write_text('Review this work.\n')
        self.git('add', 'handoffs')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.remote = self.root / 'remote.git'
        result = subprocess.run(['git', 'clone', '--bare', str(self.repo), str(self.remote)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.git('remote', 'add', 'origin', str(self.remote))
        self.manifest.handoffs.repos = {'sample': str(self.repo)}
        self.manifest.handoffs.required = True
        self.manifest.bus.mirror = False
        self.ref = self.reference()

    def git(self, *args):
        result = subprocess.run(['git', '-C', str(self.repo), *args], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def reference(self, path=None):
        path = path or self.path
        commit = self.git('rev-parse', 'HEAD').strip()
        return f'repo:sample:{path.relative_to(self.repo).as_posix()}@{commit}#sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}'

    def commit_and_push(self):
        self.git('add', 'handoffs')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'update')
        self.git('push', 'origin', 'main')

    def invoke(self, *args, code=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                result = bus.main(list(args), self.manifest) or 0
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(result, code, out.getvalue() + err.getvalue())
        return out.getvalue() + err.getvalue()

    def post(self, *args, code=0):
        return self.invoke('post', '--from', 'worker', '--to', 'lead', '--kind', 'handoff', *args, 'Ready', code=code)

    def rows(self):
        bus.configure(self.manifest)
        return bus.read()

    def test_both_portable_fields_survive_storage_and_reads(self):
        self.post('--ref', self.ref, '--evidence', self.ref)
        row = self.rows()[0]
        self.assertEqual(row['ref'], self.ref)
        self.assertEqual(row['evidence'], self.ref)
        self.assertEqual(ref_file(self.manifest, row).read_bytes(), self.path.read_bytes())
        self.assertEqual(len(obligations.open_obligations(self.manifest, self.rows(), time.time())), 1)
        self.invoke('ack', row['id'], '--from', 'lead')
        self.assertEqual(obligations.open_obligations(self.manifest, self.rows(), time.time()), [])

    def test_refusal_matrix_and_force_does_not_bypass(self):
        invalid = [[], ['--ref', str(self.path)], ['--evidence', '/mnt/project-files/handoff.md'],
                   ['--evidence', 'deadbeef'], ['--ref', self.ref.replace('sample:', 'unknown:')],
                   ['--ref', self.ref[:-64] + '0' * 64],
                   ['--ref', self.ref.replace('handoffs/', '../handoffs/')],
                   ['--ref', self.ref.replace('handoffs/', '/handoffs/')],
                   ['--ref', self.ref.replace('worker--', 'someone--')],
                   ['--ref', self.ref.replace('#sha256:', '#sha256:BAD')],
                   ['--ref', self.ref.replace('@' + self.git('rev-parse', 'HEAD').strip(), '@deadbeef')]]
        for args in invalid:
            with self.subTest(args=args):
                self.post(*args, '--force', code=2)
        self.assertEqual(self.rows(), [])

    def test_external_refs_need_explicit_visible_degradation(self):
        local = self.root / 'worker--local.md'
        local.write_text('Local only')
        self.post('--ref', str(local), code=2)
        self.post('--ref', str(local), '--degrade-handoff', '', code=2)
        out = self.post('--ref', str(local), '--degrade-handoff', 'manual transfer pending')
        self.assertIn('DEGRADED HANDOFF', out)
        self.assertEqual(self.rows()[0]['handoff_degraded'], 'manual transfer pending')
        self.assertIn('DEGRADED HANDOFF: manual transfer pending', self.invoke('tail'))
        self.assertIn('DEGRADED_HANDOFF=manual transfer pending', bus.brief(self.rows()[0]))
        local.unlink()
        self.invoke('ack', self.rows()[0]['id'], '--from', 'lead', code=2)
        self.assertEqual(self.rows()[-1]['to'], 'worker')

    def test_uncommitted_ignored_and_oversized_files_refused(self):
        other = self.path.with_name('worker--new.md')
        other.write_text('Uncommitted')
        self.post('--evidence', self.reference(other), code=2)
        self.git('add', str(other))
        self.post('--evidence', self.reference(other), code=2)  # staged is not shared
        ignored = self.path.with_name('worker--ignored.md')
        ignored.write_text('Ignored')
        (self.repo / '.gitignore').write_text('handoffs/worker--ignored.md\n')
        self.post('--ref', self.reference(ignored), code=2)
        self.path.write_text('Changed after commit')
        self.post('--evidence', self.reference(), code=2)
        self.manifest.handoffs.max_bytes = 1
        self.post('--evidence', self.ref, code=2)

    def test_symlink_escape_and_unapproved_directory_refused(self):
        link = self.path.with_name('worker--link.md')
        link.symlink_to(self.path)
        self.commit_and_push()
        self.assertIn('symlinks are refused', self.post('--evidence', self.reference(link), code=2))
        external = self.root / 'external'
        external.write_text('external')
        link.unlink()
        link.symlink_to(external)
        self.commit_and_push()
        self.assertIn('symlinks are refused', self.post('--evidence', self.reference(link), code=2))
        other = self.repo / 'worker--outside.md'
        other.write_text('Outside handoffs directory')
        self.post('--evidence', self.reference(other), code=2)

    def test_consumer_missing_repo_bounces_without_ack_and_is_retry_safe(self):
        self.post('--evidence', self.ref)
        original = self.rows()[0]
        self.manifest.handoffs.repos['sample'] = str(self.root / 'consumer-has-no-checkout')
        for _ in range(2):
            out = self.invoke('ack', original['id'], '--from', 'lead', code=2)
            self.assertIn('bounced to worker', out)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertFalse(any(r['kind'] == 'ack' for r in rows))
        bounce = rows[-1]
        self.assertEqual((bounce['to'], bounce['owner'], bounce['re']), ('worker', 'worker', original['id']))
        pending = obligations.open_obligations(self.manifest, rows, time.time())
        self.assertEqual([(i['id'], i['owner']) for i in pending], [(bounce['id'], 'worker')])
        self.invoke('ack', bounce['id'], '--from', 'worker')
        self.assertEqual(obligations.open_obligations(self.manifest, self.rows(), time.time()), [])

    def test_digest_change_bounces_claim_before_claim_row(self):
        legacy = self.ref.split('@')[0] + '@sha256:' + self.ref.split('#sha256:')[1]
        self.post('--ref', legacy)
        original = self.rows()[0]
        self.path.write_text('Overwritten')
        self.commit_and_push()
        self.invoke('post', '--kind', 'claim', '--from', 'lead', '--to', 'worker',
                    '--re', original['id'], 'Picking up', code=2)
        self.assertIn('sha256 mismatch', self.rows()[-1]['handoff_bounce'])
        self.assertFalse(any(r['kind'] == 'claim' for r in self.rows()))

    def test_local_spill_is_not_required_for_portable_evidence_pickup(self):
        self.invoke('post', '--from', 'worker', '--to', 'lead', '--kind', 'handoff',
                    '--evidence', self.ref, 'Long detail ' * 100)
        original = self.rows()[0]
        from pathlib import Path
        self.assertTrue(Path(original['spill_ref']).name.startswith('worker--'))
        Path(original['spill_ref']).unlink()  # consumer does not share the poster's local spill
        self.invoke('ack', original['id'], '--from', 'lead')
        self.assertEqual(self.rows()[-1]['kind'], 'ack')

    def test_degraded_payload_digest_is_rechecked_and_bad_repo_ref_cannot_degrade(self):
        self.post('--ref', self.ref[:-64] + '0' * 64, '--degrade-handoff', 'cannot excuse corruption', code=2)
        local = self.root / 'worker--local.md'
        local.write_text('Original local handoff')
        self.post('--evidence', str(local), '--degrade-handoff', 'local transfer')
        original = self.rows()[0]
        local.write_text('Overwritten local handoff')
        self.invoke('ack', original['id'], '--from', 'lead', code=2)
        self.assertIn('sha256 mismatch', self.rows()[-1]['handoff_bounce'])

    def test_mcp_post_accepts_portable_evidence_and_preserves_digest_in_inbox(self):
        flag = mcp_server.home(self.manifest) / 'writes-enabled'
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        client = mcp_server.Client('fixture', 'worker', (), True, True)
        result = mcp_server.call_tool(self.manifest, 'post',
                                      dict(to='lead', kind='handoff', text='Ready', ref=self.ref, evidence=self.ref),
                                      client=client)
        self.assertEqual(self.rows()[0]['evidence'], self.ref, result)
        row = self.rows()[0]
        view = mcp_server.row_view(self.manifest, row, {}, {}, time.time())
        self.assertEqual(view['evidence'], self.ref)

    def test_connector_ack_uses_same_probe_and_bystander_cannot_bounce(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        self.manifest.handoffs.repos['sample'] = str(self.root / 'missing-consumer')
        with self.assertRaises(ManifestError):
            self.invoke('ack', original['id'], '--from', 'bystander', code=2)
        self.assertEqual(len(self.rows()), 1)
        with self.assertRaises(SystemExit) as exc:
            addressed.ack(self.manifest, original['id'], 'lead', 'done')
        self.assertEqual(exc.exception.code, 2)
        self.assertEqual(self.rows()[-1]['to'], 'worker')

    def test_connector_pickup_retains_received_state_when_a_note_is_supplied(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        addressed.ack(self.manifest, original['id'], 'lead', 'received', 'Payload verified')
        self.assertEqual(addressed.ack_state(original, addressed.replies(self.rows()), self.manifest), 'received')

    def test_publication_identity_preserves_ref_and_degrade_reason(self):
        self.post('--ref', self.ref, '--id', 'stable')
        self.assertIn('already posted', self.post('--ref', self.ref, '--id', 'stable'))
        with self.assertRaises(ManifestError):
            self.post('--ref', self.ref, '--id', 'stable', '--degrade-handoff', 'changed publication')
        self.path.unlink()
        self.assertIn('already posted', self.post('--ref', self.ref, '--id', 'stable'))

    def test_verified_answer_retry_returns_receipt_without_false_repair(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        args = ('post', '--from', 'lead', '--to', 'worker', '--kind', 'answer',
                '--re', original['id'], '--id', 'answer-receipt', 'Verified')
        self.invoke(*args)
        self.path.unlink()
        self.assertIn('already posted', self.invoke(*args))
        self.assertEqual(len(self.rows()), 2)
        with self.assertRaises(ManifestError):
            self.invoke(*args[:-1], 'Conflicting payload')
        self.assertEqual(len(self.rows()), 2)

    def test_config_types_and_validation(self):
        base = self.file.read_text()
        self.file.write_text(base + '\n[handoffs]\nrequired=true\nrepos={sample="repository"}\n'
                             'directories=["handoffs"]\nmax_bytes=262144\n')
        self.assertEqual(load(self.file).handoffs.repos, {'sample': 'repository'})
        for config in ('required=true', 'repos={sample=2}', 'directories=["../outside"]',
                       'max_bytes=0', 'directories="handoffs"', 'remote="--all"',
                       'fetch_timeout_seconds=0', 'fetch_timeout_seconds=121'):
            with self.subTest(config=config):
                self.file.write_text(base + '\n[handoffs]\n' + config + '\n')
                with self.assertRaises(ManifestError):
                    load(self.file)

    def test_reply_and_direct_answer_probe_before_publishing(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        self.manifest.handoffs.repos['sample'] = str(self.root / 'missing-consumer')
        self.invoke('reply', original['id'], '--from', 'lead', 'Received', code=2)
        self.invoke('post', '--from', 'lead', '--to', 'worker', '--kind', 'answer',
                    '--re', original['id'], 'Received', code=2)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[-1]['to'], 'worker')

    def test_unverified_foreign_reply_cannot_close_obligation_or_inbox(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        reply = dict(id='foreign', ts=original['ts'], frm='lead', to='worker',
                     kind='answer', re=original['id'], text='Picked up')
        rows = [original, reply]
        self.assertEqual(len(obligations.open_obligations(self.manifest, rows, time.time())), 1)
        self.assertIsNone(addressed.ack_state(original, addressed.replies(rows), self.manifest))
        self.invoke('post', '--from', 'lead', '--to', 'worker', '--kind', 'answer',
                    '--re', original['id'], 'Verified reply')
        self.assertEqual(self.rows()[-1]['handoff_verified'], original['id'])
        self.assertEqual(obligations.open_obligations(self.manifest, self.rows(), time.time()), [])

    def test_unverified_pr_or_subject_mentions_do_not_imply_portable_pickup(self):
        self.invoke('post', '--from', 'worker', '--to', 'lead', '--kind', 'handoff',
                    '--subject', 'work', '--ref', self.ref, 'PR #123 ready')
        original = self.rows()[0]
        later = dict(id='mention', ts=original['ts'], frm='lead', to='all', kind='msg',
                     text='PR #123 queued', subject='work')
        self.assertEqual(len(obligations.open_obligations(self.manifest, [original, later], time.time())), 1)

    def test_relative_degraded_evidence_keeps_posting_directory(self):
        before = os.getcwd()
        try:
            os.chdir(self.repo)
            self.post('--evidence', './handoffs/worker--task.md', '--degrade-handoff', 'local transfer')
            original = self.rows()[0]
            self.assertEqual(original['evidence'], str(self.path))
            self.assertIn(str(self.path), original['handoff_local_digests'])
        finally:
            os.chdir(before)
        self.invoke('ack', original['id'], '--from', 'lead')

    def test_stale_pickup_snapshot_cannot_duplicate_bounce(self):
        from coordkit.handoffs import check_pickup
        self.post('--ref', self.ref)
        snapshot = self.rows()
        self.manifest.handoffs.repos['sample'] = str(self.root / 'missing-consumer')
        for _ in range(2):
            self.assertFalse(check_pickup(self.manifest, snapshot[0], 'lead', snapshot))
        self.assertEqual(len(self.rows()), 2)

    def test_reassigned_consumer_can_probe_and_claim(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        self.invoke('ack', original['id'], '--from', 'lead', '--owner', 'reviewer')
        self.invoke('ack', original['id'], '--from', 'reviewer')
        self.assertEqual(obligations.open_obligations(self.manifest, self.rows(), time.time()), [])

    def test_stale_dirty_consumer_fetches_pinned_commit_without_checkout_changes(self):
        from pathlib import Path
        consumer = self.root / 'consumer'
        consumer.mkdir()
        def git(*args, check=True):
            result = subprocess.run(['git', '-C', str(consumer), *args], capture_output=True, timeout=10)
            if check:
                self.assertEqual(result.returncode, 0, result.stderr)
            return result
        git('init', '-q', '-b', 'stale-local')
        git('remote', 'add', 'origin', str(self.remote))
        git('fetch', '-q', 'origin', self.base)
        git('checkout', '-q', self.base)
        git('switch', '-qc', 'consumer-local')
        commit = self.ref.split('@')[1].split('#')[0]
        self.assertNotEqual(git('cat-file', '-e', commit, check=False).returncode, 0)
        (consumer / 'base.txt').write_text('dirty local changes\n')
        (consumer / 'untracked.txt').write_text('keep me\n')
        git('status', '--porcelain')
        index = (consumer / '.git/index').read_bytes()
        fetch_head = (consumer / '.git/FETCH_HEAD').read_bytes()
        head = git('rev-parse', 'HEAD').stdout
        branch = git('symbolic-ref', 'HEAD').stdout
        # Posting also reads objects: even the poster's HEAD need not contain the file.
        self.git('checkout', '-q', '--detach', self.base)
        self.assertFalse(self.path.exists())
        self.post('--ref', self.ref)
        original = self.rows()[0]
        self.manifest.handoffs.repos['sample'] = str(consumer)
        self.invoke('ack', original['id'], '--from', 'lead')
        self.assertEqual(git('cat-file', '-e', commit).returncode, 0)
        self.assertEqual(git('rev-parse', 'HEAD').stdout, head)
        self.assertEqual(git('symbolic-ref', 'HEAD').stdout, branch)
        self.assertEqual((consumer / '.git/index').read_bytes(), index)
        self.assertEqual((consumer / '.git/FETCH_HEAD').read_bytes(), fetch_head)
        self.assertEqual((consumer / 'base.txt').read_text(), 'dirty local changes\n')
        self.assertEqual((consumer / 'untracked.txt').read_text(), 'keep me\n')
        self.assertFalse((consumer / 'handoffs/worker--task.md').exists())
        self.assertEqual(ref_file(self.manifest, original).read_text(), 'Review this work.\n')
        self.assertEqual(mcp_server.read_ref(self.manifest, original, {})['text'], 'Review this work.\n')

    def test_legacy_form_uses_remote_default_tip_not_local_head_or_files(self):
        legacy = self.ref.split('@')[0] + '@sha256:' + self.ref.split('#sha256:')[1]
        self.git('checkout', '-q', '--detach', self.base)
        self.assertFalse(self.path.exists())
        self.post('--ref', legacy)
        original = self.rows()[0]
        self.invoke('ack', original['id'], '--from', 'lead')
        self.assertEqual(ref_file(self.manifest, original).read_text(), 'Review this work.\n')
        self.assertEqual(self.git('rev-parse', 'HEAD').strip(), self.base)
        self.assertFalse(self.path.exists())

    def test_fetch_timeout_and_remote_failure_bounce(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        self.manifest.handoffs.fetch_timeout_seconds = 3
        with patch('coordkit.git_objects.run', side_effect=ToolError('git', 6, 'timeout', 'tool_timeout')) as run:
            self.invoke('ack', original['id'], '--from', 'lead', code=2)
            self.assertEqual(run.call_args.kwargs['timeout'], 3)
        self.assertIn('fetch failed or timed out', self.rows()[-1]['handoff_bounce'])

    def test_configured_remote_is_used_for_pickup(self):
        self.post('--ref', self.ref)
        original = self.rows()[0]
        self.git('remote', 'rename', 'origin', 'archive')
        self.manifest.handoffs.remote = 'archive'
        self.invoke('ack', original['id'], '--from', 'lead')
