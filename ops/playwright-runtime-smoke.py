"""Synthetic headed smoke. Caller MUST isolate network and provide Xvfb/venv.

No installation, external navigation, HOME override, or exception details.
ops/deploy.sh retains this source in memory so rollback can remove this file.
"""
import os
from pathlib import Path
import tempfile


def verify_runtime():
    try:
        from app.minter import _ANTIBOT_ARGS
        from playwright.sync_api import sync_playwright

        # Per-user private state avoids Crashpad/XDG collisions without masking
        # HOME permissions. /tmp is writable also under the units' PrivateTmp.
        with tempfile.TemporaryDirectory(prefix="estrado-browser-", dir="/tmp") as temp:
            for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
                path = Path(temp) / key
                path.mkdir(mode=0o700)
                os.environ[key] = str(path)
            with sync_playwright() as playwright:
                cache = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).resolve(strict=True)
                executable = Path(playwright.chromium.executable_path).resolve(strict=True)
                executable.relative_to(cache)
                if not executable.is_file() or not os.access(executable, os.X_OK):
                    return 1
                browser = playwright.chromium.launch(
                    headless=False, args=_ANTIBOT_ARGS, timeout=30_000,
                )
                try:
                    page = browser.new_page()
                    page.set_content("<title>juristrack-playwright-smoke</title>", timeout=5_000)
                    return 0 if page.title() == "juristrack-playwright-smoke" else 1
                finally:
                    browser.close()
    except Exception:
        # Browser errors can contain paths, URLs, launch environments or secrets.
        # The shell reports only browser_unavailable and the fixed service user.
        return 1


if __name__ == "__main__":
    raise SystemExit(verify_runtime())
