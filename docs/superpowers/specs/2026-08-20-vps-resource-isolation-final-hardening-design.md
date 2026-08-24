# VPS Resource Isolation Final Hardening Design

Date: 2026-08-20
Branch: `feature/vps-resource-isolation`
Base implementation: `095b4d4f18236323e2d38ad954f8ac3c46d6b296`

## Purpose

Close the three load-bearing findings from the final whole-branch audit before integration or production rollout:

1. fence an active PJUD worker so changing its systemd unit cannot race a new claim or create paid startup traffic;
2. prove required workloads are active in their expected runtime cgroups before an apply can succeed or monitoring can report health;
3. make swap rollback safely retryable after `swapoff` succeeds and a later cleanup step fails.

The user accepts one overnight maintenance window. JurisTrack's API should remain available unless its changed unit requires the already-designed bounded restart. The PJUD worker may remain idle for the night and must resume only through its natural configured business-hours behavior.

## Non-goals and boundaries

- Do not add a new database migration or persistent maintenance-control subsystem.
- Do not mutate `pjud_proxy_control` as part of resource rollout.
- Do not force a PJUD sync, retry, session mint, proxy request, or paid validation.
- Do not broaden the transaction to Caddy or unrelated services.
- Do not create the friend's account/container until integration, Linux gates, production rollout, and the observation window are green.
- Local implementation and verification do not authorize push, PR, merge, VPS mutation, credential rotation, or live alert delivery.

## Decision 1: outside-hours stop-drain-start fence

### Why this approach

The worker already handles `SIGTERM` by setting `shutdown_event`. It does not dispatch not-yet-started cases after that signal, allows already-running cases to finish, releases the claimed batch, publishes a final stopped heartbeat, closes the session pool, and only then exits. `systemctl stop` therefore provides the acquisition fence that a read-only SELECT cannot provide: once the command returns successfully and the unit is inactive, that process cannot claim or mint again.

Starting the replacement outside the PJUD processing window with `PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false` prevents startup pool initialization and paid minting. This avoids introducing a database maintenance flag and avoids changing persistent proxy control.

### Preconditions

For a changed worker that was captured active, apply must fail closed unless all of these are proven immediately before stopping it:

- current Santiago time is inside the bounded maintenance window 20:00-04:00; this leaves at least four hours before the next possible 08:00 business opening for rollback and diagnosis;
- the worker's protected environment contains exactly one valid `WORKER_ID` and exactly one `PJUD_PROCESS_OUTSIDE_OFFICE_HOURS=false`;
- the exact `WORKER_ID` has one fresh allowlisted heartbeat with status `idle_off_hours`;
- heartbeat metadata reports `process_outside_office_hours_enabled=false`;
- when proxy mode is configured, heartbeat metadata reports `proxy_control_status=enabled` and no telemetry-unavailable reason;
- the exact active-claim count is zero;
- the worker unit is active before the controlled stop.

The heartbeat query must filter by the exact configured worker ID. A heartbeat belonging to another worker, a future/stale row, an unknown status, malformed metadata, a failed producer, or multiple/ambiguous records is unsafe unknown.

### Transaction sequence

For a changed, previously active worker:

1. complete the existing backup and recheck the immutable SHA;
2. prove the outside-hours preconditions above;
3. record the worker-stop mutation durably in the transaction metadata;
4. run `systemctl stop estrado-pjud-worker.service` and require success;
5. prove the unit is inactive and its old PID/cgroup is gone;
6. poll a bounded, count-only query until the exact active-claim count is zero; failure or timeout triggers rollback;
7. provision units, apply swap changes, reload systemd, and restart only workloads captured active;
8. start the worker explicitly instead of using `restart`;
9. require the worker active in `/legaltech.slice/estrado-pjud-worker.service`;
10. require a new fresh heartbeat for the exact worker with `idle_off_hours`, override false, proxy control enabled when applicable, and `mint_attempts=0`;
11. require the active-claim count still zero;
12. only then allow postflight to succeed.

Stopping a worker that was captured inactive remains forbidden. An unchanged worker is not stopped or started.

### Failure and rollback

Rollback restores the exact backed-up unit definitions and enablement first, reloads systemd, and restores captured activity:

- a worker captured active is started under the old definition only after the restored outside-hours configuration is validated;
- a worker captured inactive remains inactive and executes no protected database query;
- because rollback remains outside hours with the override false, the restored worker stays active-but-idle and performs no paid initialization;
- failure to restore exact state is loud and never reports rollback success.

If graceful stop completes but claims remain, apply rolls back and restores the old active-but-idle worker. It does not clear claims, force retry, or wait through the four-hour lease inside the transaction. The operator may diagnose or retry a later maintenance window after natural lease recovery.

## Decision 2: exact runtime cgroup postflight

### Required runtime identities

Postflight must collect and strictly validate `LoadState`, `ActiveState`, `MainPID` where applicable, and `ControlGroup` after every affected restart/start:

- `legaltech.slice`: active and exactly `/legaltech.slice`;
- `estrado-pjud.service` when captured active: active, one strictly positive `MainPID`, and exactly `/legaltech.slice/estrado-pjud.service`;
- `estrado-pjud-worker.service` when captured active: active, one strictly positive `MainPID`, and exactly `/legaltech.slice/estrado-pjud-worker.service`;
- dynamic Hermes slice: active and exactly `/user.slice/user-<uid>.slice`;
- each Hermes service captured active: active with a non-empty cgroup below `/user.slice/user-<uid>.slice/` and ending in its exact unit name;
- workloads captured inactive: still inactive, with no requirement to own a live cgroup.

