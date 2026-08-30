"""Exercise real shell fixture setup, without executing guards or host services."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).with_name('test-resource-guards.sh')


def setup_functions():
    source = SOURCE.read_text()
    return '\n'.join(
        re.search(r'^' + name + r'\(\) \{.*?^\}', source, re.M | re.S).group()
        for name in ('write_stub', 'setup')
    )


def evaluate(command, root):
    return subprocess.run(
        ['bash', '-eu', '-c', setup_functions() + '\n' + command],
        env={'PATH': os.environ['PATH'], 'TMP': str(root),
             'EXPECTED_SHA': 'a' * 40, 'SECRET_SENTINEL': 'fixture-only'},
        text=True, capture_output=True, timeout=30,
    )


class FixtureIsolationTests(unittest.TestCase):
    def test_random_collision_never_reuses_or_overwrites_previous_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = evaluate('''
RANDOM=17
setup
first=$CASE_DIR
printf 'retain me' > "$STATE/claim-count"
RANDOM=17
setup
printf '%s\\n%s\\n' "$first" "$CASE_DIR"
''', root)
            self.assertEqual(result.returncode, 0, result.stderr)
            first, second = result.stdout.splitlines()
            self.assertNotEqual(first, second, 'setup reused a fixture directory')
            self.assertEqual((Path(first) / 'state/claim-count').read_text(), 'retain me')
            self.assertEqual((Path(second) / 'state/claim-count').read_text(), '0\n')

    def test_allocation_failure_does_not_create_partial_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = evaluate('''
mktemp() { return 1; }
setup
''', root)
            self.assertNotEqual(result.returncode, 0, 'setup ignored allocation failure')
            self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
