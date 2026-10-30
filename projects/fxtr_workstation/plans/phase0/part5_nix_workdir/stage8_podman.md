# Stage 8 — Podman runtime standardization

Standardize the **live** container-sandboxing path on **podman**: flip
the default runtime, replace the transient oci-archive-tarball delivery
with a tarless skopeo stream into podman's docker-compatible API
socket, and **remove the deprecated in-band-bytes container paths**
(`DockerTaskBundle` / `PyContainerTaskBundle`) whose backend-specific
flags were the only thing standing between us and a clean "one runtime,
one portable CLI subset" model. This is the first of two stages; the
second —
`projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage9_nix_runner.md`
— moves *nix itself* into a podman container so the host needs neither
nix nor skopeo. Stage 8 keeps host nix doing all building.

Design discussion settled 2026-06-04 (full decision log:
`stage8_podman_scratch.md`):

- **Podman is the supported runtime; others are an escape hatch, not a
  feature.** `get_runtime()` keeps its string model (the live path uses
  only the docker-CLI-portable subset: `run --rm --network none -v
  --platform`, `image inspect`) with the default flipped to
  `"podman"`. `FXTR_CONTAINER_RUNTIME` is retained as an
  *undocumented-ish* escape hatch (Apple Container / docker), kept for
  operational safety during the transition — notably while amd64 runs
  under qemu-user (Rosetta is off the table until a macOS 26 upgrade,
  see below) — not as a designed multi-backend commitment.
- **Deprecated container paths are removed, not migrated.** The
  `insecure_registry → --scheme http` flag was Apple-Container-specific
  syntax rendered into a supposedly runtime-agnostic command line
  (already broken under `FXTR_CONTAINER_RUNTIME=docker` today). Rather
  than build a `RuntimeSpec` abstraction whose only beneficiary is
  frozen code, we delete the frozen code (it has a live, tested
  replacement since
  `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md`).
  The deprecated **wasm** `TaskBundle` path is untouched — it involves
  no container runtime.
- **Delivery becomes a single branch, not an abstraction.** podman →
  `skopeo copy nix:<json> docker-daemon:<ref>` against the podman
  machine's API socket (tarless; streams layers straight out of the nix
  store). Any other runtime → the existing oci-archive + `image load`
  code, unchanged, as the generic fallback. The
  `_load_image_into_runtime` seam from stage 7 is exactly where this
  lands.
- **Image refs are fully qualified under podman**
  (`localhost/fxtr-nix-service:<storehash>`) so podman's
  unqualified-name registry resolution can't interfere with
  inspect/run. Other runtimes keep the bare ref.
- **Cache clearing is name-based pruning** (a small helper that
  `podman rmi`s the `fxtr-nix-service` repo). A dedicated/secondary
  image store was considered and rejected for v0: podman's
  `additionalimagestores` are read-only by design and the writable
  graphroot lives inside the machine VM — both make a deletable
  dedicated store disproportionately invasive.
