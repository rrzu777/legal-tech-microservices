# Worker Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Review uncommitted changes before controller commits.

**Goal:** Install the approved cooperative maintenance protocol safely on the legacy VPS as a prerequisite to the full four-stage user goal.

**Architecture:** Separate read-only evidence, stopped-only initial state installation, and authenticated first release. Existing guards/deploy remain fail-closed and unchanged; actual service shutdown and rollout remain controller-owned.

**Tech Stack:** Python3.12stdlib, Ubuntu24.04/systemd255, existing maintenance_store/operator, pytest, isolated Linux/HVF laboratory.

**Spec:** `docs/superpowers/specs/2026-08-31-worker-bootstrap-design.md`.

## Global Constraints

- Local source changes only until reviewed/tested; subagents never access VPS, launchVM/browser, publish, commit or spawn agents.
- Main base70c20583d75acda7f23f6d7a36901eaf3bf46c3b; preserve PR114–116 and coordinated upstream deploy changes.
- Runtime mutations only Santiago20:00–03:59; no forcedkill or paidPJUD/mint/sync/proxy/Telegram, no flags/migrations/secrets in outputs.
- Unknown telemetry or incomplete closure blocks installation; stale/absent ACK is not bootstrap proof.
- Controlroot:estrado0750/files0640/nlink1/nosymlinks; ACKworker0700/0600; globalroot0600; never replace existing lock.
- Full goal additionally requires production guards,24h and naturalPJUDcycle, capacity/guest isolation and Ricardo access; this plan does not claim those complete.

### Task 1: Read-only bootstrap audit

**Files:** Create `ops/bootstrap-audit.py`, `ops/tests/test_bootstrap_audit.py`, `ops/bootstrap-worker-maintenance.md` (audit section).

**Interfaces:** Script `bootstrap-audit.py --expected-sha <40hex>`; production fixed `/opt/legal-tech-microservices`, root-only. Python `audit(config, runner, opener, now)` may accept explicit dependencies for unit tests, but CLI has no arbitrary URL/path/test-mode override. Return finite JSON: version,observed_at,sha,tree_clean,services,health,work_counts,heartbeat,ready_for_shutdown_review. `ready_for_shutdown_review` is advisory, never mutation authority.

- [ ] RED: use real module with a fake external HTTP boundary to require only HEAD exact counts and GET projectedheartbeat. Expected queries: cases with any nonnullsync_worker_id, case_sync_runs statusrunning, pjud_import_jobs queued/discovering/importing counts separately plus anynonnullclaim_token, pjud_import_candidates importing/nonnullclaim_token, pjud_lookup_attempts searching. No expiry exclusion may turn unknown in-flight work into zero.

```python
def test_unavailable_count_is_not_idle(audit_fixture):
    audit_fixture.http.fail_count('pjud_import_jobs', status=503)
    result = audit_fixture.run()
    assert result['ready_for_shutdown_review'] is False
    assert result['work_counts']['import_jobs_active'] is None
    assert 'synthetic-secret' not in audit_fixture.output

def test_active_import_blocks_review_readiness(audit_fixture):
    audit_fixture.http.count('pjud_import_jobs', 1)
    assert audit_fixture.run()['ready_for_shutdown_review'] is False
```

- [ ] Read installed env internally using existing guard-style trusted-file policy; accept exact SUPABASE_URL/SUPABASE_SERVICE_KEY/WORKER_ID and safe boolean flags, never shell-source it. URLs HTTPS only; disable redirect following; no raw exceptions, headers, bodies, commandlines or env in JSON/logs. Service state via selected systemctlshow properties only, identities via proc metadata. Health200 for web/API/localAPI, timeouts bounded. Heartbeat GET selects status,last_heartbeat_at,metadata; emit only recognizedstatus/freshness and bounded numericmint/flags, never arbitrarymetadata. No program retries.
- [ ] GREEN: `python -m pytest -q ops/tests/test_bootstrap_audit.py`; cover wrongSHA/dirtytree, missing/unsafe envmetadata/duplicates, badHTTP/count/futurestaleheartbeat, activework, redirects/secretleaks; verify exact HTTPmethods/queries against actualschema. Linux root/nonroot boundary cases with source-only pinned container when necessary.
- [ ] Document root-only invocation, finite outputs and its non-authoritative nature. Report RED/GREEN/evidence and sourcefreeze; controller review then commit.
- [ ] Include read-only HEAD counts for selected import candidates and proxy budget reservations reserved/unresolved (separate finite keys, no age exclusion). Schema: web migrations00082 and00064. Unknown/nonzero values block advisory readiness; never invoke reconciliation or budget mutations.

### Task 2: Explicit stopped-only installation and first adoption

