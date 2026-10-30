# Stage 8 scratch — decision log (2026-06-04 chat)

Raw decision trail behind `stage8_podman.md`. Not a spec; the plan wins
where they disagree.

## Why removal instead of a runtime abstraction

The starting question was "what does the podman cutover do to
`get_runtime()`?" Investigation showed the runtime-as-command-string
model is fine for everything the **live** path does (`run` with the
docker-CLI-portable flag subset, `image inspect`) — the only genuinely
backend-specific code anywhere was the deprecated `container_runner` /
`py_container_runner` rendering `insecure_registry` as Apple
Container's `--scheme http`. That flag was *already broken* under
`FXTR_CONTAINER_RUNTIME=docker` (docker has no `--scheme`; docker
expresses insecure registries daemon-side). Three options considered:

1. `RuntimeSpec` abstraction that renders per-backend concepts —
   rejected: machinery whose only beneficiary is frozen,
   deletion-scheduled code.
2. Remove the deprecated container paths (they have a live replacement
   since stage 7, and a removal follow-up was already filed) —
   **chosen**. Dissolves the problem; no abstraction needed.
3. Pin deprecated paths to legacy defaults (per-caller default arg) —
   rejected as papering-over once (2) was on the table.

Post-removal end states for the runtime knob:

- **A. podman-only** (delete the env var, `get_runtime` → podman
  discovery): cleanest, but burns the Apple Container bridge while the
  macOS 15.1 Rosetta risk is unresolved.
- **B. podman-default + generic fallback** (string knob stays for the
  portable subset; delivery is one `if`: podman → socket, other →
  existing tar+load): **chosen for stage 8**. Daniel explicitly wants
  the host-nix + Apple Container combo preserved through stage 9 too,
  even with podman recommended. Collapse toward A later, from a
  position of knowledge.

## Delivery

- `containers-storage:` direct-write requires skopeo to see podman's
  graphroot — inside the machine VM on macOS, so a host skopeo can't.
  Writing it from *inside* a container (stage 9) would mean
  bind-mounting podman's graphroot+runroot and trusting
  containers/storage's multi-process locking + version compatibility —
  feasible in principle, rejected as invasive. Noted as a possible
  later optimization only.
- Chosen: `skopeo copy nix:<json> docker-daemon:<ref>` with
  `--dest-daemon-host unix://<podman socket>`. Tarless (streams over
  the socket), identical shape from the host (stage 8) and from inside
  the nix builder container (stage 9).
- `localhost/` qualification: podman normalizes unqualified names
  through its registry-resolution machinery; fully qualifying as
  `localhost/fxtr-nix-service:<hash>` keeps inspect/run unambiguous.

## Image cache store

Daniel's original ask was an "explicitly temporary storage location,
maybe an additional image store." Findings: podman
`additionalimagestores` are read-only by design (populated
out-of-band); the writable graphroot lives in the machine VM on macOS;
`podman --root` is ignored in remote mode (macOS is always remote). A
second podman machine is overkill. Settled on name-based pruning of the
`fxtr-nix-service` repo for v0 — operationally equivalent (one command
wipes the cache), zero machinery.

## Risks

- **Rosetta auxv (macOS 15.1)**: stage 7 hit `unhandled auxillary
  vector type` with guest kernels ≥ 6.11 on macOS < 15.5 and pinned
  Apple Container's kernel to 6.1.62. podman machine boots FCOS
  (kernel ≥ 6.11) and the kernel is not practically pinnable. Daniel's
  call: try Rosetta in the spike; if it bites, upgrade macOS. qemu
  fallback explicitly rejected.
- **UDS over virtiofs into podman machine**: Apple Container needed
  socket *files* mounted individually (sockets inside directory mounts
  → ENOTSUP); whether podman machine supports host-mac socket-file
  bind mounts at all is the spike's other gating question. Fallback
  sketch if not: TCP loopback via gvproxy
  (`host.containers.internal`, per-spawn random port, token in the
  routing header). Hopefully not needed.

## Removal inventory facts (audited 2026-06-04)

- Outside `fxtr_sandboxing`, the only references to the deprecated
  container entities are a stale wheel-cache copy under
  `projects/fxtr_workstation_agent_plugin/fxtr_workstation_mcp/.cache/`
  — not a consumer.
- `tests/test_nested_spawn_container_inner.py` builds its inner via
  `prepare_container_task` → must be ported to
  `NixContainerServiceDefn` (it guards capability-bridging across two
  sandbox boundaries — behavior we keep).
- `wheels.py` is shared with the deprecated **wasm** path
  (`_bundle_materialize.build_site_packages_for_bundle`) → keep.
- `_arch_tags.py` is py_container-only → delete.
- The wasm deprecated path (`TaskBundle`, `prepare.py`,
  `_prepare_common.py`) is out of scope — no container runtime
  involved.
