# VPS Resource Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task by task. Use `superpowers:test-driven-development` for each behavior change and `superpowers:verification-before-completion` before any completion claim.

**Goal:** Protect JurisTrack and Hermes with versioned cgroup, swap, monitoring, alerting, rollout, and rollback controls before provisioning an isolated guest workload.

**Architecture:** Keep JurisTrack API and worker inside one protected `legaltech.slice`, cap Hermes at its dynamically resolved user slice, and run the observers independently in `system.slice`. Install a standard-library Python metrics/alerting package plus idempotent shell scripts for swap and the controlled production rollout. Production application is a separate, gated step after code review and integration.

**Tech Stack:** systemd/cgroup v2, Bash, Python 3 standard library, pytest, existing shell test harnesses, PostgREST count-only health query.

**Spec:** `docs/superpowers/specs/2026-08-19-vps-resource-isolation-design.md`

## Global Constraints

- Never put Telegram credentials, database service keys, cookies, proxy URLs, case payloads, or other secrets in Git, test output, shell tracing, logs, or chat.
- Treat the previously exposed Telegram bot token as compromised. A human must revoke it and install a replacement outside chat before the synthetic live alert.
- Do not create the guest environment in this change.
- Do not delete the five preserved Langfuse volumes or reactivate Langfuse.
- Do not generate PJUD traffic, force a sync, mint sessions, retry paid traffic, or change worker scheduling flags.
- Do not install LXD, Kubernetes, Prometheus, Grafana, or `systemd-oomd`.
- Monitoring services must stay in `system.slice`; they must never be children of `legaltech.slice`.
- Production rollout requires a clean reviewed commit on the deployed branch, zero active worker claims, current public health checks, backups, and automatic rollback on a failed postflight.
- Preserve current defaults: provisioning must not automatically enable or start the PJUD worker when its existing opt-in flag is absent.

## Task 1: Specify and Test the systemd Resource Contract

**Files:**

- Modify: `ops/systemd/legaltech.slice`
- Modify: `ops/systemd/estrado-pjud.service`
- Modify: `ops/systemd/estrado-pjud-worker.service`
- Create: `ops/systemd-templates/hermes-user.slice.conf`
- Create: `ops/tests/test-resource-units.sh`

### Step 1: Write the failing static contract test

Create a shell test that reads the repository files without invoking systemd. Use an assertion helper that checks each exact property once and rejects unsafe parentage:

```bash
assert_line ops/systemd/legaltech.slice '^CPUWeight=1000$'
assert_line ops/systemd/legaltech.slice '^MemoryLow=3G$'
assert_line ops/systemd/legaltech.slice '^MemoryHigh=6G$'
assert_line ops/systemd/legaltech.slice '^MemoryMax=8G$'

assert_line ops/systemd/estrado-pjud.service '^Slice=legaltech.slice$'
assert_line ops/systemd/estrado-pjud.service '^TasksMax=512$'

assert_line ops/systemd/estrado-pjud-worker.service '^PartOf=legaltech.slice$'
assert_line ops/systemd/estrado-pjud-worker.service '^Slice=legaltech.slice$'
assert_line ops/systemd/estrado-pjud-worker.service '^MemoryHigh=2G$'
assert_line ops/systemd/estrado-pjud-worker.service '^MemoryMax=3G$'
assert_line ops/systemd/estrado-pjud-worker.service '^CPUQuota=200%$'
assert_line ops/systemd/estrado-pjud-worker.service '^CPUWeight=800$'
assert_line ops/systemd/estrado-pjud-worker.service '^TasksMax=512$'

assert_line ops/systemd-templates/hermes-user.slice.conf '^MemoryHigh=2G$'
assert_line ops/systemd-templates/hermes-user.slice.conf '^MemoryMax=2500M$'
assert_line ops/systemd-templates/hermes-user.slice.conf '^TasksMax=1024$'
assert_line ops/systemd-templates/hermes-user.slice.conf '^CPUWeight=200$'
```

