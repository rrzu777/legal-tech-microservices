import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('native_fixture', Path(__file__).with_name('fixture.py'))
fixture = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixture)


class FixtureConfigTests(unittest.TestCase):
    def test_dummy_environment_obeys_local_storage_and_idle_contract(self):
        values = fixture.fixture_values('# comment\nAPI_KEY\nCOOKIE_STORE_PATH\n')
        self.assertEqual(values['COOKIE_STORE_PATH'], '/var/lib/estrado-pjud/cookies.json')
        self.assertEqual(values['PJUD_PROCESS_OUTSIDE_OFFICE_HOURS'], 'false')
        self.assertEqual(values['PJUD_OFF_HOURS_VALIDATION_ONCE'], 'false')
        self.assertEqual(values['OJV_PROXY_URL'], '')
        self.assertEqual(values['API_KEY'], 'native-fixture-only')
        self.assertNotIn('# comment', values)
