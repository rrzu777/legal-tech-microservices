# Native systemd validation (local laboratory)

## macOS accelerated runner

`run_hvf.py` uses an explicitly installed Homebrew QEMU and macOS HVF instead
of nested emulation. Invoke with a locally downloaded image matching the pinned
checksum below:

```sh
python3 ops/tests/native/run_hvf.py --base-image /absolute/path/base.qcow2
```

It requires ARM64 macOS, Python 3.11+, 40 GiB free disk and at least 30% reported
memory headroom before starting. Each VM has 2 CPUs, 4 GiB RAM and a 30-minute
independent lifetime limit. A watchdog stops only its own VM below 12% host memory
headroom. SIGTERM/SIGHUP trigger cleanup; a hard kill may retain temporary files,
but the VM lifetime limit remains. Evidence directories are retained, while normal
cleanup destroys the generated guest disk, seed and ephemeral SSH key.
The lifetime limit uses an external supervisor: a native smoke test showed that
QEMU did not honor an inherited SIGALRM deadline. Supervisor timeout and SIGTERM
cleanup are tested against real child processes, including a child ignoring alarms.

Only reviewed `ops/` source plus the exact stdlib worker allowlist enter a TAR
inside a readonly ISO: `worker/__init__.py`, `maintenance.py`,
`maintenance_store.py`, `sd_notify.py`, and `maintenance_heartbeat.py`. The fixture, its isolated
`fixture_worker.py` helper, exercise and probe are explicitly included even before
commit. No blanket service copy, `.env`, application client or real venv enters
the payload. Host validation and guest extraction enforce the same allowlist.
Linux ISO
name normalization previously removed a dot from `hermes-user.slice.conf`; TAR
preserves names and modes. Archive and manifest hashes are pinned before extraction,
every file is verified, and the guest-local extracted payload is bind-mounted
readonly before execution. No host directory, Docker
socket, agent or real credential is shared. Bootstrap installs Ubuntu packages,
then powers off; the test phase reboots with QEMU `restrict=on` networking. An owned
host TCP listener must be reachable before isolation and unreachable afterwards.

**Capacity exclusion:** the small HVF guest substitutes only the explicit
`RG_FREE_BIN` test boundary with a 7 GiB available-RAM admission fixture. This does
not modify production checks. Unit configuration, systemd, cgroups, swap and both
rollback paths use real guest tools. Passing this runner is NOT proof of host
capacity, the 6 GiB admission gate, memory pressure protection or stress isolation.

## Opt-in initial shutdown characterization (not a bootstrap proof)

Controller-only, after source review. By default the **real** America/Santiago
20:00–03:59 installer window still applies. Explicit user authorization now
permits daytime **disposable-lab characterization only**, via this opt-in:

```sh
python3 ops/tests/native/run_hvf.py --base-image /absolute/path/base.qcow2 \
  --mode bootstrap-characterization --allow-daytime-lab
```

The default remains `integral`: its fixture, stages, package list and free(1)
admission substitution are unchanged. The opt-in mode runs only
`bootstrap_exercise.py`, **never `fixture.py`**, and does not manually create
control, ACK, journal, or bootstrap authority. It does not apply resource guards.
Its completion marker explicitly says **BOOTSTRAP TASK INCOMPLETE**.

`--allow-daytime-lab` is rejected for the default/integral mode and transported
unchanged only to `bootstrap_exercise.py`. Each opted-in local lifecycle gate
rechecks Linux/root/QEMU and the `native-guards` guest hostname. Without the flag,
the local gate calls the real installer window with the original Config. No
clock, Config.clock, production function or environment override is used; the
actual Santiago time is logged. All watchdog/identity/signal/rejection and VM
isolation/supervision gates remain enforced. This does not authorize daytime
production operations or add an installer option.

The new mode adds `python3-venv` to the existing connected guest-only cloud-init
phase and creates `/opt/native-runtime`. Public wheels are version/hash pinned:

| Wheel | SHA-256 |
| --- | --- |
| uvicorn 0.41.0 | `29e35b1d2c36a04b9e180d4007ede3bcb32a85fbdfd6c6aeb3f26839de088187` |
| click 8.1.8 | `63c132bbbed01578a06712a2d1f497bb62d9c1c0d329b7903a866228027263b2` |
| h11 0.16.0 | `63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86` |