Also assert that neither monitor unit contains `Slice=legaltech.slice`.

### Step 2: Confirm the test fails for missing controls

Run:

```bash
bash ops/tests/test-resource-units.sh
```

Expected: non-zero with failures for `MemoryLow`, worker resource properties, and the Hermes template.

### Step 3: Implement exact unit properties

Set `legaltech.slice` to:

```ini
[Unit]
Description=JurisTrack aggregate resource budget

[Slice]
CPUWeight=1000
MemoryLow=3G
MemoryHigh=6G
MemoryMax=8G
```

Keep all existing API service hardening and limits; add only `TasksMax=512`. Add the approved worker properties without changing its command, environment, restart policy, or schedule. Create the Hermes drop-in template as:

```ini
[Slice]
MemoryHigh=2G
MemoryMax=2500M
TasksMax=1024
CPUWeight=200
```

The template path deliberately omits a numeric UID; provisioning resolves it later.

### Step 4: Verify and commit

Run:

```bash
bash ops/tests/test-resource-units.sh
systemd-analyze verify ops/systemd/legaltech.slice ops/systemd/estrado-pjud.service ops/systemd/estrado-pjud-worker.service
git diff --check
```

Expected: all assertions pass and `systemd-analyze` reports no errors. If the local macOS host lacks systemd, record that single validation as production-only; the static test remains mandatory.

Commit:

```bash
git add ops/systemd ops/systemd-templates ops/tests/test-resource-units.sh
git commit -m "feat(ops): define production resource budgets"
```

## Task 2: Build Pure Metrics Collection Primitives

**Files:**

- Create: `ops/monitoring/resource_metrics.py`
- Create: `ops/monitoring/tests/test_resource_metrics.py`
- Create: `ops/monitoring/tests/fixtures/meminfo.txt`
- Create: `ops/monitoring/tests/fixtures/systemctl-show.txt`

### Step 1: Write failing parsing and serialization tests

Cover these public contracts:

```python
def parse_meminfo(text: str) -> dict[str, int]: ...
def parse_systemctl_show(text: str) -> dict[str, str]: ...
def percent_used(total: int, available: int) -> float: ...
def atomic_write_json(path: Path, value: dict[str, object]) -> None: ...
def append_csv(path: Path, snapshot: "ResourceSnapshot") -> None: ...
```

Tests must prove:

- meminfo kB values become bytes;
- `MemAvailable` drives host availability, not `MemFree`;
- `max`, empty, `[not set]`, and numeric systemd values normalize safely;
- percentage helpers handle zero totals without division by zero;
- JSON replacement is atomic and leaves no temporary file;
- CSV emits a schema version and stable column order;
- exception text never includes environment variable values.

### Step 2: Confirm tests fail

Run:

```bash
.venv/bin/pytest -q ops/monitoring/tests/test_resource_metrics.py
```

Expected: import failure because the module does not exist.

### Step 3: Implement injectable collection

Use immutable dataclasses with only aggregate operational data:

```python
@dataclass(frozen=True)
class HostSnapshot:
    memory_total_bytes: int
    memory_available_bytes: int
    swap_total_bytes: int
    swap_used_bytes: int
    load_1m: float
    root_bytes_total: int
    root_bytes_used: int
    root_inodes_total: int
    root_inodes_used: int

@dataclass(frozen=True)
class UnitSnapshot:
    name: str
    active_state: str
    sub_state: str
    memory_current_bytes: int | None
    memory_peak_bytes: int | None
    memory_high_bytes: int | None
    memory_max_bytes: int | None
    tasks_current: int | None
    tasks_max: int | None
    cpu_usage_ns: int | None
    n_restarts: int | None

@dataclass(frozen=True)
class ResourceSnapshot:
    schema_version: int
    timestamp_utc: str
    host: HostSnapshot
    units: dict[str, UnitSnapshot]
```

Collection accepts injected `read_text`, `statvfs`, and `run_command` callables. Query systemd with one bounded command per unit and explicit properties:

```text
ActiveState SubState MemoryCurrent MemoryPeak MemoryHigh MemoryMax
TasksCurrent TasksMax CPUUsageNSec NRestarts ControlGroup
```

Unit errors become an inactive/unknown snapshot plus a sanitized diagnostic field; one missing optional unit must not prevent host metrics from being written.

### Step 4: Verify and commit

Run:

```bash
.venv/bin/pytest -q ops/monitoring/tests/test_resource_metrics.py
python3 -m py_compile ops/monitoring/resource_metrics.py
git diff --check
```

Commit:

```bash
git add ops/monitoring/resource_metrics.py ops/monitoring/tests
git commit -m "feat(ops): collect aggregate resource metrics"
```

## Task 3: Implement Tracker, Alert Policy, and Secret-Safe Telegram Transport

**Files:**

- Create: `ops/monitoring/resource-tracker.py`
- Create: `ops/monitoring/monitor.py`
- Create: `ops/monitoring/alert_policy.py`
- Create: `ops/monitoring/tests/test_tracker.py`
- Create: `ops/monitoring/tests/test_alert_policy.py`
- Create: `ops/monitoring/tests/test_monitor_cli.py`
- Create: `ops/monitoring/tests/test_telegram_transport.py`

### Step 1: Write the failing policy tests

Represent each evaluation explicitly:

```python
@dataclass(frozen=True)
class RuleResult:
    key: str
    severity: str
    active: bool
    persist_for_seconds: int
    cooldown_seconds: int
    message: str

@dataclass(frozen=True)
class AlertEvent:
    key: str
    severity: str
    message: str
    kind: str  # firing or resolved
```

Use a fake clock and snapshots to test every approved threshold:

- critical units inactive immediately;
- host RAM below 15% for 900 seconds warns;
- host RAM below 8% for 300 seconds is critical;
- swap above 25% for 900 seconds warns and above 50% is critical;
- root bytes or inodes at 80% warn and at 90% are critical;
- `legaltech.slice` above 80% of `MemoryHigh` for 900 seconds warns;
- an increase in `NRestarts` warns once per cooldown;
- recovery emits one resolved event;
- a daily healthy heartbeat occurs at most once per UTC day;
- state survives process restarts through atomic JSON;
- `--dry-run` returns candidate events but does not write state.

### Step 2: Write failing tracker and transport tests

Patch all network entry points and prove `resource-tracker.py` never constructs an HTTP request. Test Telegram with an injected opener:

```python
class TelegramTransport:
    def __init__(self, token: str, chat_id: str, opener: Callable, timeout: float = 5.0): ...
    def send(self, message: str) -> None: ...
```

Require a 2xx response; 4xx, 5xx, timeout, invalid JSON, and Telegram `{ "ok": false }` must fail. Captured logs and exceptions must not contain token, chat ID, full request URL, or payload secrets.

CLI tests cover:

```text
resource-tracker.py --once --csv /var/log/legaltech/resources.csv
monitor.py --once --state-dir /var/lib/legaltech-monitor
monitor.py --dry-run --state-dir /var/lib/legaltech-monitor
monitor.py --test-alert
```

`--test-alert` must require both credential variables and label the message `JurisTrack synthetic monitoring test`.

### Step 3: Confirm the new suites fail

Run:

```bash
.venv/bin/pytest -q \
  ops/monitoring/tests/test_tracker.py \
  ops/monitoring/tests/test_alert_policy.py \
  ops/monitoring/tests/test_monitor_cli.py \
  ops/monitoring/tests/test_telegram_transport.py
```

Expected: import failures for the new modules.

### Step 4: Implement policy and CLIs

Keep state keyed by rule with `active_since`, `last_sent_at`, `last_value`, and `last_severity`. Evaluate first, persist second, then deliver. For normal runs, persist candidate state even when transport fails so repeated service invocations respect the configured retry/cooldown policy; record delivery failure to journald. For `--dry-run`, use an in-memory copy and make no network or filesystem mutations.

