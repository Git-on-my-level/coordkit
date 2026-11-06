import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch
from coordkit import bus
from coordkit.tail import TailReader
from tests.support import FakeCase


class CompatibilityTests(FakeCase):
    def setUp(self):
        super().setUp()
        self.manifest.bus.mirror = False
        bus.configure(self.manifest)

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            bus.main(list(args), self.manifest)
        return output.getvalue()

    def test_v2_sequences_preserve_legacy_and_physical_lines(self):
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True)
        old = b'{"ts":"2000-01-01T00:00:00Z","frm":"worker","to":"all","kind":"hold"}\nbroken\n'
        path.write_bytes(old)
        self.invoke('post','--from','worker','--to','all','hello')
        self.assertTrue(path.read_bytes().startswith(old))
        row = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual((row['v'],row['seq']), (2,2))
        self.assertEqual(bus.read()[0]['v'],1)

    def test_multito_dedup_and_reply_ack(self):
        self.invoke('post','--from','worker','--to','lead','--to','all','hello')
        rows = bus.read()
        self.assertEqual([r['to'] for r in rows], ['lead','all'])
        self.assertTrue(self.invoke('post','--from','worker','--to','all','hello').startswith('DEDUPED '))
        self.invoke('post','--from','lead','--to','worker','--kind','ruling','--owner','worker','do work')
        ruling = next(r for r in bus.read() if r['kind']=='ruling')
        self.invoke('reply',ruling['id'],'--from','worker','done')
        self.assertFalse(bus.open_rulings(bus.read()))
        self.assertTrue(any(r['kind']=='ack' and r['re']==ruling['id'] for r in bus.read()))

    def test_reader_state_dual_write_prevents_rollback_replay(self):
        self.manifest.paths.bus_log = self.root/'shared/bus.jsonl'
        bus.configure(self.manifest)
        old = self.manifest.paths.root/'state/cursors/worker.inbox'
        old.parent.mkdir(parents=True); old.write_text('3\n')
        self.assertEqual(bus.get_cursor('worker','inbox',0),3)
        bus.set_cursor('worker','inbox',4)
        self.assertEqual(old.read_text(),'4\n')
        # The v0.1 wrapper reads the old cursor directly on rollback.
        self.assertEqual(int(old.read_text()), bus.get_cursor('worker','inbox',0))
        self.assertEqual(bus.cursor_path('worker','inbox').read_text(),'4\n')

    def test_20k_incremental_parse_gate_partial_and_replaced_log(self):
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True)
        row = json.dumps(dict(ts='2000-01-01T00:00:00Z', frm='worker',to='all',kind='msg',id='x'))+'\n'
        path.write_text(row*20000)
        with patch.object(bus,'normalize', wraps=bus.normalize) as parse:
            total, rows = bus.read_from(10**12)
            self.assertEqual((total,rows),(20000,[]))
            with path.open('a') as f: f.write(row)
            total, rows = bus.read_from(total)
            self.assertEqual((total,len(rows)),(20001,1))
            self.assertEqual(parse.call_count,1)
        with path.open('a') as f: f.write('{')
        self.assertEqual(bus.read_from(total),(total,[]))
        replacement=path.with_suffix('.new'); replacement.write_text(row); replacement.replace(path)
        self.assertEqual(bus.read_from(0)[0],1)

    def test_owner_answer_only_closes_later_ruling(self):
        ruling={'kind':'ruling','owner':'worker','id':'r'}
        wrong={'kind':'answer','frm':'other','re':'r'}
        correct=dict(wrong,frm='worker')
        self.assertTrue(bus.open_rulings([ruling,wrong]))
        self.assertTrue(bus.open_rulings([correct,ruling]))
        self.assertFalse(bus.open_rulings([ruling,correct]))

    def test_distinct_structured_holds_are_not_deduplicated(self):
        for pr in ('123','124'):
            self.invoke('post','--from','worker','--to','all','--kind','hold','--pr',pr,'hold')
        self.assertEqual([r['prs'] for r in bus.read()],[['123'],['124']])

    def test_retirement_guards_raw_append_and_mcp_status(self):
        from coordkit import wake, mcp_server
        from coordkit.manifest import ManifestError
        with contextlib.redirect_stdout(io.StringIO()): wake.change(self.manifest,'worker','lead')
        with self.assertRaisesRegex(ManifestError,'lane_retired'):
            bus.append([dict(ts=bus.now().isoformat(),id='raw',frm='lead',to='worker',kind='msg',text='x')])
        rows=mcp_server.agent_rows(self.manifest,[],{},bus.now().timestamp())['agents']
        self.assertEqual(next(r for r in rows if r['name']=='worker')['status'],'retired')

    def test_post_append_sink_observes_committed_rows_outside_lock(self):
        from coordkit import adapters
        import fcntl
        seen=[]
        class Sink:
            def notify(self,manifest,rows):
                with manifest.paths.bus_log.open('r') as stream:
                    fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    disk=[json.loads(line) for line in stream]
                seen.append((rows[0]['seq'],disk[-1]['id']))
                raise RuntimeError('sink offline')
        with patch.object(adapters,'SINKS',[Sink()]),self.assertWarnsRegex(RuntimeWarning,'post_append_sink_failed'):
            self.invoke('post','--from','worker','--to','all','durable')
        self.assertEqual(seen,[(0,bus.read()[0]['id'])])

    def test_cross_process_sequence_assignment(self):
        import os, subprocess, sys
        root=Path(__file__).resolve().parents[1]/'src'
        program='''from coordkit import bus
from coordkit.manifest import load
import sys
bus.configure(load(sys.argv[1]))
for i in range(8):
 bus.append([dict(ts=bus.now().isoformat(), id=sys.argv[2]+str(i), frm='worker',to='all',kind='msg',text='x')])
'''
        self.manifest.bus.mirror=False
        with self.file.open('a') as stream: stream.write('\n[paths]\nroot="sequence"\n')
        # The bus section already exists in BASE; disable the mirror in both child manifests.
        self.file.write_text(self.file.read_text().replace('live_check = false','live_check = false\nmirror = false'))
        children=[subprocess.Popen([sys.executable,'-c',program,str(self.file),str(i)],env=dict(os.environ,PYTHONPATH=str(root)),stdout=subprocess.PIPE,stderr=subprocess.PIPE) for i in range(2)]
        try:
            for child in children:
                out,err=child.communicate(timeout=20)
                self.assertEqual(child.returncode,0,err.decode())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill();child.wait()
        from coordkit.manifest import load
        bus.configure(load(self.file))
        self.assertEqual([r['seq'] for r in bus.read()],list(range(16)))

    def test_cursor_same_location_written_once(self):
        with patch.object(bus, 'replace_text', wraps=bus.replace_text) as replace:
            bus.set_cursor('worker', 'wait', 5)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(bus.get_cursor('worker', 'wait', 0), 5)

    def test_full_and_tail_readers_share_physical_complete_line_contract(self):
        path = self.manifest.paths.bus_log
        path.parent.mkdir(parents=True)
        first = dict(ts='2000-01-01T00:00:00Z', frm='worker', to='all', kind='msg', id='first', text='a\u2028b')
        second = dict(first, id='second', text='unicode ü')
        encoded = json.dumps(second, ensure_ascii=False).encode()
        prefix = (json.dumps(first, ensure_ascii=False)+'\n').encode()
        split = encoded.index('ü'.encode()) + 1
        path.write_bytes(prefix + encoded[:split])
        self.assertEqual(len(bus.read()), 1)
        self.assertEqual(bus.read_from(0), (1, list(enumerate(bus.read()))))
        # Valid JSON without the terminating newline is also uncommitted.
        path.write_bytes(prefix + encoded)
        for complete in (True, False):
            self.assertEqual(len(bus.read_path(path, complete=complete)), 1)
        self.assertEqual(bus.read_from(0), (1, list(enumerate(bus.read()))))
        with path.open('ab') as stream:
            stream.write(b'\n')
        self.assertEqual(len(bus.read()), 2)
        self.assertEqual(bus.read_from(0), (2, list(enumerate(bus.read()))))
        self.assertEqual(bus.read()[-1]['text'], 'unicode ü')

    def test_wire_constants_come_from_one_module(self):
        from coordkit import bus_protocol
        for name in ('KINDS', 'STATES', 'BLOCKER_CLOSERS', 'WAKE_KINDS'):
            self.assertIs(getattr(bus, name), getattr(bus_protocol, name))
        default_cap = bus_protocol.TEXT_CAP
        self.manifest.bus.text_cap = default_cap + 10
        bus.configure(self.manifest)
        self.assertEqual(bus.TEXT_CAP, default_cap + 10)
        self.assertEqual(bus_protocol.TEXT_CAP, default_cap)

    def test_harvest_mark_advances_rollback_cursor_too(self):
        self.manifest.paths.bus_log = self.root/'shared/bus.jsonl'
        bus.configure(self.manifest)
        self.invoke('post', '--from', 'worker', '--to', 'all', '--kind', 'lesson', 'learned')
        self.invoke('harvest', '--mark')
        old = self.manifest.paths.root/'state/harvest.cursor'
        new = self.manifest.paths.bus_log.parent/'state/harvest.cursor'
        self.assertEqual(old.read_text(), '1\n')
        self.assertEqual(new.read_text(), old.read_text())

    def test_reader_cursor_reupgrade_uses_progress_from_either_wrapper(self):
        self.manifest.paths.bus_log = self.root/'shared/bus.jsonl'
        bus.configure(self.manifest)
        bus.append([dict(ts=bus.now().isoformat(), id=text, frm='lead', to='worker',
                         kind='msg', topic='general', text=text) for text in ('first', 'second', 'third')])
        for kind in ('inbox', 'wait'):
            old = self.manifest.paths.root/'state/cursors'/f'worker.{kind}'
            new = bus.cursor_path('worker', kind)
            bus.set_cursor('worker', kind, 1)  # v0.2
            old.write_text('2\n')  # rolled-back v0.1 advances only its copy
            self.assertEqual(bus.get_cursor('worker', kind, 0), 2)  # re-upgrade
            self.assertEqual(new.read_text(), '1\n')  # selection is read-only
            new.write_text('3\n')  # new writer advances before its legacy copy
            self.assertEqual(bus.get_cursor('worker', kind, 0), 3)
            self.assertEqual(old.read_text(), '2\n')
        # Consumption holds the write gate and cursor lock and advances both.
        self.assertEqual(self.invoke('inbox', 'worker', '--unread'), '')
        self.assertEqual(old.with_name('worker.inbox').read_text(), '3\n')
        self.assertEqual(bus.cursor_path('worker', 'inbox').read_text(), '3\n')

    def test_harvest_reupgrade_does_not_replay_legacy_progress(self):
        self.manifest.paths.bus_log = self.root/'shared/bus.jsonl'
        bus.configure(self.manifest)
        for text in ('first', 'second', 'third'):
            self.invoke('post', '--from', 'worker', '--to', 'all', '--kind', 'lesson', text)
        old = self.manifest.paths.root/'state/harvest.cursor'
        new = self.manifest.paths.bus_log.parent/'state/harvest.cursor'
        for path in (old, new):
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text('1\n')
        old.write_text('2\n')  # v0.1 consumed the second lesson
        output = self.invoke('harvest')
        self.assertNotIn('first', output)
        self.assertNotIn('second', output)
        self.assertIn('third', output)
        self.assertEqual(old.read_text(), '2\n')
        self.assertEqual(new.read_text(), '1\n')  # an unmarked preview cannot repair
        self.invoke('harvest', '--mark')
        self.assertEqual(old.read_text(), '3\n')
        self.assertEqual(new.read_text(), '3\n')
        self.assertIn('nothing to harvest', self.invoke('harvest'))

    def test_migration_ignores_invalid_positions_without_writing(self):
        self.manifest.paths.bus_log = self.root/'shared/bus.jsonl'
        bus.configure(self.manifest)
        self.invoke('post', '--from', 'worker', '--to', 'all', 'row')
        old = self.manifest.paths.root/'state/cursors/worker.inbox'
        new = bus.cursor_path('worker', 'inbox')
        for path in (old, new):
            path.parent.mkdir(parents=True, exist_ok=True)
        for invalid in ('broken', '-1', '100'):
            for invalid_path, valid_path in ((old, new), (new, old)):
                invalid_path.write_text(invalid); valid_path.write_text('1\n')
                self.assertEqual(bus.get_cursor('worker', 'inbox', 0), 1)
                self.assertEqual(invalid_path.read_text(), invalid)
                self.assertEqual(valid_path.read_text(), '1\n')
        self.manifest.mode = 'shadow'
        old.write_text('1\n'); new.write_text('0\n')
        self.assertEqual(bus.get_cursor('worker', 'inbox', 0), 1)
        self.assertEqual(new.read_text(), '0\n')

    def test_unmarked_harvest_cannot_regress_a_concurrent_mark(self):
        self.manifest.paths.bus_log = self.root/'shared/bus.jsonl'
        bus.configure(self.manifest)
        bus.append([dict(ts=bus.now().isoformat(), id=str(i), frm='worker', to='all',
                         kind='lesson', text=str(i)) for i in range(60)])
        old = self.manifest.paths.root/'state/harvest.cursor'
        new = self.manifest.paths.bus_log.parent/'state/harvest.cursor'
        for path in (old, new):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('50\n')
        actual_read = bus.read_from
        marked = False
        def interleave(start):
            nonlocal marked
            if start == 10 ** 18 and not marked:
                # The preview has already captured both positions at 50.
                marked = True
                self.invoke('harvest', '--mark')
                self.assertEqual(old.read_text(), '60\n')
                self.assertEqual(new.read_text(), '60\n')
            return actual_read(start)
        with patch.object(bus, 'read_from', side_effect=interleave):
            self.invoke('harvest')
        self.assertTrue(marked)
        self.assertEqual(old.read_text(), '60\n')
        self.assertEqual(new.read_text(), '60\n')

    def test_unmarked_harvest_never_creates_or_repairs_cursor_files(self):
        self.manifest.paths.bus_log = self.root/'shared/bus.jsonl'
        bus.configure(self.manifest)
        self.invoke('post', '--from', 'worker', '--to', 'all', '--kind', 'lesson', 'one')
        old = self.manifest.paths.root/'state/harvest.cursor'
        new = self.manifest.paths.bus_log.parent/'state/harvest.cursor'
        with patch.object(bus, 'replace_text', wraps=bus.replace_text) as write:
            self.invoke('harvest')
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text('1\n')
            self.invoke('harvest')
        write.assert_not_called()
        self.assertEqual(old.read_text(), '1\n')
        self.assertFalse(new.exists())