pip uses `--isolated --require-hashes --only-binary=:all: --no-deps
--disable-pip-version-check`, no cache/retries, a bounded timeout and the explicit
public PyPI index. Readiness additionally checks all three installed versions;
Python 3.12 is required in the guest. No host/VPS venv, application, `.env` or
credentials are copied. The generated guest app uses a guest-local `.venv`
symlink to that public runtime; it is not a trusted Git path. Neither source
allowlist boundaries nor readonly transport change. Package downloads finish
before the existing restricted-network reboot; no test executes while connected.

The isolated fixture generates only an ASGI health/lifespan body and a legacy
idle Python worker. Installed units retain the canonical API
`xvfb-run -> .venv/bin/uvicorn app.main:app` command and worker xvfb/notify
handoff, with **only** `EnvironmentFile` removed; the legacy worker also lacks
the two maintenance RuntimeDirectory lines. No `.env` is created. The five real
stdlib worker payload modules remain available; the synthetic legacy body uses
real `sd_notify` without creating maintenance control or ACK. All business,
browser, database, provider and remote operations are absent.

The planned native observations are:

1. Refuse a reused/non-QEMU/non-root/non-Linux guest. Without the explicit
   lab-only daytime opt-in, also refuse an out-of-window real clock. No clock
   override or production gate is injected.
2. Start both generated legacy services persistently disabled, and establish
   that the unchanged installer rejects their actual active snapshot.
3. Add only owned runtime shutdown overrides (`Restart=no`, `WatchdogSec=0`,
   `SendSIGKILL=no`, `TimeoutStopSec=infinity`). In the worker only, run the
   contained administrative watchdog characterization described below, and
   still require effective zero. Then authenticate real
   boot/PID/start-ticks/UID/parent/cgroup and wrapper identity. Bind a pidfd and
   send exactly one SIGTERM to the real Uvicorn child, not the Xvfb wrapper.
   Before the unchanged override assertion, a `NATIVE OVERRIDE CHECK` JSON
   record identifies phase/unit, the six selected guard properties and differing
   keys. No environment or command-line fields are logged. A mismatch still
   aborts before health checks or signal delivery. The watchdog cycle also
   authenticates identity before any administrative reload, and retains that
   original identity through both later workload-signal gates.
4. Record real final API/worker unit properties, shell result, empty cgroups,
   and synthetic lifespan journal for normal cleanup and for an intentional
   lifespan RuntimeError. No `reset-failed`, status rewriting, SIGKILL escalation,
   blanket cgroup stop, or implicit restart/release is allowed. The second API
   start is an explicit, planned independent failure case, not a blind retry.
5. Remove only the exact owned override files, revalidate real Git/metadata,
   window, absent authority, global EX and unit/cgroup prerequisites, directly
   exercise the unchanged stopped-snapshot gate, and invoke the real installer
   CLI. Verify rejection leaves all authority absent and unit bytes unchanged.

The direct snapshot exception is accepted only when its real traceback comes
from the exact reviewed unit-state predicate: frozen installer SHA-256
`b567f93089facbca3ccf0b9442e03e79220f31d3f035d2ee0fdc06b39b90ddfd`, loaded function
code matching that source, and the predicate's direct call to `require` at
line 216. Source drift, replaced code, boot/metadata/drop-in/cgroup failures, or
any other traceback origin abort before CLI execution. Merely sharing
`MaintenanceError` or the finite CLI `validation/blocked` response is insufficient.
This observation changes no installer helper or production acceptance predicate;
the fixture binding must be reviewed again if that source changes.
The unchanged real CLI retains its production window and still reports blocked
outside it. Its finite response does not expose the rejection gate: a separate
`NATIVE PRODUCTION CLI BLOCKED` record logs actual time/window observation and
`cli_rejection_gate=not_exposed`. Only the direct, authenticated stopped_snapshot
traceback proves the unit-state rejection; daytime CLI blocked is not that proof.

### Approved lab-only active-watchdog characterization