Read credentials only at startup from environment:

```python
token = os.environ.get("LEGALTECH_TELEGRAM_BOT_TOKEN")
chat_id = os.environ.get("LEGALTECH_TELEGRAM_CHAT_ID")
```

Do not support tokens in command-line arguments. Send JSON with `urllib.request`, an explicit timeout, and no request URL in errors. Tracker writes only metrics; monitor owns all notification behavior.

### Step 5: Verify and commit

Run:

```bash
.venv/bin/pytest -q ops/monitoring/tests
python3 -m py_compile ops/monitoring/*.py
rg -n 'bot[0-9]|TELEGRAM.*=' ops/monitoring ops/systemd || true
git diff --check
```

The search may show environment variable names but no assigned credentials or bot-token-shaped value.

Commit:

```bash
git add ops/monitoring
git commit -m "feat(ops): add persistent resource alerts"
```

## Task 4: Provision Monitoring and the Dynamic Hermes User Slice

**Files:**

- Modify: `ops/systemd/legaltech-monitor.service`
- Modify: `ops/systemd/legaltech-resource-tracker.service`
- Create: `ops/systemd/legaltech-monitor.timer`
- Create: `ops/systemd/legaltech-resource-tracker.timer`
- Create: `ops/logrotate/legaltech-resources`
- Modify: `ops/provision.sh`
- Modify: `ops/tests/test-provision.sh`

### Step 1: Extend failing provision tests

Add command stubs and filesystem assertions for:

- recursive installation of monitoring Python files into `/opt/legaltech-monitoring` owned by root and not writable by group/other;
- creation of `/var/lib/legaltech-monitor` and `/var/log/legaltech` with explicit permissions;
- installation of the logrotate rule with daily rotation, 14 files, compression, and `copytruncate` or safe reopen behavior;
- both monitor services set `Slice=system.slice`, `MemoryMax=128M`, `CPUQuota=20%`, and `TasksMax=64`;
- both timer units use `OnBootSec=5min`, `OnUnitActiveSec=5min`, `Persistent=true`, and `RandomizedDelaySec=60s`;
- both services use `EnvironmentFile=-/etc/legaltech-monitoring.env` and never inline credentials;
- dynamic `id -u hermes`, verification that the returned UID maps back to username `hermes`, and installation at `/etc/systemd/system/user-<uid>.slice.d/50-legaltech-resource-limits.conf`;
- refusal when `hermes` is absent, UID is nonnumeric, reverse lookup differs, or the user owns unexpected persistent services not on an explicit allowlist;
- idempotent repeated provisioning;
- existing worker opt-in semantics remain unchanged.

### Step 2: Confirm the provision test fails

Run:

```bash
bash ops/tests/test-provision.sh
```

Expected: failures for the unimplemented copies, permissions, and Hermes drop-in.

### Step 3: Implement the service units

Both units must include:

```ini
[Service]
Type=oneshot
User=root
EnvironmentFile=-/etc/legaltech-monitoring.env
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/legaltech-monitor /var/log/legaltech
MemoryMax=128M
CPUQuota=20%
TasksMax=64
Slice=system.slice
```

The tracker writes CSV. The monitor writes alert state and talks to Telegram. Add dedicated timer units at five-minute cadence with `Persistent=true` and `RandomizedDelaySec=60s`; neither timer belongs to `legaltech.slice`.

### Step 4: Implement provisioning

Resolve the Hermes UID at runtime, validate it before substitution, and render the committed template into its numeric drop-in. Inspect current processes/units for the UID and accept only the known Hermes gateway/dashboard/user-manager set; fail closed on unknown persistent services.

Install an empty credential file only when absent:

```bash
install -o root -g root -m 0600 /dev/null /etc/legaltech-monitoring.env
```

Never overwrite a populated credential file. Run `systemctl daemon-reload`; enable timers, not the one-shot services directly.

### Step 5: Verify and commit

Run:

