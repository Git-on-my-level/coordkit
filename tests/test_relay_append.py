"""Core append contract for foreign rows, independent of relay transport."""
from unittest.mock import patch
from tests.support import FakeCase
from coordkit import bus


class RelayAppendTests(FakeCase):
    def test_foreign_rows_are_deduped_under_append_lock_and_never_mirrored(self):
        bus.configure(self.manifest)
        record = dict(id='foreign', ts=bus.now().isoformat(), frm='remote', to='worker', kind='ask',
                      text='test', text_hash='hash', origin='relay:remote', _relay={'seq': 4})
        index = bus.ReplayIndex(self.manifest.paths.bus_log)
        with patch('coordkit.wake.notify') as wake, patch('coordkit.adapters.builtin.Anx.project') as mirror:
            bus.append([record], replay_index=index)
            bus.append([dict(record)], replay_index=index)
            bus.append([dict(record, _relay={'seq': 5})], replay_index=index)
            self.assertEqual(len(bus.read()), 2)
            self.assertEqual(sum(bool(c.args[1]) for c in wake.call_args_list), 2)
            mirror.assert_not_called()
        # Site namespaces allow two hosts to have the same short row ID.
        bus.append([dict(record, origin='relay:other')], project=False)
        self.assertEqual(len(bus.read()), 3)

    def test_index_reads_history_once_then_only_new_rows_and_rebuilds_after_restart(self):
        bus.configure(self.manifest)
        base = dict(ts=bus.now().isoformat(), frm='lead', to='all', kind='fyi', text='old')
        bus.append([dict(base, id=str(i)) for i in range(100)], project=False)
        index = bus.ReplayIndex(self.manifest.paths.bus_log)
        counts = []
        read = index.reader.read
        def counted(start):
            result = read(start)
            counts.append(len(result[1]))
            return result
        rec = dict(base, id='collision', origin='relay:remote', _relay={'seq': 0})
        with patch.object(index.reader, 'read', side_effect=counted), patch.object(bus, 'read_path', side_effect=AssertionError('full reread')):
            bus.append([rec], project=False, replay_index=index)
            bus.append([dict(rec, _relay={'seq': 1})], project=False, replay_index=index)
            bus.append([dict(rec)], project=False, replay_index=index)
        self.assertEqual(counts, [100, 1, 1])
        rebuilt = bus.ReplayIndex(self.manifest.paths.bus_log)
        bus.append([dict(rec)], project=False, replay_index=rebuilt)
        self.assertEqual(len(bus.read()), 102)

    def test_wake_scan_retries_relay_rows_so_a_crash_cannot_lose_the_wake(self):
        import contextlib
        import io
        from coordkit import wake
        bus.configure(self.manifest)
        self.manifest.paths.bus_log.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.paths.bus_log.touch()
        with contextlib.redirect_stdout(io.StringIO()):
            wake.scan(self.manifest)  # initialize the scanner before the new row
        rec = dict(id='remote', ts=bus.now().isoformat(), frm='lead', to='worker', kind='ask',
                   origin='relay:remote', _relay={'seq': 0})
        with patch('coordkit.wake.notify') as notify:
            bus.append([rec], project=False)
            notify.assert_called_once()
            notify.reset_mock()
            with contextlib.redirect_stdout(io.StringIO()):
                wake.scan(self.manifest)
            # The repeat reaches wake policy, whose debounce keeps it to one wake.
            self.assertEqual([r['id'] for r in notify.call_args.args[1]], ['remote'])

    def test_doctor_prints_one_relay_warning_without_failing_the_check(self):
        import contextlib
        import io
        from coordkit import lifecycle
        out = io.StringIO()
        with patch('coordkit.relay.health', return_value='WARN suppressed_rows: possible reused site'), contextlib.redirect_stdout(out):
            lifecycle.doctor(self.manifest)
        lines = [line for line in out.getvalue().splitlines() if line.startswith('DOCTOR relay ')]
        self.assertEqual(lines, ['DOCTOR relay warn WARN suppressed_rows: possible reused site'])