The existing shutdown override bytes/removal are unchanged. A separate owned
worker runtime drop-in, `91-native-watchdog-admin.conf`, temporarily adds only
`TimeoutStartSec=infinity` and direct
`ExecReload=/usr/bin/systemd-notify WATCHDOG_USEC=<interval>`.
There is no shell, `--pid`, externally impersonated PID, READY/MAINPID/STATUS/
STOPPING payload or business ACK. The normal notification barrier remains enabled.
Type must be exactly `notify`, NotifyAccess `all`; no preexisting ExecReload,
propagation in either direction, activator, foreign job/control process, unknown
drop-in or changed unit bytes is accepted. Exact unit/drop-in metadata and safe
ancestors are authenticated with the unchanged installer's read-only helpers.
No foreign file is overwritten, and exact helper bytes/ownership/mode/nlink/
non-symlink metadata are rechecked before each replacement/removal.

Effective argv **and execution flags** are checked using typed busctl JSON
`ExecReloadEx` (`a(sasasttttuii)`, ten fields per row), not the lossy space-joined
systemctl display. Only one exact command with empty flags is accepted. The
controller derived this shape from official systemd v255 source; its native
rendering is still unverified by this variant, and unknown output fails closed.
`WatchdogUSec` is also read as an exact typed uint64, not inferred from `5min`.

Before any workload signal, the same live synthetic worker must demonstrate
three explicit phases: **zero → recorded 300000000 microseconds → zero**.
Any other initial interval is rejected. The restore phase rebases the watchdog;
it does not restore the previous remaining deadline. Each targeted reload uses
`systemctl --job-mode=fail reload estrado-pjud-worker.service`, so a concurrent
conflicting job is not replaced. Restart=no, SendSIGKILL=no, infinite stop/start
timeouts, original boot/MainPID/start-ticks/cgroup/InvocationID, running/success,
ControlPID0, no job and the expected interval must hold around every transition.
Client return0 or notification success alone is insufficient.

The synthetic idle worker sends ordinary `WATCHDOG=1` Unix datagrams every
250ms without RPCs. A private guest-only file retains just the latest two
sequence numbers, PID, pre-send monotonic timestamps and successful send byte
counts. READY follows the first two samples, so evidence exists before admission.
After the first zero transition, the controller records a monotonic lower bound
and baseline sequence; it must observe two newer sends from that same worker
and recheck effective zero and identity. This is bounded synthetic send evidence,
not proof of business heartbeat/RPC settlement. There is no indefinite ping log.

Finally, only the authenticated helper drop-in is removed; daemon-reload must
leave effective zero, the original identity and no helper/job, while the existing
shutdown override remains. Removing a file is never assumed to undo a runtime
notification override. Any failure/timeout/unknown identity aborts before
workload signals, prints finite phase/unit metadata when available and preserves
evidence: no retries, automatic restoration, kill, stop or restart. The existing
runner can terminate only the timed-out systemctl client; infinite service reload
timeout prevents systemd's reload-timeout kill. Controller observation remains
bounded. Owned disposable-VM cleanup is separate from workload drain.

Local Darwin characterization already shows both normal and failed ASGI cleanup
can propagate shell **143**. `143`, and even Uvicorn's `Finished server process`,
therefore do **not** prove clean cleanup. A separate local check with the real
CLI's default **lifespan=auto** also logged `Application shutdown complete.`
after a synthetic cleanup RuntimeError, while calling the lifespan protocol
unsupported. The earlier explicit `lifespan=on` check did log shutdown failed;
these are separate results, not interchangeable runtime evidence. The native
fixture preserves the canonical CLI (no `--lifespan=on`); finite outcome fields
come from markers in the synthetic body, and record Uvicorn's logs separately.
Those fixture markers are not a new production acceptance signal.
This is a bare synthetic ASGI application, **not FastAPI/Starlette**. Framework
lifespan handling can catch exceptions and emit `lifespan.shutdown.failed`
before reraising; the observed auto-mode log collision must not be generalized
to every exception in the production framework. No framework dependencies or
business code are added by this mode.
The new native mode expects to observe
both 143 results and the different lifespan outcomes, while the unchanged
installer's direct snapshot predicate rejects the snapshots. Unknown result,
lingering cgroup, lost identity, a default-mode window violation or any other
prerequisite failure aborts without forced progress;
it must not count as the expected installer rejection. Owned VM destruction by
the runner remains cleanup, never proof of drain.