Any command failure, duplicate property, malformed path, wrong slice, inactive required unit, or residual old PID/cgroup triggers the existing rollback.

### Monitoring policy

Monitoring must compare required continuous-unit cgroups with their expected identities, not merely accept any absolute path. The dynamic Hermes expected path is derived from the already-resolved slice name. Timers continue to use load/unit-file/active state and inactive one-shots continue to use `Result`; neither class is required to own a process cgroup.

A wrong cgroup produces the existing stable availability alert for that unit and suppresses the healthy heartbeat. Alert text remains sanitized and does not expose observed paths or command diagnostics.

The resource orchestrator's exact postflight is the apply success gate. `monitor.py --dry-run` remains a non-mutating diagnostic and is not treated as a success signal merely because its process exits zero.

## Decision 3: retryable phased swap rollback

### Accepted states

The swap tool must recognize only validated states produced by its own rollback order:

1. `managed-active`: all managed artifacts valid and the exact swap target active;
2. `managed-deactivated`: live swappiness restored, exact target inactive, managed fstab block and validated backup still present, remaining exact artifacts valid;
3. `fstab-restored`: exact target inactive, managed block absent, backup consumed, and any remaining sysctl/swapfile/metadata artifacts individually valid;
4. `clean`: no managed artifacts and no exact active target.

Every other combination remains unsafe unknown. In particular, symlinks, hardlinks, unexpected ownership/mode/content, duplicate markers, an unexpected active swap, or a managed block without its validated backup are never repaired heuristically.

### Retry algorithm

- `managed-active` performs the existing RAM gate, restores and verifies live swappiness, then calls `swapoff` and verifies the exact target inactive.
- `managed-deactivated` skips the RAM gate and `swapoff`, re-verifies the stored original swappiness and continues with fstab restoration.
- `fstab-restored` never rewrites fstab and continues deleting only validated remaining exact artifacts.
- cleanup order remains deterministic: restore fstab, remove managed sysctl, remove inactive exact swapfile, remove swappiness metadata last.
- after every step, a failure returns non-zero while leaving one of the accepted retry states.
- repeated rollback from every injected failure point converges to `clean`; repeated rollback from `clean` remains a no-op success.

No retry path may reactivate swap, synthesize a backup, overwrite unrelated fstab bytes, or delete metadata before it is no longer needed to validate remaining ownership.

## Test strategy

Implementation uses strict TDD, one track at a time.

### Track A: worker maintenance fence

Behavioral shell tests must prove RED then GREEN for:

- heartbeat query scoped to exact `WORKER_ID`;
- wrong worker, stale/future heartbeat, wrong status, override true, proxy paused/unavailable, malformed metadata, producer failure, and business-hours execution all fail before stop;
- stop precedes the post-stop zero-claim proof and any provisioning;
- a claim that appears before stop is drained/released before provisioning;
- persistent claims after stop trigger rollback with no paid action;
- replacement starts only after unit installation and remains `idle_off_hours` with zero mint attempts and zero claims;
- inactive/unchanged worker paths perform no stop/start or protected query;
- every failure boundary performs one exact rollback and restores captured activity/enablement.

Worker Python tests must continue proving graceful drain: SIGTERM prevents undispatched work, running work completes, claims release before exit, and startup outside hours does not initialize the paid pool.

### Track B: runtime cgroups

Behavioral tests must independently place API, worker, LegalTech slice, Hermes slice, and active Hermes units in a wrong but syntactically valid cgroup. Each mismatch must fail apply or raise the stable availability alert and suppress the healthy heartbeat. Correct dynamic Hermes paths, inactive worker/Hermes units, timers, and inactive successful one-shots must remain healthy.

### Track C: swap retry

Inject failure after successful `swapoff`, during fstab restoration, and during each exact artifact deletion. The first rollback must fail loudly in an accepted partial state; a second rollback must converge to byte-identical original fstab, exact original live swappiness, no active target, and no managed artifacts. Corrupt or ambiguous partial states must remain untouched.

## Review and verification gates

Each track follows Build -> independent Review -> Fix -> Re-review -> Verify -> Commit. Critical/High findings block the track; Medium findings are fixed unless explicitly ruled out with evidence.

After all tracks:

- run the complete resource-guards, provision, swap, resource-unit, deploy, monitoring, digest, and product suites;
- run Bash syntax, ShellCheck, Python compile, secret/IP/forbidden-traffic scans, and `git diff --check`;
- perform a new whole-branch exact-head review from `origin/main`;
- preserve the branch/worktree if any Critical or Important finding remains.

## Integration and production sequence

After a clean exact-head review, integration remains a separate user choice. Production rollout then occurs in an announced 20:00-04:00 Santiago maintenance window:

1. refresh production state, exact deployed SHA, credentials, proxy control, worker flag, heartbeat, claims, memory, disk, swap, units, and backups;
2. stop if any precondition is unknown or if proxy telemetry is unavailable;
3. apply the reviewed exact SHA through `resource-guards.sh` only;
4. validate API/Hermes, exact cgroups, timers, swap, alerts, and the idle worker without generating PJUD traffic;
5. keep the worker naturally idle overnight;
6. observe the next normal business-hours cycle using aggregate-only evidence, without manual retries;
7. require the planned observation window green before creating the friend's constrained environment.

The compromised Telegram credential must be rotated outside chat before any live synthetic alert. No secret enters Git, argv, reports, or conversation output.
