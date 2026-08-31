"""Fast red/green cases for native contracts, without invoking host systemd."""
import os
from pathlib import Path
import re
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'resource-guards.sh'


def evaluate(command, **values):
    source = SOURCE.read_text()
    functions = []
    for name in ('fail', 'show_contract', 'verify_tracker_environment_files', 'verify_monitor_configuration',
                 'scoped_systemctl', 'read_unit_state', 'read_correlated_unit_activity',
                 'read_monitor_restore_activity', 'run_apply'):
        match = re.search(r'^' + name + r'\(\) \{.*?^\}', source, re.M | re.S)
        if match:
            functions.append(match.group())
    stubs = '''
exec 3>&2
systemctl_bin=fake_systemctl
busctl_bin=fake_busctl
null_file=/dev/null
EXIT_ERROR=1
recovered=0
fake_busctl() {
  case "${@: -1}" in
    DropInPaths) printf '%s\\n' "$DROPINS" ;;
    NeedDaemonReload) printf '%s\\n' "$RELOAD" ;;
    *) printf '%s\\n' "$BUS_OUTPUT" ;;
  esac
  return "$BUS_RC"
}
fake_systemctl() {
  case "$1" in
    show) printf '%s\\n' "$SHOW" ;;
    is-active)
      if [ "$recovered" = 1 ]; then echo inactive; return 3; fi
      echo failed; return "$ACTIVE_RC" ;;
    is-enabled) echo not-found; return 4 ;;
    stop) echo STOP >&3; return "$STOP_RC" ;;
    reset-failed) echo RESET >&3; recovered=$RESET_EFFECT; return "$RESET_RC" ;;
    *) return 99 ;;
  esac
}
'''
    env = dict(PATH=os.environ['PATH'], BUS_OUTPUT='a(sb) 0', BUS_RC='0',
               SHOW='LoadState=not-found\nFragmentPath=', ACTIVE_RC='4',
               STOP_RC='0', RESET_RC='0', RESET_EFFECT='1', DROPINS='as 0', RELOAD='b false')
    env.update(values)
    return subprocess.run(['bash', '-c', stubs + '\n'.join(functions) + '\n' + command],
                          env=env, text=True, capture_output=True)


class NativeContractTests(unittest.TestCase):
    def test_monitor_override_gate_accepts_only_typed_empty_and_fresh_config(self):
        self.assertEqual(evaluate('verify_monitor_configuration 0').returncode, 0)
        for values in ({'DROPINS': 'as 1 "/private/override.conf"'}, {'DROPINS': ''},
                       {'RELOAD': 'b true'}, {'RELOAD': ''}, {'BUS_RC': '1'}):
            with self.subTest(values=values):
                result = evaluate('verify_monitor_configuration 0', **values)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('/private', result.stderr)

    def test_empty_array_uses_typed_dbus_property(self):
        result = evaluate('verify_tracker_environment_files')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dbus_empty_only_exact_and_successful(self):
        for output, rc in [('as 0', '0'), ('', '0'), ('a(sb) 1 "/private" true', '0'),
                           ('a(sb) 0\na(sb) 0', '0'), ('a(sb) 0', '1')]:
            with self.subTest(output=output, rc=rc):
                self.assertNotEqual(evaluate('verify_tracker_environment_files',
                                            BUS_OUTPUT=output, BUS_RC=rc).returncode, 0)

    def test_unrelated_missing_property_stays_fail_closed(self):
        self.assertNotEqual(evaluate('show_contract legaltech-monitor.service User root', SHOW='').returncode, 0)

    def test_contract_error_identifies_only_unit_property(self):
        result = evaluate('show_contract legaltech-monitor.service User root', SHOW='User=secret-sentinel')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('legaltech-monitor.service User', result.stderr)
        self.assertNotIn('secret-sentinel', result.stderr)

    def test_removed_failed_timer_can_recover_after_restore(self):
        result = evaluate('read_monitor_restore_activity legaltech-monitor.timer absent 1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'inactive')
        self.assertEqual(result.stderr.count('RESET'), 1)

    def test_failed_rc4_requires_exact_absent_timer_and_loaded_metadata(self):
        for unit, enabled, capability, show in [
            ('legaltech-monitor.service', 'absent', '1', 'LoadState=not-found\nFragmentPath='),
            ('legaltech-monitor.timer', 'enabled', '1', 'LoadState=not-found\nFragmentPath='),
            ('legaltech-monitor.timer', 'absent', '0', 'LoadState=not-found\nFragmentPath='),
            ('estrado-pjud-worker.service', 'absent', '1', 'LoadState=not-found\nFragmentPath='),
            ('legaltech-monitor.timer', 'absent', '1', 'LoadState=loaded\nFragmentPath='),
            ('legaltech-monitor.timer', 'absent', '1', 'LoadState=not-found\nFragmentPath=/unexpected'),
            ('legaltech-monitor.timer', 'absent', '1', 'LoadState=not-found'),
        ]:
            with self.subTest(unit=unit, enabled=enabled, capability=capability, show=show):
                result = evaluate(f'read_monitor_restore_activity {unit} {enabled} {capability}', SHOW=show)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('RESET', result.stderr)

    def test_recovery_operations_and_final_read_must_succeed(self):
        for values in ({'STOP_RC': '1'}, {'RESET_RC': '1'}, {'RESET_EFFECT': '0'}):
            with self.subTest(values=values):
                result = evaluate('read_monitor_restore_activity legaltech-monitor.timer absent 1', **values)
                self.assertNotEqual(result.returncode, 0)

    def test_apply_failure_reports_phase_before_any_rollback(self):
        result = evaluate('''
run_preflight() { return 0; }
validate_hermes_inventory() { echo 1002; }
build_managed_paths() { :; }
run_apply_steps() { apply_phase=postflight; mutation_started=1; backup_dir=/fixture; return 1; }
automatic_rollback() { echo rollback-marker >&2; }
run_apply
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('apply failed in phase: postflight', result.stderr)
        self.assertLess(result.stderr.index('phase: postflight'), result.stderr.index('rollback-marker'))


if __name__ == '__main__':
    unittest.main()