Evidence is retained in `bootstrap-characterization.log`, alongside existing
payload/isolation/bootstrap logs. **The first controller-owned native attempt
failed before any characterization SIGTERM**, with runner exit1 and owned cleanup
reported by the controller. Retained evidence:
`/private/tmp/resource-guards-hvf-evidence-4ehsoahl/`. It verified systemd
255.4-1ubuntu8.17, 96 exact payload files, restricted-network canary and the real
active-legacy installer rejection. It then failed the `set_overrides` assertion;
the old log omitted unit/property values, so the mismatch cause is **unknown**.
No native143 result, completed shutdown case or bootstrap success was obtained.
The separately reviewed **second attempt also failed before signals**, with
runner exit1 and owned cleanup reported by the controller. Evidence:
`/private/tmp/resource-guards-hvf-evidence-i7mqv_86/`. Its finite properties confirm
the sole mismatch: worker `WatchdogUSec=5min` after `WatchdogSec=0` plus
daemon-reload; the API and other five worker guard fields matched. Neither attempt
proved native143, completed shutdown, watchdog administration or bootstrap.
The contained administrative variant above is prepared under explicit user
approval but **has not completed its native proof**. It requires independent review and a
controller-owned trial; the separately authorized `--allow-daytime-lab` permits
that disposable characterization now, without waiting for the production window.
No automatic retry or clock override is allowed. Initial hold, authenticated
adoption/release, partial-install recovery, production cutover, business/RPC
closure and natural-cycle observation remain separate uncompleted gates.

The immediate daytime controller trial also **FAILED before any administrative
watchdog transition or workload signal**. Evidence:
`/private/tmp/resource-guards-hvf-evidence-chxt67f0/`. Active-legacy direct
snapshot rejection and the separate daytime CLI blocked observation passed;
API overrides matched. At worker preconditions the unit was failed/exit-code,
MainPID0 and WatchdogUSec5min. Its journal was not retained, so the cause remains
unknown. A persistent notify socket and the ping path outside the canonical
writable exception are source-level concerns, not demonstrated native causes;
the earlier active observation must not be ignored.

Whole-characterization abort instrumentation now records finite worker InvocationID, exit
code/status, restart count, monotonic lifecycle timestamps, effective
ProtectSystem/ReadWritePaths and state/job fields. It requests only this exact
synthetic unit's current-boot journal (40 lines, at most 16384 logged characters,
5-second command timeout). Missing/invalid process or invocation identity never
causes a PID lookup or a whole-journal fallback: this journal is explicitly
unit-scoped, not attributed to one invocation. It also records ping-directory
lstat metadata and at most 2048 characters of findmnt output, with a 5-second
timeout. Those filesystem observations describe the **guest root namespace,
not the service namespace**, and cannot prove service writability after its
process has vanished. No file contents, environment or arbitrary command-line
dumps are collected. Diagnostic failure is annotated and the original error
is re-raised; there is no recovery, retry, signal or functional socket/path fix.
The collector runs once at the outer main failure boundary, only after verifying
Linux/root/QEMU and the laboratory hostname. It covers startup, initial rejection,
watchdog and shutdown failures; the inner watchdog still logs its finite phase
metadata without duplicating journal collection. A subsequent diagnostic trial
in `/private/tmp/resource-guards-hvf-evidence-r2gjatmy/` failed earlier, at initial
installed-files snapshot equality, before reaching the former watchdog-only
collector. That trial also failed without a worker journal; equality and all
other safety gates remain unchanged by this coverage correction.

The next diagnostic trial, `/private/tmp/resource-guards-hvf-evidence-h98wq3sc/`,
confirmed the worker failure: `OSError: [Errno 30] Read-only file system` at the
atomic ping temporary file under `/opt/native-bootstrap-characterization/`.
The effective unit reported ProtectSystem=strict and only the canonical APP/logs
writable exception. The synthetic ping file is therefore moved to
`APP/logs/native-bootstrap-watchdog/latest.json`; setup creates/chowns logs first,
then the estrado-owned 0700 subdirectory. Atomic two-record writes remain 0600.
Only this generated runtime subdirectory is excluded in the guest's synthetic
.gitignore before its fixture commit; source and unrelated logs remain visible
to the unchanged exact-tree gate. Host regression runs the actual body in the
configured relative path and checks real Git ignore/status behavior without an
index update or commit. Neither ProtectSystem/ReadWritePaths nor the persistent
notify socket changes. Its socket-failure hypothesis was not demonstrated.
The earlier active observation is retained as observed, not used to bypass the
subsequent mismatch. The following controller trial verified that path correction.

