# Scoping: reaching the build-time rehost from inside nix builds

> Status: scoping doc (2026-06-12), per the OPEN-3 discussion — "extend the
> nix runner so that both host and podman nix allow you to tunnel in the
> localhost rehost". Options below need verification spikes before a plan;
> nothing here is implemented.
>
> **Fourth-sync reshape (2026-06-13):** the rehost now lives **in
> `fxtr_nix_runner`** (the manager tier — it gained a fxtr2 dep), serving the
> **repo entity store** rather than a separately-opened git-index namespace,
> and is stood up **unconditionally** per build span (RUNNER-2/4). The
> `expose_host_endpoint` + extra-build-env hooks land on the transport
> `Handle` (kept fxtr2-free); the spike checklist below is unchanged.

## Problem

The serving script (or test harness) runs an ephemeral-port HTTP rehost of
the entity store on the **host** (`design/05` §6, part5 note 07). Fetch FODs
inside `nix build` must reach it via `FXTR_REMOTE_BASE_URL`. Two backends
(`projects/fxtr_nix_runner/`):

- **host backend** — nix runs on the host. FODs get network access (that's
  the FOD exception to the build sandbox) in the host network namespace, so
  `http://127.0.0.1:<port>` works as-is. On macOS the nix sandbox profile can
  restrict network for non-FODs, but FODs are exempt — expected to Just Work;
  verify once.
- **podman backend** — nix runs inside a per-repo builder container
  (`_podman.py`), typically inside a podman **machine** VM on macOS. A
  host-`127.0.0.1` URL is meaningless there. Three hops to consider:
  FOD → builder container netns → podman machine VM → host.

Two sub-problems: (a) an address the FOD can dial that lands on the host
rehost; (b) getting the env vars into the build. (b) is mostly solved:
`run_nix` gives us argv control, and FODs read `impureEnvVars` (declared on
the derivation) which we can pass via `podman exec -e` + nix config
(`impure-env` setting requires the `configurable-impure-env` experimental
feature; alternatively bake `FXTR_REMOTE_*` into the builder's
`/etc/nix/nix.conf` at container start, which `_podman.py` already writes).

## Options for (a), podman backend

1. **`host.containers.internal` / `host-gateway`.** Podman ≥4 injects
   `host.containers.internal` into containers' `/etc/hosts`; with podman
   machine it is supposed to resolve to the actual macOS host (the VM
   forwards). The rehost must then bind a non-loopback interface (or
   `0.0.0.0`) — bearer token becomes the only access control, so generate a
   fresh `secrets.token_hex` per build (already the note-07 Q7.B decision).
   Cheapest if it works; needs a verification spike on our podman-machine
   setup (gvproxy vs pasta networking differ here).

2. **Unix-socket bind-mount + in-container forwarder.** Host serves the
   rehost on a unix socket under the existing scratch mount; the builder
   container runs a tiny TCP→unix forwarder (socat or a python one-liner)
   on `127.0.0.1:<port>` inside the container; FODs dial container-local
   localhost. No interface exposure at all, works regardless of podman
   networking mode. Caveat: **unix sockets do not cross the podman-machine
   VM boundary via virtiofs mounts** on macOS — this option works for
   rootful-linux podman but likely not for podman machine; verify.

3. **Reverse tunnel via `podman exec`.** Start a long-lived
   `podman exec` running a forwarder that connects back over the exec
   stdio stream (à la the stage-8 inetd-mode ssh trick for the remote-podman
   socket). Most plumbing, fully transport-agnostic; keep as fallback.

4. **Port-forward into the VM with `podman machine ssh -R`.** Reverse-forward
   the host rehost port into the VM, then containers reach it via the VM
   gateway address. Moderate plumbing; depends on machine ssh availability.

_Daniel note:_ Current nix runner uses a sidecar container to bridge in sockets,
this is a pattern we could lean on if we need to.

Recommendation to verify first: option 1, with option 2 as the
linux-rootful fast path and option 3 as the universal fallback. Whatever
wins should land in `fxtr_nix_runner` as a backend-neutral handle API,
roughly:

```
async with h.expose_host_endpoint(host_port) as ep:   # name TBD
    # ep.url_for_builds  -> the FXTR_REMOTE_BASE_URL value valid inside builds
    # host backend: identity (http://127.0.0.1:<port>)
    # podman backend: whichever option won
```

plus a `run_nix`/builder hook for delivering the env pair to FODs.

## Interaction with purity

FODs may use the network *and* impure env vars they declare; the output hash
pins the result, so the final flake stays pure (the OPEN-3 hard constraint).
The rehost URL/token being per-run is fine — they're declared
`impureEnvVars`, not part of the derivation hash. One sharp edge: a FOD whose
fetched bytes drift (remote store contents changed for the same id set)
fails its hash check — which is the correct behavior for content-addressed
data (ids pin content), so drift should only ever mean "the FOD's input id
set changed" → re-lock (see `followups.md`, FOD lock file).

## Spike results (2026-06-13 — RESOLVED)

Spiked on a macOS arm64 box with **Determinate Nix 2.34.6 (multi-user
daemon)** + **podman 5.8.2 (applehv machine)** — the live setup. Scratch in
`/Users/daniel/extgit/spikes/2026-06-13-nix-rehost/` (tiny bearer-token HTTP
server + a `runCommand` FOD that curls it). **Option 1 wins cleanly on both
backends; no reverse tunnel / sidecar bridge needed.** Findings:

**Networking — both backends reach the host rehost from inside a FOD build.**
- Host: a FOD build fetched the payload from `127.0.0.1:<port>` ✓ (FODs are
  network-exempt; this host also has `sandbox = false`, but that's incidental).
  > **Correction (2026-06-14, from part 1's E2E):** this `127.0.0.1` result
  > holds only when the build runs **locally** (the spike's FOD was a
  > native/darwin build). When host nix **offloads** — notably macOS building a
  > *linux* derivation via a remote builder VM (`ssh-ng://…`) — the remote
  > builder can't reach the mac host's loopback rehost, so `127.0.0.1` fails.
  > The host backend's `expose_host_endpoint → 127.0.0.1` therefore only works
  > for local builds (e.g. linux CI); on macOS use the **podman** backend
  > (`host.containers.internal`, validated E2E). `test_nix_e2e` skips the
  > FOD-fetch test on the darwin host backend. Revisit if the host backend ever
  > needs FOD reachability under a remote builder (would need a gateway address,
  > not loopback — the same class of problem podman solves via gvproxy).
- Podman: a FOD build *inside the builder container* (sandbox=true,
  multi-user daemon) fetched it from `http://host.containers.internal:<port>`
  ✓. `host.containers.internal` resolves to the gvproxy gateway
  (`192.168.127.254`) and routes to the host. Both backends produced the
  **same** output hash → end-to-end correct.

**The rehost must bind `0.0.0.0` for podman** (a `127.0.0.1`-bound server is
unreachable from the VM/container). The bearer token is then the only access
control — generate a fresh `secrets.token_hex` per run (already the plan).
Host backend can stay `127.0.0.1`. So the manager binds per backend.

**Env delivery — the real subtlety, and it splits by backend.** Both backends
**confirmed end-to-end** (identical output hash).
- Under a **multi-user daemon**, `impureEnvVars` are read from the *daemon's*
  env, **not** the client's — passing them on the client command line does
  nothing (confirmed: empty inside the build). The dynamic channel is the
  **`impure-env` setting** (`--option impure-env "K=V …"`), which has **two
  daemon-side prerequisites**:
  1. the invoking user must be a **trusted user** (else `impure-env` is a
     "restricted setting" and silently ignored — with a warning); and
  2. **`configurable-impure-env` must be in the *daemon's*
     `experimental-features`** — enabling it only as a client flag does
     **not** propagate; the daemon silently drops `impure-env` without it (no
     warning, just an empty var — this was the subtle failure mode we hit).
  Both are **static daemon config**, then `--option impure-env` per build
  works.
- **Podman backend: solved, because we own the builder daemon** — it runs as
  **root** (trusted) and we write its `nix.conf`, so both prerequisites are
  ours to set (we already write `experimental-features = … configurable-impure-env`
  + sandbox config). Verified end-to-end. **Caveat:** the builder needs
  **`filter-syscalls = false`** under Apple-silicon emulation or the
  sandboxed build dies with `unable to load seccomp BPF program: Invalid
  argument` — this is exactly the "arch-resolved `filter-syscalls`" the
  backend already sets; just make sure it's in effect for the build, not only
  `skopeo`.
- **Host backend, multi-user box: requires both daemon settings above**
  (trusted-user **and** `configurable-impure-env` daemon-side). Verified
  end-to-end on Determinate Nix after setting both + restarting the daemon
  (`launchctl kickstart -k system/systems.determinate.nix-daemon`). A
  documented host prerequisite (heavier than I first thought — it's two
  settings, not one). On **single-user** nix (typical Linux CI), client-env
  `impureEnvVars` work directly — no trust, no feature flag.
- `podman exec -e` is *not* the delivery path (it sets the *daemon-launch*
  process env, not per-build env); `--option impure-env` per build is, and it
  keeps flake **eval pure** (env read at build time, not eval — unlike
  `builtins.getEnv`, which would violate the purity constraint).

**Recommended implementation (the `Handle` hooks from part 1):**
- `expose_host_endpoint`: host → identity `http://127.0.0.1:<port>`; podman →
  `http://host.containers.internal:<port>` (rehost bound `0.0.0.0`).
- extra-build-env: deliver `FXTR_REMOTE_*` via `--option impure-env "…"` on
  the `run_nix` build invocation (+ `--extra-experimental-features
  configurable-impure-env`). Per-build, so it composes with the per-run token.
- Ensure `filter-syscalls = false` is active for podman builds under emulation.
- **Env can be delivered per-build** (not per-builder) on both backends — so
  the part-1 TODO "per-build vs per-builder env" resolves to **per-build**,
  and the API can sit on `run_nix`/the build call rather than `builder_for`.

## Verification spike checklist

- [x] host backend: FOD curl→`127.0.0.1` rehost — **works** (macOS; FOD
      network exemption). Linux single-user expected-trivial (client env).
- [x] podman: `host.containers.internal` resolves+routes from the builder
      container **and** from a FOD build sandbox inside it — **works**
      (rehost bound `0.0.0.0`, gvproxy gateway).
- [x] env delivery: `--option impure-env` per build is the channel —
      confirmed end-to-end on **both** backends. Multi-user daemon needs TWO
      daemon-side settings: trusted-user **and** `configurable-impure-env` in
      the daemon's `experimental-features` (the latter doesn't propagate from
      a client flag). Podman owns both (root + our `nix.conf`); host needs
      both set + a daemon restart. Podman also needs `filter-syscalls = false`
      under emulation.
- [ ] rootful-linux podman: unix-socket + socat forwarder — **not needed**
      (option 1 covers podman-machine); only revisit if a no-`host.containers.internal`
      environment shows up.