```bash
bash -n ops/provision.sh
bash ops/tests/test-provision.sh
bash ops/tests/test-resource-units.sh
.venv/bin/pytest -q ops/monitoring/tests
git diff --check
```

Commit:

```bash
git add ops/provision.sh ops/systemd ops/logrotate ops/tests/test-provision.sh
git commit -m "feat(ops): provision monitoring and Hermes limits"
```

## Task 5: Add Idempotent, Guarded Swap Management

**Files:**

- Create: `ops/swap/configure-swap.sh`
- Create: `ops/swap/tests/test-configure-swap.sh`

### Step 1: Write the failing shell tests

Inject every host command through environment variables or `PATH` stubs: `df`, `fallocate`, `chmod`, `mkswap`, `swapon`, `swapoff`, `sysctl`, `free`, `cp`, and `mv`. Test:

- apply refuses when `/` has less than 8 GiB free;
- apply creates exactly 4 GiB at `/swapfile`, mode `0600`;
- apply adds one marker-delimited `/etc/fstab` entry and one managed sysctl file;
- repeated apply changes nothing and does not run `mkswap` twice;
- verify checks `/proc/swaps`, size, mode, `swappiness=10`, and the fstab marker;
- rollback removes only the managed marker block and sysctl file;
- rollback refuses `swapoff` when available RAM is not greater than current swap use plus a 1 GiB safety margin;
- rollback never edits unrelated fstab lines.

### Step 2: Confirm tests fail

Run:

```bash
bash ops/swap/tests/test-configure-swap.sh
```

Expected: non-zero because the script does not exist.

### Step 3: Implement explicit subcommands

Expose only:

```text
configure-swap.sh preflight
configure-swap.sh apply
configure-swap.sh verify
configure-swap.sh rollback
```

Use strict mode, explicit absolute default paths overridable only for tests, and a marker block:

```text
# BEGIN LEGALTECH MANAGED SWAP
/swapfile none swap sw 0 0
# END LEGALTECH MANAGED SWAP
```

Use `fallocate` with a `dd` fallback, validate the final byte size, set mode before `mkswap`, and write `/etc/sysctl.d/60-legaltech-swap.conf` with `vm.swappiness=10`. Back up fstab before replacement and use a same-directory atomic rename.

### Step 4: Verify and commit

Run:

```bash
bash -n ops/swap/configure-swap.sh
bash ops/swap/tests/test-configure-swap.sh
git diff --check
```

Commit:

```bash
git add ops/swap
git commit -m "feat(ops): add guarded emergency swap"
```

## Task 6: Add a Fail-Closed Rollout and Rollback Orchestrator

**Files:**

- Create: `ops/resource-guards.sh`
- Create: `ops/tests/test-resource-guards.sh`

### Step 1: Write failing orchestration tests

Stub SSH-local dependencies and verify exact ordering for `preflight`, `apply`, `postflight`, and `rollback`. Required cases:

- dirty Git tree or deployed SHA mismatch refuses apply;
- non-200 JurisTrack or Estrado health refuses apply;
- fewer than 8 GiB free or less than 6 GiB available RAM refuses apply;
- unresolved Hermes UID refuses apply;
- an unavailable or ambiguous active-claim count refuses worker restart;
- any active claim count above zero refuses worker restart;
- backups occur before the first mutation;
- `daemon-reload` precedes service restarts;
- API and Hermes are restarted only if their unit/drop-in changed;
- worker restart occurs only after a confirmed zero claim count;
- failed postflight invokes rollback once;
- rollback restores only files listed in the generated manifest;
- swap rollback observes its RAM safety gate;
- no command calls a PJUD sync endpoint, proxy, session mint, or retry endpoint.

### Step 2: Specify the count-only active-claim adapter

The script receives the PostgREST base URL and service credential through environment variables already installed on the host. It computes a UTC lease cutoff and issues a `HEAD` request selecting only a count:

```text
/rest/v1/cases?select=id&sync_worker_id=not.is.null&sync_claimed_at=gte.<cutoff>
Prefer: count=exact
```

