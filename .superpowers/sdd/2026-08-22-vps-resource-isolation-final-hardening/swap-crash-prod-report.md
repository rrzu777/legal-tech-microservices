# Production fix: swap crash/reboot recovery and rollback prevalidation

Date: 2026-08-23
Branch: `feature/vps-resource-isolation`
Exact wave base: `1ed1957130b78da59609aaf19ed4e594311e8ea7`
Concurrent reviewed HEAD retained: `cc5d0f28384d7178baabf67177c1bc058c53c9b0`

## Scope

This wave closes the two Important pre-integration blockers from the whole-branch
audit:

1. standalone swap apply now persists an exact durable phase before every
   protected side effect and can recover every transaction-produced crash
   prefix, including deterministic literal reboot replay;
2. orchestrated rollback validates the backup swap marker and strict standalone
   ownership state before it may quiesce or stop a changed active PJUD worker.

No production/VPS, GitHub, credential, network, PJUD/proxy, alert-delivery, or
guest-environment mutation was performed.

## F1: crash and literal reboot recovery

The former apply bookkeeping existed only in process memory. A SIGKILL after a
valid side-effect prefix could therefore leave ownership artifacts that the
rollback inspector rejected forever.

The new metadata is an exact root-owned `0600` record with version, original
live swappiness, and phase. Each transition is written to a same-directory
exclusive temporary, fsynced, atomically replaced, and followed by parent
directory fsync before the protected effect. Safe stale writer temporaries are
validated as a complete set before any deletion; malformed names, symlinks,
wrong mode/identity, or mixed safe/unsafe sets fail without mutation.

The strict classifier accepts only exact phase-bound pre/post tuples for:

- apply: `swapfile`, `mkswap`, `fstab`, `sysctl`, `swappiness`, `swapon`, and
  `complete`;
- rollback: `rollback-swappiness`, `rollback-swapoff`, `rollback-fstab`,
  `rollback-sysctl`, `rollback-swapfile`, and `rollback-metadata`.

Literal reboot replay is narrow and artifact-derived: a validated durable
managed fstab may reactivate only the exact target, and a validated durable
managed sysctl file may restore only live swappiness `10`. Reboot before managed
fstab durability does not authorize an active target. Rollback-phase replay can
repeat the RAM gate and exact-target `swapoff`, preserve/recover its phase, and
then converge to byte-identical original fstab, original live swappiness,
inactive target, metadata-last deletion, and no managed artifacts.

RED evidence:

- initial SIGKILL/stale-temporary matrix: `86 ok, 4 fail`;
- mixed safe/unsafe stale set atomicity: `89 ok, 1 fail`;
- initial literal reboot matrix: `118 ok, 23 fail`;
- every phase-before-effect crash matrix: `179 ok, 21 fail`.

Final focused evidence:

- apply crash/phase/reboot and every rollback phase/effect reboot recovery:
  `278 ok, 0 fail`;
- durable phase-before-effect gate: `28 ok, 0 fail`;
- rollback retry: `81 ok, 0 fail`;
- swappiness recovery: `40 ok, 0 fail`;
- RAM-gated apply compensation: `33 ok, 0 fail`.

## F2: swap authority before worker quiesce

`resource-guards rollback` now validates `swap-state` and calls the standalone
non-mutating `rollback-preflight` classifier before worker quiesce. The public
output is allowlisted; unknown output, truncated metadata, stale markers, and
marker/live-state conflicts fail before stop/start/restart, provision, manifest
restore, daemon reload, or swap mutation.

Focused evidence:

- stale/truncated/conflicting marker with changed active worker: `27 ok, 0 fail`;
- durable outer `attempted` marker, delegated inner recovery, failed first
  rollback, and same-`BACKUP_DIR` retry convergence: `9 ok, 0 fail`.

## Final verification

- full standalone swap harness: `668 ok, 0 fail`;
- full resource-guards harness: `840 ok, 0 fail`;
- Bash syntax, ShellCheck warning severity, and `git diff --check`: clean;
- diff-only credential-shaped, public-IP, and forbidden PJUD/proxy action scans:
  no matches.

## Self-review and residual gates

Self-review specifically checked every phase transition against the exact tuple
allowlist, all active reboot paths against the RAM gate, metadata-last cleanup,
and mixed safe/unsafe stale writer artifacts. It found and fixed two issues
before final verification: stale temporaries initially needed two-pass set
validation to guarantee zero mutation, and process-death-only tests missed
fstab/sysctl boot replay.

The mandated no-subagent boundary prevented independent review in this focused
task, so review was diff-based self-review plus executable characterization.
Real Linux kernel swap, boot-time fstab/sysctl ordering, GNU filesystem fsync and
rename behavior, procfs/flock, systemd/cgroup-v2, and the production maintenance
window remain external rollout gates.
