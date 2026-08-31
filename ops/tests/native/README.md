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