Parse only `Content-Range: 0-0/<count>` or `*/0`; never print response headers, URL query credentials, or row data. HTTP failure, absent header, wildcard total other than the exact zero form, or a nonnumeric count is an unsafe unknown and must stop the rollout.

### Step 3: Confirm tests fail

Run:

```bash
bash ops/tests/test-resource-guards.sh
```

Expected: non-zero because the orchestrator does not exist.

### Step 4: Implement manifest-based backups and rollback

Use a root-owned timestamped directory under `/var/backups/legaltech-resource-guards`. The manifest records, for every managed path, whether it existed, its backup location, mode, owner, and group. Include:

```text
/etc/systemd/system/legaltech.slice
/etc/systemd/system/estrado-pjud.service
/etc/systemd/system/estrado-pjud-worker.service
/etc/systemd/system/legaltech-monitor.service
/etc/systemd/system/legaltech-resource-tracker.service
/etc/systemd/system/user-<uid>.slice.d/50-legaltech-resource-limits.conf
/etc/legaltech-monitoring.env
/etc/fstab
/etc/sysctl.d/60-legaltech-swap.conf
/opt/legaltech-monitoring
```

Do not copy credential contents into logs. The backup directory is `0700`; files preserve restrictive modes.

Apply sequence:

1. Run all preflights and record the expected Git SHA.
2. Create and validate the backup manifest.
3. Run repository `ops/provision.sh`.
4. Run swap `apply` then `verify`.
5. Run `systemctl daemon-reload`.
6. Restart only changed API/Hermes units.
7. Query the claim count; restart worker only at exact zero.
8. Start both timers and invoke one tracker and one monitor dry run.
9. Run postflight; on any error, invoke rollback.

Postflight uses `systemctl show` to assert the exact live cgroup properties, confirms monitor services belong to `system.slice`, checks swap, then calls the two public HTTPS health endpoints with bounded timeouts.

### Step 5: Verify and commit

Run:

```bash
bash -n ops/resource-guards.sh
bash ops/tests/test-resource-guards.sh
bash ops/tests/test-provision.sh
bash ops/tests/test-deploy.sh
git diff --check
```

Commit:

```bash
git add ops/resource-guards.sh ops/tests/test-resource-guards.sh
git commit -m "feat(ops): orchestrate resource guard rollout"
```

## Task 7: Document Operations and Run Full Repository Verification

**Files:**

- Modify: `ops/README.md`
- Create: `ops/monitoring/README.md`
- Create: `ops/swap/README.md`

### Step 1: Write the operator runbook

Document exact commands for local tests and each safe subcommand, expected success output, backup directory, and rollback. Include these explicit blockers:

- do not apply from an unreviewed/local-only commit;
- do not restart the worker unless claim count is exactly zero;
- do not paste the replacement Telegram token into a terminal command that persists in shell history; install it through a root-only editor or protected secret delivery channel;
- do not run `swapoff` manually under pressure;
- do not claim total-outage coverage until an external uptime monitor exists;
- do not provision the guest until the 24-hour gate passes.

Document aggregate metrics only. State that the monitor never requires case content, cookies, proxy telemetry, or user data.

### Step 2: Run focused and full validation

Run:

```bash
bash ops/tests/test-resource-units.sh
bash ops/tests/test-provision.sh
bash ops/tests/test-deploy.sh
bash ops/swap/tests/test-configure-swap.sh
bash ops/tests/test-resource-guards.sh
.venv/bin/pytest -q ops/monitoring/tests
.venv/bin/pytest -q
bash -n ops/provision.sh ops/resource-guards.sh ops/swap/configure-swap.sh
python3 -m py_compile ops/monitoring/*.py
git diff --check
```

Expected baseline comparison: the existing product suite remains at or better than 1217 passed, 1 skipped, with no new failure. Classify any deviation against the baseline before continuing.

### Step 3: Run secret and unsafe-pattern checks

Run:

```bash
rg -n --hidden --glob '!*.md' '(bot[0-9]{6,}:|TELEGRAM_(BOT_)?TOKEN=.+|service_role.+[A-Za-z0-9_-]{20})' ops
rg -n 'Slice=legaltech\.slice' ops/systemd/legaltech-monitor.service ops/systemd/legaltech-resource-tracker.service
rg -n '(PJUD.*(sync|retry|mint)|proxy.*request)' ops/resource-guards.sh
```

Expected: all three commands produce no matches. Environment variable names without values are acceptable only outside the credential-shaped regex.

### Step 4: Review the complete diff

Inspect every changed path and compare it line by line with the spec. In particular, confirm the monitor is outside the observed slice, the worker retains its opt-in behavior, rollback is namespace-limited, and no production credential was introduced.

Commit:

```bash
git add ops/README.md ops/monitoring/README.md ops/swap/README.md
git commit -m "docs(ops): add resource guard runbook"
```

## Task 8: Review, Integrate, and Perform the Controlled Production Rollout

This task is operational and must not begin merely because Tasks 1–7 pass locally.

### Step 1: Independent review and exact-head verification

Use `superpowers:requesting-code-review`. Resolve substantive findings, rerun affected tests, and refresh the exact branch HEAD, diff, checks, mergeability, and unresolved review threads before any integration action. GitHub push, PR creation, approval, or merge requires the user's explicit authorization for that external action.

### Step 2: Integrate through the normal reviewed branch flow

After authorization, push `feature/vps-resource-isolation`, create the PR, wait for required checks, and merge only if separately authorized and exact-head review remains green. Production must deploy the resulting reviewed main-branch SHA, not an unpushed worktree commit.

### Step 3: Rotate the compromised credential outside chat

The user revokes the old Telegram bot token and installs a replacement into `/etc/legaltech-monitoring.env` with `0600 root:root`. Verify only variable presence and file mode; never print values.

### Step 4: Run read-only production preflight

On the VPS:

```bash
sudo ./ops/resource-guards.sh preflight --expected-sha "$(git rev-parse HEAD)"
```

Capture only aggregate evidence: current SHA, unit states, free RAM/disk, swap state, Hermes UID validation, exact-zero active claim count, and public health codes. Abort on any unsafe unknown.

### Step 5: Apply and verify with automatic rollback armed

Run:

```bash
sudo ./ops/resource-guards.sh apply --expected-sha "$(git rev-parse HEAD)"
sudo ./ops/resource-guards.sh postflight
sudo /opt/legaltech-monitoring/monitor.py --dry-run
sudo /opt/legaltech-monitoring/monitor.py --test-alert
```

The live alert is allowed only after token rotation. Confirm receipt without disclosing identifiers. Verify `systemctl show` exact properties, worker cgroup membership, Hermes services, swap size/mode/swappiness, timers, journald errors, and HTTP 200 for JurisTrack/Estrado.

### Step 6: Complete remaining functional checks

Send one normal message through Hermes and confirm its expected response path. This is a Hermes channel validation only; it must not invoke PJUD or Langfuse. Configure an independent external uptime monitor for both public endpoints, or record the missing total-outage coverage as an explicitly accepted risk.

### Step 7: Observe for 24 hours before guest work

At the start, middle, and end of the window collect aggregate snapshots. The gate passes only when:

- no OOM or unexpected restart occurred;
- swap was empty or only incidental, never sustained above policy thresholds;
- JurisTrack, Estrado, worker, Hermes, timers, and endpoints stayed healthy;
- available RAM did not remain below 6 GiB;
- disk and inodes stayed below 80%;
- synthetic Telegram alert was received;
- external monitor status and Hermes E2E result are either verified or explicitly accepted as residual risks.

Do not provision the guest as part of this plan. A separate change can then implement the approved profile: 2 vCPU, `MemoryHigh=2500M`, `MemoryMax=3G`, 20 GiB fixed filesystem, isolated network, no host/Docker access, and a kill switch independent of JurisTrack.