The trial at `/private/tmp/resource-guards-hvf-evidence-fmihcnrk/` **FAILED**
later: watchdog 0→300000000→0, same identity and two post-zero pings passed;
normal API retained Code1/Status143. The worker disappeared, but its final
inactive/dead/success record had Code/PID/timestamps0 and empty InvocationID.
The harness rejected those unavailable execution fields before calling the real
stopped snapshot. The worker body marker is not an exit-status proof. Unit GC
is compatible with this observation, but no GC event was demonstrated.

The revised lab-only contract (native validation pending) records pre-signal
ExecMainPID/start/InvocationID and all three group members on the same boot.
That original worker record survives both API cases. It requires every prior
PID absent (reuse aborts), frozen empty-cgroup checks, MainPID/ControlPID0,
NRestarts0, no Job, disabled units and unchanged trusted files/lock/authorities.
Worker observations allow exactly two mutually exclusive forms:

- `retained-clean-exit-record`: Code1/Status0, the original positive ExecMainPID,
  original positive start timestamp and InvocationID, positive active/state-change
  timestamps and an exit timestamp no earlier than start.
- `execution-metadata-unavailable`: Code/Status/PID0, all four timestamps0 and
  empty InvocationID, explicitly `worker_exit_status=unknown`. No exit0, clean
  shutdown or business claim follows. Partial/default mixtures and failures abort.

API must retain its original wrapper execution record and actual failed143.
The unchanged installer's authenticated line216 frame must have
`values is services[API]`; the worker in that same real map is also validated.
Finite auxiliary properties supply fields absent from the installer schema;
their common projection must equal the untouched real map, with exact repeated
observations before/after the direct call and after CLI. No frame fields or
statuses are filled or normalized. The active-legacy proof and source binding
are unchanged. CLI blocked remains separate, its internal gate not exposed.
This completes neither Task3/bootstrap nor production/business shutdown proof.

The subsequent trial `/private/tmp/resource-guards-hvf-evidence-_iex_3np/`
**FAILED before reaching that revised final-metadata contract**, at the initial
active-proof snapshot equality. Worker activation preceded its recorded Python
MainPID handoff by about370ms. No before/after maps were retained, so the exact
changed field is unproven. Xvfb's supported READY notification is a source-backed
explanation for early readiness, not an attributed datagram from that trial.

The startup observer now precedes the single active proof (native validation
pending). It has a30-second monotonic deadline and polls only the recognized
initial xvfb-wrapper state. It anchors boot/invocation/wrapper and every seen
member; failed/restarted/unknown states, unsafe metadata or identity drift abort.
Readiness requires actual MainPID Python with its exact configured argv, expected
parent, three-member kernel group and two validated pings from that same worker.
An existing malformed/untrusted ping file is never treated as pending. No new
READY/ACK, runtime helper, unit, sleep-based settling delay or installer retry is
introduced. After one ready pin, the proof retains exact before/after equality
and revalidates that pin. Any installed-snapshot mismatch prints only differing
keys and their finite selected before/after values, then aborts. The production
installer, all final-metadata restrictions and real CLI behavior remain unchanged.

The approved base-image hash/QEMU path, 2CPU/4GiB, 30% admission/12% watchdog,
1800-second supervisor, readonly ISO, ephemeral identity/key, no host shares or
agent forwarding, restricted test networking and owned cleanup are unchanged.

## Nested emulation runner

This laboratory runs a disposable Ubuntu 24.04 ARM64 guest inside QEMU, inside
an unprivileged Docker Desktop container. It never connects to the VPS. No host
directory, Docker socket, device, SSH agent or published port is passed through.
The same explicit ops/fixture/stdlib allowlist is copied. A fresh guest-only SSH key is destroyed with
the container. Bootstrap uses the network for Ubuntu packages; Docker networking
is disconnected and verified before any test payload runs.

```sh
python3 ops/tests/native/run.py --build
python3 ops/tests/native/run.py --integral
```

