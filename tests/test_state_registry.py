import unittest
from tests.state_registry import SOURCE, REGISTRY, LOGICAL, markers


class StateRegistryTests(unittest.TestCase):
    def test_every_marker_has_enter_exit_age_and_escalation(self):
        registered = {}
        for module, name, *contract in REGISTRY:
            with self.subTest(module=module, marker=name):
                self.assertEqual(len(contract),4)
                self.assertTrue(all(value.strip() for value in contract))
                self.assertNotIn(name,registered.setdefault(module,set()),'duplicate lifecycle row')
                registered[module].add(name)
        for path in SOURCE.glob('*.py'):
            with self.subTest(module=path.name):
                missing=markers(path.read_text())-registered.get(path.name,set())
                self.assertFalse(missing, f'New state needs lifecycle entry in tests/state_registry.py: {sorted(missing)}')
        for name,*contract in LOGICAL:
            with self.subTest(state=name):
                self.assertEqual(len(contract),4)
                self.assertTrue(all(value.strip() for value in contract))

    def test_registry_detects_new_marker_field_and_writer(self):
        source="""
def new_writer(state, plan, directory):
    state['new_latch'] = True
    plan['phase'] = 'new_phase'
    (directory / 'new.lock').write_text('held')
"""
        found=markers(source)
        self.assertTrue({'state:new_latch','phase:new_phase','new.lock','writer:new_writer'} <= found)
