# Native systemd validation (local laboratory)

This laboratory runs a disposable Ubuntu 24.04 ARM64 guest inside QEMU, inside
an unprivileged Docker Desktop container. It never connects to the VPS. No host
directory, Docker socket, device, SSH agent or published port is passed through.
Only tracked `ops/` source is copied. A fresh guest-only SSH key is destroyed with
the container. Bootstrap uses the network for Ubuntu packages; Docker networking
is disconnected and verified before any test payload runs.

```sh
python3 ops/tests/native/run.py --build
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
`fixture.py` prepares harmless API/worker/Hermes doubles for the integral rollout;
it refuses a reused fixture or a non-QEMU/non-laboratory guest. Real application
traffic, browser automation and external credentials are not used. Integral
rollout execution is a separate gate, not implied by a successful probe.

Ubuntu cloud image is checksum-pinned to the official 20260826 release; packages
come from Ubuntu repositories and are not version-frozen. Probe output records
the installed systemd version and source hash; runner records the image ID.
See [Ubuntu image checksums](https://cloud-images.ubuntu.com/noble/20260826/SHA256SUMS),
[NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html),
and [QEMU ARM virt](https://www.qemu.org/docs/master/system/arm/virt.html).
