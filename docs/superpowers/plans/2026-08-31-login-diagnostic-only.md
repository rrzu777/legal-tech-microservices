# Login Diagnostic Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Produce a reviewed, independently releasable diagnostic backport without installing the new worker maintenance system.

**Architecture:** Retain the complete legacy runtime from commit3a599e07a3c43ce6cc237e8f4157c0c1afe5210f. Backport only PR117's passive post-submit observations, leaving authentication decisions, deadlines, cookies, accounting and claims unchanged. This is not the functional sync fix and does not authorize deployment.

**Tech Stack:** Python, existing Playwright1.62.0, pytest and offline intercepted Chromium.

**Spec:** The bounded diagnostic contract below, corresponding to the approved PR117 observation scope.

## Global Constraints

- Runtime base is3a599e07a3c43ce6cc237e8f4157c0c1afe5210f, not current main.
- Diagnostic source is7118911cccd6d59c09461c186b487835c0f1c9da.
- Only the five Python files listed in Task1 and this plan may change.
- Retain `manager = async_playwright()`; do not import `owned_playwright` or add maintenance dependencies.
- Do not change deadlines, locators, authentication acceptance, credentials, cookies, budgets, retries, schemas, service units or deploy scripts.
- Observations remain finite counters/categories only; no raw URLs, bodies, headers, cookies, credentials or exception messages in logs.
- No network, PJUD, browser profile, VPS, DB, service lifecycle, secret access or package installation for local validation.
- Do not stage or commit until the controller completes independent review and verification. No push or deploy by the implementer.
- Local approval is not production approval; runtime cutover and a real login remain separately verified operations.

### Task 1: Backport the existing passive diagnostic, preserving the legacy runtime

**Files:**
- Modify:`estrado-pjud-service/app/ojv/browser_login.py`.
- Create:`estrado-pjud-service/app/ojv/submit_diagnostics.py`.
- Modify:`estrado-pjud-service/tests/test_ojv_browser_login.py`.
- Modify:`estrado-pjud-service/tests/test_ojv_browser_login_dom.py`.
- Create:`estrado-pjud-service/tests/test_ojv_submit_diagnostics.py`.
- Preserve this plan as controller-owned documentation.

**Interfaces:**
- Consumes unchanged `login_official_ojv(rut, password, *, proxy_url, user_agent)` and legacy `async_playwright()`.
- Produces PR117's `_SubmitProbe` observations without changing the return/error contract.

- [ ] **Step 1: Verify exact inputs and a clean implementation surface.**

Run `git rev-parse HEAD` and `git status --short`. Require the base above and only this untracked plan. Compare the five source files against the controller's frozen compatibility snapshot supplied with the brief. Four files are byte-identical to the diagnostic commit; `browser_login.py` differs from it only by retaining these legacy lines:

```python
# No import from app.playwright_runtime.
manager = async_playwright()
```

- [ ] **Step 2: Backport the existing adapter tests first and demonstrate missing observations.**

Use `apply_patch` for the changes to `tests/test_ojv_browser_login.py` from the exact diagnostic source. Run its `test_official_adapter_uses_observed_ui_and_returns_owned_typed_cookies` against legacy production code in an env-free source copy. Expected RED: emitted page event subscriptions are the legacy two, rather than the diagnostic subscriptions. Import or environment errors are not a valid RED.

- [ ] **Step 3: Apply the remaining exact backport with `apply_patch`.**

Copy the four remaining frozen files. Preserve the legacy runtime import/manager described above. Do not bring any other difference from main. Compare all five hashes to the frozen compatibility manifest and verify `git diff --check`.

- [ ] **Step 4: Verify GREEN and offline compatibility.**

Run the same adapter test, then these five modules in a complete exact-source copy excluding dotenv except tracked `.env.example`: `test_ojv_browser_login.py`, `test_ojv_submit_diagnostics.py`, `test_ojv_browser_login_dom.py`, `test_ojv_session.py`, `test_import_worker.py`. Use the existing canonical venv, `env -i`, the supplied Python external-network audit plugin, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, explicit pytest-asyncio plugin and `PJUD_RUN_BROWSER_TESTS=1`; all Chromium requests must remain intercepted and service workers blocked. No downloads or inherited application environment.

- [ ] **Step 5: Freeze for independent review.**

Record exact hashes, full diff, RED/GREEN commands/output and constraints in the task report. Review spec compliance and code quality, then whole-branch compatibility. The controller runs the complete suite and commits only after review closes. Retain any known operational limitations explicitly; a test pass is not a successful PJUD login.