Requires macOS ARM64 Docker Desktop with a local Unix socket and no `DOCKER_HOST`
or `DOCKER_CONTEXT` override. The resolved endpoint and image ID are pinned for
the run. Only the UUID-named, owner-labelled container is removed; no global prune
or changes to existing containers. Bootstrap is bounded to 25 minutes and the
container has an independent 60-minute timeout.

Host limits: 2 CPU, 3 GiB RAM, no additional Docker swap, 128 processes. The guest
advertises 8 GiB to exercise the real guard's 6 GiB availability gate, but backing
pages are demand-allocated beneath that hard host limit. This is intentionally
overcommitted: bootstrap or tests can exhaust it; OOM/termination means failed
validation, never permission to increase limits or touch production. This proves
contracts and recovery, **not** host capacity or resilience under memory load.

`probe.py` characterizes omitted `EnvironmentFiles`, unrestricted address families,
and a removed running timer's `not-found` / `failed` / exit 4 state. It invokes the
actual selected production functions. Before those fixes, two probes must fail.
`--integral` runs `fixture.py` and `exercise.py` after the native probes.
It exercises an exact-SHA preflight/apply/postflight, real swap and oneshot
sandboxes, manual rollback, then one injected HTTP failure and automatic rollback.
The 16 managed paths (excluding credential contents), swap-file metadata, unit activity/enablement,
worker cgroup, swap and swappiness must equal the pre-apply baseline after each
rollback. Local monitor events must report no active unhealthy/unknown rule.
`fixture.py` prepares harmless API/operation/Hermes doubles for the integral rollout;
it refuses a reused fixture or a non-QEMU/non-laboratory guest. Real application
traffic, browser automation and external credentials are not used. Integral
rollout execution is a separate gate, not implied by a successful probe.

## Real maintenance protocol exercise (Task 4)

The compatible baseline has legacy resource placement (`system.slice`), not a
legacy worker: production `MaintenanceStore`, `WorkerMaintenance` and `sd_notify`
run under real `xvfb-run`. Control is initialized explicitly while the isolated
fixture worker is stopped; this does not implement production legacy bootstrap.
systemd creates the 0700 runtime ACK directory. The worker UID cannot write
control, replace the stable lock, or create entries in the control directory.
The fixture exports complete matching `RG_TEST_MODE=1` and `WM_TEST_MODE=1`
boundaries. WM still uses real guest Python/flock/date/sleep/systemctl, `/proc`,
protocol paths and root/estrado IDs resolved from the guest account database.
Only health points to the local fixture API. This same environment reaches the
maintenance helper and delegated provision; there is no fake maintenance CLI.

The exercise authenticates CLI/kernel/cgroup/MainPID and the Python ACK publisher,
then covers:

- A helper dying after genuine hold/drain; hold survives loss of both leases.
- Rejection of stale operation/PID/start-ticks/boot/nonce ACK proofs.
- Closed restart under continuous real EX leases, recreated RuntimeDirectory,
  new nonce/kernel identity and blocked new work, followed by validated release.
- Incompatible installed unit and absent legacy ACK rejected before mutation.
- An operation admitted before apply/manual rollback; live work retains SH while
  hold blocks a new operation, then the operator drains and changes lifecycle.
- Real apply/postflight and both exact rollback snapshots, with hold checked
  separately. Control and its stable lock are never part of restoration.
- One intended final-postflight HTTP failure and automatic rollback, still held.

After each rollback, the exercise first proves exact baseline restoration and
then explicitly invokes low-level CLI `finish` with current identity, EX and API
health. The restored legacy resource layout intentionally fails the full new
guards postflight; this fixture release is not a recommendation to bypass a
production recovery gate. No API/worker business E2E or remote-outcome proof is
claimed from these local operation doubles.

## Validation status (2026-08-30)

Task 4 host/Linux regressions cover the real fixture coordinator, actual Unix
datagram, payload boundary, identity comparisons and CLI publication failures.

The prior corrected controller-owned HVF integral trial **PASSED** on systemd
255.4-1ubuntu8.17. Evidence is retained at
`/private/tmp/resource-guards-hvf-evidence-ixpgrme8/`:

- `integral.log`: actual MainPID/ACK identity, helper-death hold, five stale ACK
  fields rejected, closed service restart and release, incompatible unit/missing
  ACK rejected, preflight/apply/postflight success and active alert keys `[]`.
  Manual rollback matches the baseline and retains hold before validated release.
  The intended final-postflight fault reaches automatic rollback, also exact,
  held, and explicitly released; final marker is
  `NATIVE AUTOMATIC ROLLBACK EXACT; REAL MAINTENANCE INTEGRAL PASS`.
- `probe.py.log`: all three native probes pass; tested resource-guards SHA256
  `b30dc3804402c77c3b75d09fecb85737910ae242485d25ea4163a8479ccd0013`.
- `payload.log`:83 exact files, manifest SHA256
  `ed17c8b38441a8b8465f5cc7110481c156c3e7c65f0a0b33fbc1b9bc821fb788`.
- `isolation.log`: restricted QEMU user networking; host TCP probe refused.

The controller reports runner exit0 and cleanup of its owned guest disk/seed/key.
This proves the enumerated local fixture contracts, not RAM admission/capacity,
stress isolation, business E2E/remote effects or a full host reboot during hold.
That trial predates the final I1–I3 fixes. The current payload has86files and five
stdlib worker modules; the fixture now publishes `paused` plus the same exact
maintenance proof as production Metrics, including a cold-start phase marker.
Host tests exercise that projection and its hashed stdlib-only transport. A fresh
controller-owned trial of those final bytes also **PASSED**, runner exit0, in
`/private/tmp/resource-guards-hvf-evidence-wpmowowc/`. Its86-file payload used
guard SHA256 `58c9bdc3d296ab723a990014cae1a696cce20ef32a244a135dfb812ec687c380`
on the same systemd version. It revalidated the enumerated identity, closed
restart, apply/postflight, manual rollback and injected-failure automatic rollback
legs, with local active alerts `[]`; both recoveries retained hold until explicit
release. Restricted host TCP was refused and owned VM disk/seed/key were removed.
The same capacity/business/full-host-reboot exclusions apply. Focused final
review and integration remain controller-owned;
production rollout, legacy bootstrap, natural observation and access authorization
remain separate gates.

The first controller-owned maintenance-aware HVF trial verified actual
MainPID/ACK identity, helper-death hold, rejection of five stale ACK fields,
closed restart and validated release. It did **not** complete: the first guard
entry rejected the incomplete RG/WM fixture environment with exit2, before
preflight/mutation. Its evidence remains in
`/private/tmp/resource-guards-hvf-evidence-7d_xaa_e/`. Fix round2 completed that
boundary and tested actual guard/helper/provision initialization before the
separate successful corrected trial above. Neither attempt is relabeled or
combined with the earlier dummy-worker result.

The earlier, pre-maintenance accelerated HVF integral run **passed** on real Ubuntu systemd
255.4-1ubuntu8.17: all three probes, preflight, apply, postflight, local monitoring
without active alert keys, manual rollback and an automatic rollback after the
intended final-postflight HTTP failure. Both rollbacks matched the enumerated
baseline surfaces below. The guest verified 59 payload files; the tested
`ops/resource-guards.sh` SHA256 was
`8bfdfb8f39879892de10e3ffbcf61dfa33249f18b100978912536629fa9eeaca`.
The owned disk, seed and ephemeral SSH key were removed after completion.

This result excludes RAM admission/capacity and stress isolation, as described
above. Applications and HTTP endpoints were local test doubles. It does not
authorize or prove a production rollout, natural PJUD observation or guest access.

The earlier nested TCG integral attempt did **not** pass: its initial preflight
timed out enumerating enabled system services over D-Bus, before any `apply`.
Do not weaken production inventory/time/memory checks to accommodate emulation.

Snapshot helpers reject symlinks, including dangling links and ancestors, and
record file type. Credential contents and swap-file contents are never hashed.
The injected-failure leg must prove it reached the intended HTTP failure in
postflight; an earlier failure followed by rollback is not a passing test.

Ubuntu cloud image is checksum-pinned to the official 20260826 release; packages
come from Ubuntu repositories and are not version-frozen. Probe output records
the installed systemd version and source hash. The Docker runner records its image
ID; HVF validates the pinned base-image checksum and reports the QEMU version.
See [Ubuntu image checksums](https://cloud-images.ubuntu.com/noble/20260826/SHA256SUMS),
[NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html),
and [QEMU ARM virt](https://www.qemu.org/docs/master/system/arm/virt.html).