- **Host-env risk, resolved (spike 2026-06-05):** Rosetta under podman
  machine is broken on this macOS 15.1 host (vdso `DT_HASH` assert,
  sibling of stage 7's auxv bug) *and* upstream podman-machine-os
  ships it deliberately disabled ("not functional on kernel 6.13 or
  newer") — the confirmed fix is **macOS 26**, not 15.5
  (containers/podman-machine-os#212; PR #237 makes Rosetta
  client-controlled in newer images). Decision: **qemu-user-static is
  the accepted short-term amd64 path** (works on both providers,
  verified with real python; slow), and the long-term fix is a macOS
  26 upgrade + re-testing Rosetta as a speed optimization. Stage 8 is
  not blocked on the host env.

## What changes where

```
fxtr_sandboxing/_container_common.py   get_runtime(): default "container" → "podman"
fxtr_sandboxing/nix_container_runner.py  delivery branch (podman socket vs generic tar),
                                         localhost/-qualified refs, cache-clear helper
fxtr_sandboxing/spawner.py             deprecated container arms + kwargs removed
fxtr_sandboxing/{container_runner,py_container_runner}.py   deleted
fxtr_sandboxing/deprecated/…           container/py_container modules deleted
fxtr_sandbox_types/…                   deprecated container entities + union trim
examples/ + tests/                     deprecated container demos/tests deleted;
                                       nested-spawn container-inner test ported
```

The nix-side declaration surfaces (`mkContainerService`, the `kind`
discriminator, `NixContainerServiceDefn`) and the spawner-side
resolution (`fxtr_sandboxing/nix.py`) are **untouched** — exactly as
stage 7 promised when it isolated delivery.

## Chunk 1 — derisk spike (manual, gate for everything else)

Mirror the stage-7 chunk-1 pattern: a throwaway spike dir + NOTES.md,
findings folded back into this plan before implementation proceeds.

a. **podman machine + Rosetta on macOS 15.1.** `podman machine init`
   (applehv, rosetta on — the default), run a `linux/amd64` image, and
   specifically exercise real binaries (e.g. python) to smoke the auxv
   bug. Outcome decides: proceed / upgrade macOS first.
b. **UDS socket-file bind mounts.** Host-side `anyio` UDS listener,
   bind-mount the *socket file* into an amd64 container
   (`-v <sock>:/run/fxtr/task.sock`), connect from inside. This is the
   load-bearing assumption of the whole UDS transport under podman
   machine (host mac → virtiofs → VM → container). If it fails:
   fallback design is TCP loopback via gvproxy
   (`host.containers.internal` + per-spawn random port + an auth
   token in the routing header) — a real design change we'd rather
   know about now.
c. **Tarless delivery.** `skopeo copy nix:<json>
   docker-daemon:localhost/fxtr-nix-service:<hash>` with
   `--dest-daemon-host unix://<machine socket>`; confirm streaming
   works, the ref is visible to `podman image inspect`, and
   `podman run` of it works with `--platform linux/amd64`. Also confirm
   the host-side machine-socket path discovery (`podman machine
   inspect` / `podman info`).
d. **Flag subset sanity.** The exact `run` invocation
   `nix_container_runner` builds today, verbatim, under podman.

### Spike findings (2026-06-04/05 — all done)

Full detail in the spike dir's `NOTES_STAGE8.md`.

- **(a) Rosetta: broken until macOS 26; qemu-user is the path.** The
  default machine here is **libkrun** (`containers.conf`), where
  Rosetta is unsupported and amd64 runs via **qemu-user-static
  binfmt** — which *works* (verified with real python). On a second
  **applehv** machine, Rosetta is plumbed in (`Rosetta: true`, VZ
  shares the `rosetta` virtiofs tag) but the FCOS image ships the
  activation unit disabled behind a marker file, citing breakage on
  kernels ≥ 6.13; force-enabling it dies instantly on amd64 python
  (`Failed to find vdso DT_HASH`, guest kernel 6.19). Upstream
  confirms the fix only in **macOS 26** (podman-machine-os#212), with
  newer images making Rosetta client-controlled (PR #237). amd64
  under qemu-user verified working on applehv too — the fallback is
  provider-independent. Decision: ship on qemu-user now; revisit
  Rosetta after a macOS 26 upgrade (recreate the machine, re-test).
- **(b) host-mac UDS bind mounts do NOT work** (virtiofs shares the
  inode, not the listener → `ECONNREFUSED`). An ssh `-R` tunnel
  against the *machine VM's* sshd was validated first but rejected:
  its listener is `unconfined_t`, so the SELinux `connectto` peer
  check forces either `label=disable` (sandbox unconfined) or a
  VM-level CIL policy module — downgrading or fighting the VM's
  security posture either way, plus chmod/chcon/stale-socket
  incantations against an sshd we don't configure.
  **Settled design (validated end-to-end 2026-06-05/06): sshd
  *sidecar* in a shared pod, inetd-mode transport** — per spawn, one
  `podman pod create --network none` pod holds a tiny sidecar and the
  sandbox container, sharing a named volume (`:Z`); the host opens
  `ssh -R <vol_sock>:<host_sock>` whose transport is
  `ProxyCommand=podman exec -i <sidecar> /usr/sbin/sshd -i` (inetd
  mode — sshd speaks the protocol on the exec's stdio, riding the
  machine API socket; **no listening sshd, no nc, nothing on the pod
  loopback at all** — verified: the sandbox's 127.0.0.1:22 probe gets
  ECONNREFUSED). The forwarded socket lands on the shared volume,
  where the sandbox's connect is plain same-kernel
  container↔container traffic:
  - **No SELinux changes at all.** Spike tested a 3-mode ladder
    (`uds_sidecar_volume_test.py`): default labels just work — fresh
    named volumes are `container_file_t:s0` (usable by all
    containers) and `connectto` passes `container_t → container_t`
    even across MCS category pairs. The CIL module was purely an
    artifact of the `unconfined_t` listener.
  - **The pod gives per-spawn MCS compartmentalization for free**
    (`uds_sidecar_pod_test.py`): podman assigns all containers in a
    pod one shared, pod-random SELinux label, and `:Z` labels the
    volume with that pair — verified that a container outside the
    pod mounting the same volume gets `EACCES` on listing *and*
    socket connect. (This is the "mode C" hardening from the volume
    test, without us managing categories.) `pod rm -f` is the single
    teardown handle for sidecar + sandbox + infra.
  - **Nothing listens anywhere.** The pod is `--network none`, and
    with inetd-mode sshd there is no listener even on the pod's
    isolated loopback — the sidecar's main process is `sleep
    infinity` (it exists to hold pod membership, the volume, host
    keys, and authorized_keys). No gvproxy exposure — the isolation
    objection that killed TCP-via-gvproxy doesn't apply; ssh provides
    the multiplexing that bare exec-stdio relays lacked. Guest
    contract unchanged. The sidecar's host key is **pinned** in the
    host's ssh client (fetched via `podman exec`, baked at image
    build so there's no key-generation race), not
    `StrictHostKeyChecking=no`.
  - **Readiness is positive, not retried**: the tunnel session runs a
    remote command (`while [ ! -S <vol_sock> ]; …; echo READY; exec
    sleep infinity`) instead of `-N`; the host reads READY (with
    `ExitOnForwardFailure=yes` so a failed bind kills the session
    rather than hanging the wait) before starting the sandbox, so
    the guest's single `connect()` needs no retry loop — and the
    foreground ssh process handle survives for teardown (no `-f`
    orphaning).
  - **We own the sidecar's `sshd_config`** — `StreamLocalBindUnlink
    yes` + `StreamLocalBindMask 0111` eliminate the stale-socket and
    chmod dances. Gotcha for the image: Alpine-style hardened
    defaults (`AllowTcpForwarding no`) also deny remote *streamlocal*
    forwards even in inetd mode; the sidecar config must set
    `AllowTcpForwarding yes` (harmless without network).
  - Validated chain: host anyio listener ← ssh -R ← sidecar sshd ←
    shared volume ← `--network none` amd64 sandbox, echo round trip
    (`uds_sidecar_sshd_test.py` standalone-container shape;
    `uds_sidecar_pod_test.py` pod shape + label/compartment checks;
    `uds_sidecar_inetd_test.py` inetd transport + READY readiness +
    pinned-host-key flow, single-connect, loopback-refused check).
- **(c) tarless delivery validated.** `skopeo copy nix:<json>
  docker-daemon:localhost/fxtr-nix-service:<tag>` with
  `--dest-daemon-host unix://<machine API socket>` (socket path from
  `machine inspect .ConnectionInfo.PodmanSocket.Path`) streams from
  the nix store with no tarball; `image inspect` shows the right
  arch; `run` with the baked `Cmd` works. `localhost/` qualification
  behaves as planned.
- **(d) confirmed.** `--rm --network none --platform -v` all behave
  verbatim under podman; no new run flags needed (the settled sidecar
  design requires no SELinux changes at all).

## Chunk 2 — remove the deprecated container paths (done 2026-06-06)

Inventory (audited 2026-06-04 — outside `fxtr_sandboxing` only a stale
MCP wheel-cache copy references these; no live consumers):

- `spawner.py`: drop the `DockerTaskBundle` / `PyContainerTaskBundle`
  dispatch arms, their entries in `spawn`'s union (impl +
  `SandboxSpawnerProto` in `fxtr_sandbox_types/protocols.py`), the
  `insecure_registry` / `target_arch` env kwargs on
  `SandboxSpawnerImpl` / `create_sandbox_environment`, and the
  `_detect_target_arch` helper (reads `FXTR_CONTAINER_RUNTIME` for
  py_container only).
- Delete `container_runner.py`, `py_container_runner.py`.
- `deprecated/`: delete `container_prepare.py`, `container_config.py`,
  `container_rebuild.py`, `py_container_prepare.py`, `base_images.py`,
  `_arch_tags.py`; trim `_bundle_materialize.py` to its wasm half.
  **Keep** `prepare.py`, `_prepare_common.py`, `wheels.py` (shared
  with the still-live deprecated wasm path).
- `fxtr_sandbox_types/deprecated_entities.py`: remove `DockerImage`,
  `SourceMount`, `DockerTaskBundle`, `PyWheelArtifact`,
  `PyMultiWheelPackage`, `ArchSpec`, `PyContainerTaskBundle`; keep the
  wasm trio (`PySourcePackage`, `PyWheelPackage`, `TaskBundle`).
  Trim `__init__.py` re-exports + `protocols.py` docstrings/unions.
- `examples/`: delete `container_rpc_task`, `py_container_rpc_task`,
  `container_transport_task`, `py_container_transport_task` (and their
  registry/buildkit `setup.sh` infra). Wasm examples stay.
- `tests/`: delete `test_container_spawner.py`,
  `test_container_config.py`, `test_container_prepare.py`,
  `test_container_rebuild.py`, `test_py_container_*.py`,
  `test_transport_e2e_container.py`. **Port**
  `test_nested_spawn_container_inner.py` to a
  `NixContainerServiceDefn` inner (the
  `_workdir_templates/container_transporter/` template +
  `prepare_workdir_repo`, mirroring how the wasm-inner test works) —
  it's the only deprecated-path test guarding behavior we still want
  (a capability ref bridging across two sandbox boundaries into a
  Linux container).
- `_container_common.py` keeps `get_runtime`, `accept_or_exit`,
  `transport_accept_loop`, `handle_transport_conn`,
  `teardown_container_proc` — all used by `nix_container_runner` (the
  stage-7 "when they're deleted this becomes the only copy" note now
  comes due; fold anything orphaned). `accept_or_exit`'s error
  message lists deprecated-path causes (`insecure_registry`, missing
  registry image, Apple Container Rosetta) — rewrite for the live
  path's actual failure modes.
- Update `CODE_OVERVIEW.md` (fxtr_sandboxing) for the removals.

## Chunk 3 — podman default + tarless delivery (done 2026-06-06)

- `get_runtime()`: default → `"podman"`. Docstring reframed: podman is
  the supported runtime; the env var is an escape hatch for
  docker-CLI-compatible alternatives (delivery falls back to the tar
  path for them).
- New module-level helper in `nix_container_runner.py` (or
  `_container_common.py`): resolve the podman API socket
  (`podman machine inspect --format` on macOS; on Linux the standard
  user/system socket paths; overridable via the standard
  `CONTAINERS_*` env if it turns out to matter — spike decides the
  exact discovery order).
- `_image_ref_for` becomes runtime-aware: `localhost/`-qualified under
  podman, bare otherwise.
- `_load_image_into_runtime`: cache-check unchanged (`image inspect`);
  on miss, branch —
  ```
  podman  → skopeo --insecure-policy copy nix:<json> \
              docker-daemon:<ref>  (--dest-daemon-host <socket>)
  other   → existing oci-archive tmp tar + `image load` + delete
  ```
- Cache-clear helper: `clear_image_cache()` +
  `python -m fxtr_sandboxing.nix_container_runner clear-images` (or a
  similarly small entry point — exact home decided in implementation):
  `podman images localhost/fxtr-nix-service -q | xargs podman rmi`,
  plus the `localhost/fxtr-sshd-sidecar` repo. A sibling
  `clear-spawn-debris` sweep removes orphaned pods/volumes by the
  `fxtr-spawn` label (crash recovery; normal teardown leaves none).
- Run side flags: unchanged (spike-confirmed). The Apple
  Container kernel-pin note from
  `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage7_nix_container.md`
  chunk 1 moves to escape-hatch documentation.
- **UDS sidecar machinery** (new, per spike findings b + 7): on the
  remote-podman path, `_run_nix_container_internal` replaces direct
  socket mounts with a per-spawn sshd-sidecar pod —
  ```
  per spawn:  volume create → pod create --network none →
              sidecar run -d --pod (volume at :Z, host's throwaway
              pubkey in via env) → fetch sidecar host key (podman
              exec; baked at image build, no generation race) →
              one ssh process (ProxyCommand = podman exec -i
              <sidecar> /usr/sbin/sshd -i, pinned host key,
              ExitOnForwardFailure=yes, one -R per socket: task +
              transport, targets on the volume; remote command
              waits for the socket(s) then echoes READY) →
              host reads READY → sandbox run --pod with the volume
              mounted (:Z)
  teardown:   kill ssh → pod rm -f → volume rm
  ```
  Per-spawn ed25519 keypair (tempdir, like the sockets today); the
  guest contract is unchanged — the sandbox sees the same socket
  paths, now under the volume mount. The pod's shared label + `:Z`
  give each spawn a private MCS compartment with no label management
  on our side. Everything created per spawn (pod, volume) carries a
  `--label fxtr-spawn=<id>` so crash debris is sweepable (see the
  cache-clear helper). Detection: `podman info --format
  '{{.Host.ServiceIsRemote}}'` → `true` ⇒ sidecar-pod path (covers
  machine VMs *and* `CONTAINERS_CONNECTION`-style remote setups);
  `false` (native Linux) ⇒ mount host sockets directly as today. The
  runtime escape hatch (Apple Container / docker) keeps the
  direct-mount behavior unconditionally.

  Native auto-teardown of the pod when the sandbox exits was
  considered and skipped: `--exit-policy stop` only fires when the
  *last* container exits (the sidecar is long-lived), and the
  alternatives (shared pod PID namespace with a watcher) weaken the
  sidecar↔sandbox isolation. Host-side teardown + the labeled orphan
  sweep cover it.
- **Sidecar image**: **podman-built from an inline Containerfile**
  (no nix — it's glue, not sandbox payload, and a podman-native
  build is exactly the shape `stage9_nix_runner.md` needs anyway).
  Built on demand at first spawn, tagged
  `localhost/fxtr-sshd-sidecar:<content hash of the Containerfile>`
  so edits invalidate cleanly; native arch. Contents: alpine +
  openssh, host keys baked via `ssh-keygen -A`, authorized_keys
  written from a `PUBKEY` env at start, main process `sleep
  infinity` (nothing listens — inetd-mode sshd is spawned per
  connection by the host's ProxyCommand). sshd_config:
  `PermitRootLogin prohibit-password`, `AllowTcpForwarding yes` (see
  spike gotcha — required even in inetd mode), `StreamLocalBindUnlink
  yes`, `StreamLocalBindMask 0111`. Cache pruning includes it
  alongside `fxtr-nix-service`.

## Chunk 4 — tests + setup docs (done 2026-06-06)

- Existing `test_nix_container_spawner.py` /
  `test_nix_container_transport.py` / the ported nested-spawn test run
  green under podman (machine prerequisite documented in
  `fxtr_sandboxing/CODE_OVERVIEW.md` Development section: one-time
  `podman machine init` + start — no policy/setup steps beyond that;
  tests skip without a runtime as today).
- A focused unit test for `_image_ref_for` / the delivery branch
  (mock the subprocess calls; the E2E suite covers the real thing).
- Confirm `test_nix_spawner.py` / `test_spawner.py` (wasm paths) are
  unaffected.

## Chunk 5 — docs (done 2026-06-06)

- `fxtr_sandboxing/CODE_OVERVIEW.md`: runtime model (podman default,
  escape hatch), delivery description, removal of deprecated container
  sections, test list.
- `overview.md` (this dir): stage map entry; move "podman runtime
  standardization" out of the deferred list.
- `followups.md`: mark "Remove the deprecated in-band-bytes container
  paths" and "Podman runtime standardization" landed (pointing here);
  add a follow-up for collapsing the escape hatch + tar fallback once
  `stage9_nix_runner.md` makes podman load-bearing for nix itself.
- Update CODE_OVERVIEW.md (final check that all touched packages'
  overviews are current).

## End-of-stage check (all verified 2026-06-06, podman machine = libkrun,
## amd64 under qemu-user)

- `podman machine` running on this Mac; an amd64 nix-built service
  image spawns via `NixContainerServiceDefn` with **no tarball ever
  created** (verify: no `fxtr_oci_` tmpdirs); second spawn of the same
  commit is an `image inspect` cache hit.
- Transport round-trips (raw + HTTP) work under podman; nested spawn
  with a container inner works via the git-ref path.
- Spawn teardown leaks nothing: after a spawn exits, `podman pod ls` /
  `podman ps -a` / `podman volume ls` show no leftover sidecar pods,
  containers, or volumes, and no ssh tunnel processes remain.
  `clear-spawn-debris` removes deliberately-orphaned (`fxtr-spawn`
  labeled) pods + volumes.
- `FXTR_CONTAINER_RUNTIME=container` still works end-to-end through
  the tar-fallback delivery (the escape hatch is real).
- `grep -r "DockerTaskBundle\|PyContainerTaskBundle"` over live code
  comes back empty; wasm `TaskBundle` suites still green.
- Image cache prune helper removes all `localhost/fxtr-nix-service` +
  `localhost/fxtr-sshd-sidecar` images and a subsequent spawn
  re-delivers/re-builds cleanly.

## Implementation notes (2026-06-06)

- **Teardown-order subtlety** (now encoded in
  `nix_container_runner.py`): the sidecar pod must be torn down
  *after* the guest's graceful EOF-driven exit — `pod rm -f` SIGKILLs
  pod members, so closing the tunnel before
  `teardown_container_proc` would skip the graceful ladder.
- **Per-spawn cost not yet measured** in isolation (spike open item
  stands): the spawner E2E (2 spawns + cache-hit delivery) runs in
  ~75s wall on qemu-user; the sidecar machinery (volume + pod +
  sidecar start + ssh handshake) is a small share of that. Revisit
  pooling only if it hurts once Rosetta lands.
- **First spawn needs network once**: the sidecar image build pulls
  the alpine base. A nix-built (or stage-9 in-machine-built) sidecar
  image would remove that; fine for now.
- **`podman info` remote-detection runs per spawn** (one subprocess);
  could be cached per-process if spawn rates ever make it matter.
- **Pre-existing test flake surfaced while validating**:
  `test_nested_spawn_wasm_inner` can blow its 30s timeout under
  `pytest -n auto` worker contention (passes solo in ~11s). Unrelated
  to this stage; worth a timeout bump if it keeps tripping.

## Addendum — posture update (2026-06-06, post-stage)

The "others are an escape hatch, not a feature" framing above was
softened after the stage landed (see
`stage8post1_runtime_sweep_tests.md`): **Apple Container is a
supported non-default backend**, not a mere escape hatch — podman is
the recommended flow, but the alternative backend stays *tested* (the
container E2E tests sweep over both runtimes) so it can't rot
silently. docker remains the one true compatibility escape hatch
(untested, not in the sweep). The decision log above is left as
written; read its "escape hatch" wording through this lens.

## Addendum — Rosetta re-opened by macOS 26 (2026-06-06, post-stage)

The host got its macOS 26 upgrade (26.5.1) and the spike's "long-term
fix" was retested the same day. Everything the spike predicted held:

- **Rosetta under podman machine works on macOS 26.** The dev
  machine's `podman-machine-default` was recreated as **applehv**
  (`[machine] provider = "applehv"` in
  `~/.config/containers/containers.conf`; `rosetta=true` is the
  applehv default). Current podman-machine-os images (5.8) still ship
  the activation unit gated behind a
  `ConditionPathExists=/etc/containers/enable-rosetta` marker — the
  upstream "test it when Apple ships a fix" hook from
  podman-machine-os#212. Creating the marker + starting
  `rosetta-activation.service` registers the rosetta binfmt and
  unregisters qemu-x86_64. Real amd64 python runs cleanly (no vdso
  `DT_HASH` assert) at near-native speed. One-time step + the
  marker-survives-stop/start-but-not-machine-rm gotcha are documented
  in `fxtr_sandboxing/CODE_OVERVIEW.md` (Development).
- **The full container E2E sweep is green on the new machine** (12/12,
  both runtimes), so the fxtr flow — tarless delivery, sidecar pods,
  transports, nested spawn — needs no changes to benefit: amd64
  sandboxes just got fast.
- **Apple Container (0.9.0) survived the OS upgrade** — same sweep,
  same result.
- **qemu-user is demoted from "the accepted short-term amd64 path" to
  the documented fallback** for hosts still on macOS < 26 (where guest
  Rosetta genuinely stays broken). It still works on both providers;
  nothing in the code branches on it.