**Files:** Create `ops/bootstrap-worker-maintenance.py`, `ops/tests/test_worker_bootstrap.py`; extend runbook.

**Interfaces:** `install --expected-sha <40hex>` creates initialhold only; `adopt --expected-sha <40hex> --operation-id <uuid>` verifies firstnewidentity and initializes a normal protocoljournal without opening. Release remains explicit existing `worker-maintenance.py finish` after its checks. Both productionfixedpaths/root-only; pure helpers/testdependencies allowed, no permissiveCLItestmode.

- [ ] RED safety cases exercise real store/metadata with tempfixtures and external systemctl doubles: activeAPI/worker, leftovercgroup/proc, failedtermination, wrongSHA/tree, outsidewindow, existingcontrol/lock, unsafeunit/links/modes or staleproof all reject without control creation.

```python
def test_active_worker_never_gets_bootstrap_control(bootstrap_fixture):
    bootstrap_fixture.worker(active=True)
    assert bootstrap_fixture.install().returncode != 0
    assert not bootstrap_fixture.control.exists()

def test_install_stays_closed_until_explicit_adoption(bootstrap_fixture):
    bootstrap_fixture.clean_stopped_services()
    assert bootstrap_fixture.install().returncode == 0
    assert bootstrap_fixture.store.read_control().state == 'hold'
    assert bootstrap_fixture.lifecycle_calls == []
```

- [ ] Implement globalEX ownership and stoppedproof validations before anywrite; require both installed units UnitFileState=disabled as well as clean stopped metadata/empty cgroups. No process signals/git/packageinstall/start/stop in installer. SHAexact includesapp/worker/ops and safeworktree. Save originalunit and targethash/phase in root-only durablebootstraprecord; add only RuntimeDirectory/Mode to existingworkerunit, preservingxvfbdropin/otherconfig. Initialcontrol via realMaintenanceStore.initialize_hold; atomic/fsyncedwrites, failclosed onpartialstate.
- [ ] Adopt requiresmatchingbootstraprecord/targetSHA, exactliveMainPID/kernel/cgroup andquiescentACK/EX plusAPIhealth, then writesnormaloperatorjournal using sharedreviewedfunctions. No release or rollback automatic; recordphase/hash and exposeonlyUUID/finitephase/result. ExistingCLIvalidators must accept resultingunit/control/journal; no change to theirsecurityrequirements.
- [ ] GREEN property/failure/integration tests including durable failures and each sideeffectboundary. Runbook contains exact coordinatedshutdown procedure and explains that auditor alone cannot authorizelegacyshutdown; validate no remaining active work/claims after orderlyexit before invokinginstall, without claiming no interruption. Include persistent disable of both exact units before signals; owned temporary runtime Restart=no/WatchdogSec=0/SendSIGKILL=no/TimeoutStopSec=infinity, verified before signals. Remove only owned temporary overrides after successful stopped proof and before install; disabled state protects against boot activation until hold exists. Exclude other activators/deploys; restore original enabled state explicitly only after durable newhold. Controller stops if adequateindependentevidence cannot be established.
- [ ] Review/fix/verify/commit this track before nativeexecution.

### Task 3: Native initial-cutover proof and operational handoff

**Files:** Create `ops/tests/native/test_bootstrap.py` and `ops/tests/native/bootstrap_exercise.py`; narrowly extend `ops/tests/native/run_hvf.py` with explicit bootstrapexercise mode/payload if required; update runbook/evidence.

- [ ] RED hosttransport test rejects missingrequiredbootstrapmodule and defaultmode remains unchanged; actualbootstrapexercise starts withlegacyunit/noinitialcontrol, provesactivelegacyrejection, orderlystop/emptycgroups, realinitialhold, closednewworker andauthenticatedadoption/release. Injectpartialinstallfailure and prove noautomaticopen/lifecycle.
- [ ] Preserve readonlyISO/sourceallowlist/nohostmount/agent/secrets,2CPU4GiB,30percentadmission/12percentwatchdog,1800secsupervisor,restrictednetwork andownedcleanup. No app orcredential payload. VM is controller-only after complete harnessreview; failures require diagnosis before anothertrial.
- [ ] Verify focusedhost/Linux tests; controller runs onefreshnativebootstraptrial and fullaffectedservice suite. Review evidence/newdiff before localcommit/publication.
- [ ] Controller coordinates exacttargetSHA with othertask, repeatsVPSaudit, performs authorizedinitialcutover and subsequently existingguardpreflight/apply/postflight once, diagnosesrollback withoutblindretry. Report actualproduction result and notify coordinatingtask, then retain fullgoal for24h/naturalcycle/guestgates.
