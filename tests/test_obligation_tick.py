"""Old launchd entry point remains a thin route to the shared sweep."""
from pathlib import Path
import unittest


class ObligationTickTests(unittest.TestCase):
    def test_compat_wrapper_runs_sweep_without_its_own_state_or_alerts(self):
        source = (Path(__file__).resolve().parents[1]/'examples/omi/bin/obligations-tick.sh').read_text()
        self.assertIn('exec env PYTHONPATH=', source)
        self.assertIn('coordkit.toml sweep "$@"', source)
        self.assertNotIn('UNPICKED', source)
        self.assertNotIn('obligations-seen', source)
