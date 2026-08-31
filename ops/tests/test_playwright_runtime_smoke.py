"""Runtime probe contracts; browser boundary is fake, paths and XDG are real."""
import os
from pathlib import Path
import runpy
import stat
import tempfile
import types
import unittest
from unittest.mock import patch


PROBE = Path(__file__).resolve().parents[1] / "playwright-runtime-smoke.py"


class RuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PROBE.is_file(), "runtime probe not implemented")
        self.verify = runpy.run_path(str(PROBE))["verify_runtime"]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.binary = self.cache / "resolved-revision" / "chrome"
        self.binary.parent.mkdir()
        self.binary.touch(mode=0o755)
        self.args = ["--actual-minter-arg"]
        self.launched = []
        self.private_dirs = []
        self.home_before = os.environ.get("HOME")
        self.title = "juristrack-playwright-smoke"
        self.launch_error = False
        self.closed = False
        browser = types.SimpleNamespace(new_page=lambda: page, close=self.close)
        page = types.SimpleNamespace(set_content=lambda *_a, **_k: None,
                                     title=lambda: self.title)

        def launch(**kwargs):
            self.launched.append(kwargs)
            self.assertEqual(os.environ.get("HOME"), self.home_before)
            for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
                path = Path(os.environ[key])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                self.assertEqual(path.stat().st_uid, os.getuid())
                self.private_dirs.append(path)
            if self.launch_error:
                raise RuntimeError("secret-provider-url")
            return browser

        self.chromium = types.SimpleNamespace(executable_path=str(self.binary), launch=launch)
        runtime = types.SimpleNamespace(chromium=self.chromium)

        class Manager:
            def __enter__(inner):
                return runtime

            def __exit__(inner, *_args):
                return False

        modules = {
            "app.minter": types.SimpleNamespace(_ANTIBOT_ARGS=self.args),
            "playwright.sync_api": types.SimpleNamespace(sync_playwright=Manager),
        }
        self.addCleanup(patch.stopall)
        patch.dict("sys.modules", modules).start()
        patch.dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(self.cache)).start()

    def close(self):
        self.closed = True

    def test_uses_resolved_binary_headed_real_args_and_private_xdg_then_cleans(self):
        self.assertEqual(self.verify(), 0)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.launched[0]["headless"], False)
        self.assertEqual(self.launched[0]["args"], self.args)
        self.assertTrue(self.closed)
        self.assertTrue(all(not p.exists() for p in self.private_dirs))

    def test_old_binary_does_not_satisfy_exact_revision(self):
        self.chromium.executable_path = str(self.cache / "new-revision" / "chrome")
        self.assertEqual(self.verify(), 1)
        self.assertEqual(self.launched, [])

    def test_non_executable_binary_fails_before_launch(self):
        self.binary.chmod(0o644)
        self.assertEqual(self.verify(), 1)
        self.assertEqual(self.launched, [])

    def test_resolved_path_outside_cache_fails_before_launch(self):
        self.chromium.executable_path = "/bin/sh"
        self.assertEqual(self.verify(), 1)
        self.assertEqual(self.launched, [])

    def test_launch_exception_is_closed_and_private_dirs_removed(self):
        self.launch_error = True
        self.assertEqual(self.verify(), 1)
        self.assertTrue(all(not p.exists() for p in self.private_dirs))

    def test_wrong_page_title_fails_and_closes_browser(self):
        self.title = "unexpected"
        self.assertEqual(self.verify(), 1)
        self.assertTrue(self.closed)


if __name__ == "__main__":
    unittest.main()
