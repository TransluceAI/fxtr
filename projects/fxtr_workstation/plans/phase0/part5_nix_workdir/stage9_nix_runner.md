# Stage 9 — `fxtr_nix_runner`: nix invocations inside podman

Make **every fxtr nix invocation** — workstation MCP entity builds,
spawner service builds, dispatcher preset builds — run inside a podman
container with nix in it, instead of host nix against the real
`/nix/store`. Someone setting up the fxtr workstation system then needs
**neither nix nor skopeo on the host**, and no remote-builder
configuration: the builder container is `x86_64-linux`, so
canonicalSystem builds are *native* inside it (on Apple Silicon the
podman machine runs amd64 under Rosetta at near-native speed since
the macOS 26 upgrade — see chunk 1f; qemu-user on older macOS). Host nix remains a fully supported backend —
including the host-nix + Apple Container combo via stage 8's
(`projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage8_podman.md`)
supported non-default runtime — selected by a switch; podman-nix
becomes the recommendation and (as the final chunk, once proven) the
default.

Builds on stage 8: tarless socket delivery and the podman-default
runtime land there; this stage relocates *where nix and skopeo run*.

Design discussion settled 2026-06-04 (full decision log:
`stage9_nix_runner_scratch.md`):

- **Why this works at all:** fxtr only uses nix to *build files* that
  are then read by the host (wasm `sitePackages` mounted into eryx,
  entity layer dirs, renderer `.mjs` bytes) or streamed into the
  container runtime (nix2container images, delivered by skopeo from
  *inside* the builder container). We never execute built artifacts via
  nix on the host — the one exception, skopeo, moves inside.
- **One builder container per workdir/repo**, all sharing a single
  named `/nix` volume. Containers can't gain mounts after start, and
  the only host inputs nix needs are the git repos/workdirs — so the
  repo *is* the mount, RO at a canonical path. Direct sync (git
  bundles / push into the container) was considered and rejected: it
  works for the frozen CODE path but the MCP DATA path is deliberately
  **non-frozen against the dirty working tree**, which needs the live
  tree visible.
- **`/nix` lives in a named podman volume** (fast, native fs — not a
  virtiofs host mount, which risks nix-sqlite-on-network-fs problems),
  and consumed outputs are **materialized**: closure-copied into a
  host-visible scratch dir.
- **Layer dirs are not self-contained** — `parents.json` carries
  absolute store paths to parent layer dirs. Rather than have the
  copy-out step reimplement layer-format parsing to rewrite paths, the
  **v2 layered-store reader gains a parent-path remap hook**; the
  closure copy stays verbatim/dumb.
- **New project `projects/fxtr_nix_runner/`** owns all of it: the
  host-vs-podman backend switch, builder-container lifecycle, ref/path
  translation, materialization, and in-container skopeo delivery. It is
  the shared *backend* the three nix wrappers call through — a layer
  *beneath* them. The wrappers keep their own ref-construction and
  error conventions; the backend is transport, not policy.

## Security posture (the default-path decision)

This stage moves *builds* of potentially untrusted code (agent-written
flakes, which can pull in others' code via references) off host nix and
into podman. That's a security-sensitive move, and it drives whether
podman-nix can be the **default** or only a non-default **option**.

**Desideratum (v0):** *no additional attack surface during build beyond
what a sandbox-enabled nix remote builder (today's virby) already
allows.* We measure parity against virby, not against bare-metal nix.

**First line vs second line.** The load-bearing idea: untrusted build
code runs *inside nix's build sandbox*, so it never sees the build
**container's** capabilities unless it first escapes that sandbox.

- *First line* = nix's own `sandbox = true`: per-build private network
  namespace (normal derivations get **no** network; only fixed-output,
  hash-pinned derivations fetch — exactly virby's rule) + a
  **read-only** `/nix/store` view (the build can't poison other store
  paths; the trusted nix process moves outputs in from outside). This
  is the line that must match virby.
- *Second line* = the container boundary (caps, namespaces, no API
  socket). Only relevant *after* a first-line escape.

Because the caps are second-line, **granting the build container more
privilege is acceptable if and only if it's what lets `sandbox = true`
work** — at that point the untrusted code is isolated to the same first
-line degree as under virby, and we've only widened the *second* line.

**What `sandbox = false` costs (why we won't ship it as the parity
story).** Without the sandbox: every build gets the container's full
network (free exfiltration + unpinned fetch-and-run — fails the
desideratum on the network axis), and single-user nix runs the build as
root against the shared RW `/nix` volume, so it can **overwrite any
store path undetected** (store paths are input-addressed; nix doesn't
re-hash on read). That last one is a real **build-container → host**
vector, because materialized outputs are consumed host-side: a poisoned
`.mjs` runs in the host renderer, poisoned `sitePackages` are mounted
into eryx and executed, poisoned entity layers are read by the v2
reader. The builder/deliverer split (below) does **not** close this —
the poisoning target is the host-consumed output, not the deliverer.

**The decision ladder (resolved by spike item g).** Report the *minimum
privilege rung* at which `sandbox = true` builds actually succeed
in-container (nested user namespaces in the machine VM are the fiddly
unknown; nix's sandbox wants namespace+mount setup that normally wants
`CAP_SYS_ADMIN` *within* the build's userns):

1. **Works with no extra caps** → all-podman can be the **default**;
   second line is strong (container boundary fully intact).
2. **Works only with some caps / `--privileged`** → shippable as a
   first-line-parity **option**; second line degrades (a `--privileged`
   container ≈ root on the machine-VM kernel, so an escape-from-nix
   reaches the socket/images/`/Users` mount). Whether it's *also* the
   default is a judgment call on that erosion.
3. **Doesn't work even `--privileged`** → **host backend stays the
   default** (nix-outside-podman); the podman backend ships
   trusted-builds-only, documented as the lower-trust option.

In all three cases we likely still **ship** the podman backend; the
ladder decides *default vs option* and *what trust caveat we document*.

**Spike result (2026-06-06, item g — `sandbox_gate_test.py` +
`arch_check.py`; full grid in the spike's `NOTES_STAGE9.md`):** the
sandbox **engages with full virby first-line parity** (normal-build
network denied; FOD fetch allowed; real `/nix` store stays clean — the
sandboxed sibling-write is discarded, verified by a persistence check),
and the **minimum container privilege is `--cap-add SYS_ADMIN
--security-opt label=disable --security-opt unmask=/proc/*`** — tighter
than full `--privileged` (keeps default seccomp + minimal device set +
most cap drops, and keeps `/sys/firmware` masked). Decomposed, all three
are load-bearing: **`unmask=/proc/*`** unlocks namespace creation
(masked `/proc` blocks the fresh-`/proc` mount), **`label=disable`**
clears SELinux (enforcing FCOS machine-os) blocking the ns/mount ops,
**`SYS_ADMIN`** does sethostname + in-sandbox mounts. `/proc` must be
unmasked *whole* (partial unmask of only the read-only paths fails — the
kernel won't mount over `/proc` while locked masked-with-null submounts
remain), so `/proc/kcore` + `/proc/sysrq-trigger` stay exposed — but
**second-line only**: the sandboxed build gets its own fresh `/proc`, so
only a sandbox *escapee* sees the container's. This is
**outcome 2 at a moderate (not heaviest) rung**: second-line erosion is
real but bounded, and the builder/deliverer split still forces a
container escape. Impl-critical riders: (1) **must set
`sandbox-fallback = false`** — the default silently runs builds
*unsandboxed* when setup fails (the spike caught exactly this: "passing"
rungs had poisoned the real store); (2) **`filter-syscalls` is
native-arch-only** — it can't load under amd64/Rosetta at any rung
(`seccomp BPF … Invalid argument`, an emulation artifact) but **loads on
native arm64**, so run it on for native builders, off for emulated.
**Rootful buys nothing** (re-ran the full ladder rootful: identical —
the blocker is the container default config, not the rootless userns
remap; the VM allows userns, `max_user_namespaces=59732`), so **stay
rootless**. All measured on macOS-VM; a native Linux host is a separate,
likely-easier regime — `scope default-vs-option by host` stands as the
working assumption (host-nix default on macOS until the moderate-rung
posture is judged acceptable; podman plausibly default on Linux).

**Rules that hold regardless of the rung:**

- **Builder/deliverer split.** The container running untrusted builds
  holds the `/nix` volume + repo (RO) + its own output scratch and
  **nothing else** — no API socket, no extra mounts. A separate
  *trusted* deliverer container holds the socket (or a filtered socket
  proxy, see below) and **execs skopeo but runs no nix build itself**.
  *Provenance is the boundary, not packaging:* a malicious flake could
  pin a trojaned skopeo that does anything once it has the socket, so
  the executed skopeo must come from **fxtr's own pinned `nix2container`
  rev — never the skopeo the untrusted flake resolves**
  (`fxtr_sandboxing/nix.py`'s `build_skopeo` already pins that rev — the
  podman backend carries it forward). *Build ≠ run:* skopeo is **built
  in the builder** (`sandbox = true`, which protects its store-path
  integrity from untrusted sibling builds, and the builder is already
  running natively) and only **exec'd in the deliverer**, which reaches
  the pre-built binary + the image.json closure via the shared `/nix`
  volume. The socket-holding container therefore never runs a build at
  all. (Baking skopeo into a separate deliverer *image* is **optional
  defense-in-depth, not required** — provenance, not packaging, is the
  boundary.)
- **Multi-user nix-daemon is hardening, not the bar.** virby's real
  config is multi-user daemon + build-user separation + sandbox. First
  -line parity needs only `sandbox = true` (even single-user); the
  daemon/`nixbld`-user separation additionally contains a *post-escape*
  build (build runs as non-root, so a sandbox escapee is a non-root user
  in the builder rather than container-root). Given the builder already
  holds no socket + a RO repo, this is **second-line defense-in-depth,
  not parity-critical**. **Resolved by the Chunk-1.5 spike (GATE PASS):
  multi-user + sandbox engages at the *same* moderate rung — no extra
  privilege — so multi-user is the default builder config.** One rider:
  the daemon crashes on the old `2.24.9` pin (a Rosetta `monitor-fd`
  artifact), so the builder image must be pinned to **≥ nix 2.34.7**
  (see Chunk 1.5). Single-user remains a documented fallback / config
  knob.

**Honest, irreducible caveat (co-tenancy).** Inside the machine VM the
build shares **one kernel** with the podman service, the deliverer, and
other containers; virby is a dedicated VM kernel shared with nothing. So
a *kernel*-level exploit from inside the nix sandbox reaches more here
than under virby. This is deep second-line (kernel-0day tier), doesn't
break first-line parity, and **can't be closed by tuning caps** — it's
inherent to running builds as containers rather than as a separate VM.
Document it; accept it for the podman backend.

**Socket least-privilege (the deliverer's capability).** Even the
trusted deliverer's API socket is all-or-nothing in podman (full API ⇒
privileged containers, bind-mount any VM path incl. the `/Users`
virtiofs share, exec into other containers). For the untrusted-build
posture, the natural upgrade is a **filtering socket proxy** — a small
trusted container holding the real socket and exposing, on a shared
volume, only the endpoints skopeo's `docker-daemon:` transport needs
(ping/version + image load). Same sidecar shape stage 8 already uses;
the skopeo invocation is unchanged (just a different socket path). v0
may use the raw socket on the deliverer; the proxy is the documented
path to shrink the deliverer's own capability. (Prior art:
docker-socket-proxy.)

**Rejected fallback (noted, not spiked): build container as the netns.**
Run the untrusted build with `--network none` and pre-fetch flake
inputs + FODs via a separate trusted networked step. This would recover
per-build network denial without nix's sandbox — but it **breaks
fixed-output derivations / `builtins.fetch*` at build time**, which we
want to keep working, and it still leaves the RW-store poisoning vector
open (sandbox=false). Considered non-workable for our needs; mentioned
for completeness, not a spike item.

(Note on eval: agent-written flake code **cannot** execute arbitrary
host code at eval time by default — Nix is pure-functional with no FFI;
the escape hatches (`builtins.exec`, plugins) are off by default and
flakes' pure eval keeps them off. Eval can read files in allowed paths,
fetch hash-pinned data, and trigger builds (IFD) — the last being build
code, governed by the sandbox above. So the surface that matters is the
**build**, not eval.)

## The backend interface

`fxtr_nix_runner` exposes one backend object (resolved from
`FXTR_NIX_BACKEND=host|podman` + the config file). Per-repo work goes
through a **refcounted async context manager**, `backend.builder_for(repo)`,
which yields a **handle**; the operations below are **methods on that
handle** (so they all run against the repo's already-ensured builder
container + its lazy deliverer, and lifecycle stays structured — see
Lifecycle). The **host** backend's `builder_for` is a trivial no-op CM
whose handle just shells out to host nix. Names indicative:

```
backend.builder_for(repo: Path) -> async CM yielding a Handle
    `repo` is BOTH the keying basis (builder keyed/mounted on its
    git-repo-root) AND the ref target the yielded handle is bound to.
    podman: refcounted ensure/teardown of the repo's builder container
    (lazy `run -d`; the last context to exit stops it). host: no-op CM.
    Ownership is explicit (NO process-global singleton): a long-lived
    consumer (the MCP server) holds one backend + reuses it so concurrent
    same-repo borrows refcount-share a builder; a one-shot caller resolves
    per operation.

handle.flake_ref_root() -> str
    The path string to embed in a flake ref — for the path the builder was
    scoped to (the `repo` passed to builder_for). No argument: the handle
    remembers its target. host: the resolved target path. podman: the
    in-container mount path — `/workdir` for the repo root, or
    `/workdir/<relpath>` when the target is a subdir of the repo root.

handle.run_nix(argv) -> ProcessResult
    host: anyio.run_process(["nix", *argv]).
    podman: podman exec <builder> nix <argv> (multi-user: with
    NIX_REMOTE=daemon). Non-raising (returncode/stdout/stderr) — each
    wrapper keeps its own error types and raise logic (NixError vs
    RuntimeError).

handle.lock() -> None
    Create/refresh the workdir's flake.lock (`nix flake lock`), written
    back to the host workdir. host: in place. podman: in the builder,
    whose /workdir is mounted read-write (the only write surface the
    builder gets — builds run sandboxed off a store copy). Needs the
    flake's inputs reachable (network + `mountable_paths`).

handle.materialize(store_path, *, closure: bool) -> Materialized
    host: identity (the store path is already host-readable).
    podman: runs IN THE BUILDER (it already holds the source store +
    the bind-mounted dest) — `podman exec <builder> nix copy --to
    "local?root=<materialized-root>"` the path (closure-aware +
    dedup'ing) into the persistent host bind-mount store; return the
    host path (`<root>/nix/store/<base>`) + the
    `/nix/store`→`<root>/nix/store` prefix remap for the reader hook.
    (See Materialization below — spike-validated, incl. concurrent
    copies + sqlite-on-virtiofs.)

handle.deliver_image(image_json_store_path, *, runtime) -> DeliveredImage
    Load a nix2container image manifest into `runtime`'s image store and
    return `DeliveredImage(image_ref, platform)` — the ref + `--platform`
    the spawner needs to *run* it (the spawner never reads the store; the
    nix-runner is the side that can — see "Abstraction boundary"). The
    backend builds + runs the fxtr-pinned skopeo itself (`nix2container`
    rev — **never the user's flake**) and reads the manifest's `arch` for
    `platform` (no `linux/amd64` assumption). Delivery by (backend ×
    runtime):
      host  → podman: host skopeo → podman API socket (tarless stream).
      host  → other (Apple Container / docker): skopeo → oci-archive +
              `<runtime> image load` (transient tar).
      podman → podman: the *deliverer* container (lazy; shares the
              builder's lifetime — see Lifecycle) execs the pre-built
              skopeo against the mounted VM-side socket; layers stream
              from the shared `/nix` volume. The socket-holding container
              never runs an untrusted build (builder/deliverer split).
      podman → other: **unsupported → raises** (the podman backend
              requires the podman service runtime; v0 doesn't bridge a
              podman-built image into a non-podman runtime).
```

## Abstraction boundary (nix-store side vs runtime side)

`fxtr_nix_runner` owns the **nix-store side**: building, and *exporting
build outputs to where they're consumed*. On the podman backend nix writes
into the `/nix` **volume**, so outputs aren't on the host fs — only a
process with the volume mounted can read them. Two sibling export ops:
`materialize` (store path → host-readable **file path**) and
`deliver_image` (manifest store path → loaded into a runtime's image store,
returning **ref + platform**). Everything store-side lives here: the
fxtr-pinned skopeo, the delivery socket discovery, the image-ref convention
(derived from the store-path hash), and reading the manifest's platform.

`fxtr_sandboxing` owns the **runtime side**: selecting the runtime
(`FXTR_CONTAINER_RUNTIME`/`get_runtime`), **running** the delivered ref
(sockets, sidecar tunnel, transport), and the run-path `podman_is_remote`
check. It passes the runtime *into* `deliver_image` and gets back exactly
what it needs to run — it never touches `image.json`.

Runtime coupling: the podman backend **requires** the service-container
runtime to be podman (delivery lands in podman's image store; there is
nothing to choose). `FXTR_NIX_BACKEND=host` keeps stage 8's runtime
flexibility.

## Configuration

Backend selection + the override knobs come from these sources:

- **`FXTR_NIX_RUNNER_CONFIG`** (env) — path to a complete config file. When
  set, *that file is the entire config* — it supersedes both the default
  location and `FXTR_NIX_BACKEND`. This is the hermetic test/CI override (a
  temp toml), and the only way to set `mountable_paths` for a test run.
- **`FXTR_NIX_BACKEND=host|podman`** (env) — the lightweight "just flip the
  backend" selector; **env wins** over the default-location file (ignored
  when `FXTR_NIX_RUNNER_CONFIG` is set).
- **`~/.config/fxtr/nix_runner.toml`** (XDG; optional) — the default override
  file. Secure defaults are baked in code; the file overrides them only where
  a user genuinely must. **File wins** for everything *except* the backend
  selector (where `FXTR_NIX_BACKEND` wins). Minimal v0 schema, all keys
  optional:

  ```toml
  backend = "host"            # overridden by FXTR_NIX_BACKEND if set
  [podman]
  image = "docker.io/nixos/nix:<version>"   # the builder/deliverer pin (>=2.34.7 for multi_user)
  sandbox = true                            # escape hatch; default true
  multi_user = true                         # nix-daemon + nixbld build users; default true
  filter_syscalls = "auto"                  # "auto" | true | false
  mountable_paths = []                      # host dirs bind-mounted RO into the builder (abspath)
  ```

  `mountable_paths` are host directories the builder mounts **read-only at
  their original abspaths** so a flake input referencing a local path under
  one of them (e.g. a local lib checkout) resolves in-container. Whitelist:
  only listed dirs are mountable (no lockfile-driven mounts); a missing dir is
  skipped. An unreachable input surfaces as nix's own error naming the path.

  `multi_user` (default `true`, Chunk-1.5 result) selects the daemon +
  `nixbld` build-user posture; setting it `false` falls back to the
  single-user (container-root) builder. Note the coupling: the
  `>=2.34.7` image floor is a **multi-user** requirement (older daemons
  hit the Rosetta `monitor-fd` crash) — single-user has no such floor,
  so `multi_user = false` is also the escape hatch for anyone who must
  pin an older nix.

  Deferred (not v0): `sandbox_fallback` exposure and `extra_run_args`
  (custom caps/security-opts) — add as real needs land.

## Builder + deliverer containers

### Image + sandbox config

- **Image:** pinned `docker.io/nixos/nix:<version>`, **≥ 2.34.7**
  (older daemons — e.g. the 2.24.9 spike pin — crash under Rosetta with
  the `monitor-fd` bug in multi-user mode; see Chunk 1.5), always
  `--platform linux/amd64` (canonicalSystem). Flakes enabled via
  `NIX_CONFIG`. Same image for builder and deliverer.
- **Sandbox: `sandbox = true`, always** (resolved by gate spike (g);
  supersedes the earlier `sandbox = false` "nix-in-container norm"
  assumption — see Security posture). The builder runs at the
  spike-measured **moderate rung**: `--cap-add SYS_ADMIN --security-opt
  label=disable --security-opt unmask=/proc/*`, rootless. Mandatory
  riders, set via `NIX_CONFIG`:
  - **`sandbox-fallback = false`** — a failed sandbox setup must be a
    hard error, never a silent unsandboxed build.
  - **`filter-syscalls`**: default by arch detection (config-overridable,
    see Configuration). The builder is always `linux/amd64`, so we query
    the podman machine's *native* arch (`podman info --format
    '{{.Host.Arch}}'`) and set `filter-syscalls = false` when the build
    is emulated (native arch ≠ amd64 — i.e. arm64/Darwin, where it can't
    load under Rosetta) and `true` when native.
  - **Multi-user (`build-users-group = nixbld` + a running
    `nix-daemon`)** is the **default** (Chunk-1.5 spike: PASS at the same
    rung, builds run non-root). The builder bootstrap creates the
    `nixbld` group + `nixbld1..N` users (direct `/etc/passwd`+`/etc/group`
    edits — no `shadow` tools in the image), launches `nix-daemon`, and
    execs `nix` with `NIX_REMOTE=daemon`. Single-user stays a documented
    fallback / config knob.

### Lifecycle (refcounted, structured-concurrency)

- **One builder per git repo root per Python process**, tracked in
  process-global state and handed out through an **async context
  manager** (`async with backend.builder_for(repo) as h: …`),
  **refcounted**: the first entrant lazily `podman run -d … sleep
  infinity`s the container; the last context to exit tears it down. This
  shares the builder across all in-process consumers (one workstation
  server, etc.) while leaving the system clean on exit — no idle
  container sprawl across runs. Keyed on the **git repo root** (walking
  up from the workdir, matching nix's bare-path auto-git resolution) or
  the workdir itself for the no-repo `path:` fallback.
- **Deliverer: lazy, paired to the builder's context.** Most repos never
  deliver a container image, so the deliverer is **not** started with
  the builder — it spins up on the **first `deliver_image`** for that
  repo and tears down with the builder's context. Same image as the
  builder, run with the API socket + `--security-opt label=disable`,
  **no repo mount, no gate caps**, and it runs **no nix build at all**
  (skopeo is built in the *builder*; see Security posture) — its only
  job is "hold socket, exec the pre-built skopeo."
- **`clear` CLI demoted to crash-debris recovery.** Normal teardown is
  the context manager; the CLI (`python -m fxtr_nix_runner clear`) exists
  for orphans left by a crashed process — label-query
  (`fxtr-nix-builder=<repo hash>`, `fxtr-nix-deliverer=…`) stop+rm, plus
  `podman volume rm` of the store + eval-cache volumes; image-cache
  pruning stays the stage-8 helper.

### Mounts + volumes

- **Builder mounts:** repo RO at a canonical path (e.g. `/workdir`); the
  shared `/nix` named volume (`fxtr-nix`); the eval-cache volume
  (`fxtr-nix-cache`, below); the materialize scratch dir RW. **No API
  socket** (builder/deliverer split).
- **Deliverer mounts:** the `/nix` named volume (to reach the built
  `image.json` closure + the pre-built skopeo) + the API socket. No
  repo, no scratch.
- **`/nix` named volume (`fxtr-nix`)** is auto-populated from the image
  on first mount (podman named-volume copy-up), which is how the nix
  binaries survive being mounted over. Consequences: the volume is
  initialized by whichever pinned image touches it first; bumping the
  image pin does not refresh an existing volume (document; cache-clear
  re-initializes).
- **Eval-cache volume (`fxtr-nix-cache`)** — a *second* named volume
  mounted at e.g. `/fxtr-cache` with `XDG_CACHE_HOME=/fxtr-cache`, so the
  flake eval cache is **shared across builder containers** (per-repo
  containers otherwise get cold per-container eval caches) while keeping
  `/nix` pristine. Same fast native-fs as the store (not virtiofs); nix's
  own locking coordinates concurrent eval-cache sqlite access (the same
  property the store-concurrency spike validated). A second named volume
  is negligible overhead (just a directory under the VM storage root, no
  preallocation). Cleared alongside the store by the `clear` CLI.
- **Concurrency:** multiple builder containers (across processes) share
  the volumes; nix's file locks coordinate (same kernel) — spike-verified.

## Materialization + the v2 reader remap hook

- **Materialize via `nix copy` into a persistent host bind-mount store**
  (spike-validated, `copy_test.py`): a second nix store rooted at a
  host-visible dir — `nix copy --no-check-sigs --to
  "local?root=<materialized-root>" <path>`, run inside the container with
  `<materialized-root>` bind-mounted. This is closure-aware (arbitrary
  depth), **deduplicating** (skips paths already valid in the dest — the
  shared parent layers across entities transfer once), GC-able
  (`nix store gc`/`optimise` against it, roots = live entities), and
  host-readable directly (`<root>/nix/store/<hash>-name/...`; the host
  needs no nix). Chosen over manual `cp`/`podman cp`/tar-pipe: same
  closure semantics, but with real dedup + GC + hash-verified copies.
  The dest carries a sqlite db on the (virtiofs) bind-mount — the
  "nix-sqlite-on-network-fs" risk the build store avoids — but the spike
  confirmed it works, **including concurrent `nix copy` from multiple
  containers to the same dest** (nix's store lock coordinates; db stayed
  intact). Default root e.g. `~/.cache/fxtr_nix_runner/store/`
  (configurable). (Not stress-tested at high builder/closure scale —
  revisit if it bites.)
- **v2 change (small):** the layered-file-store **reader** accepts an
  optional parent-path remap — a **prefix-pair** (`/nix/store` →
  `<materialized-root>/nix/store`; the internal shape leaves room for a
  callable later, but prefix-pair is what we implement) applied where
  `parents.json` entries are canonicalized (in `search.py`'s parent
  expansion — between `read_parents` and `_add_layer`). Only parents
  carry paths (`pins.json` is FxtrIDs), so the hook is scoped to parent
  resolution and is **read-side only — the layer writer is untouched**
  (nothing writes layers *from* materialized parents). MCP + dispatcher
  thread the remap from `Materialized` when the podman backend is active.
- What materializes what: entity layers + preset `typeEntity` layers,
  wasm `sitePackages`, renderer `.mjs` → all via `nix copy` (it always
  copies the path's closure, so the layer-parents case and the
  single-artifact cases use the same call); container images →
  **never** (delivered inside podman). (Symlink fidelity: `nix copy`
  preserves store-path structure; the v2 reader resolves within the
  materialized root via the prefix remap.)

## Wrapper adoption

**Handle-passing (decided 2026-06-07).** The wrappers are independent
functions a consumer calls in sequence, so having each open its *own*
`builder_for` would churn the podman builder (create/teardown per nix
call). Instead the **consumer** opens `async with backend.builder_for(repo)
as h:` once around its whole sequence and **passes `h`** to the wrapper
functions. The wrappers therefore **drop their `repo` argument** (the only
use of it was the ref root, now `h.flake_ref_root()`) and keep just the
non-path ref components (`commit` / `attr` / `flake_path`). They swap
`anyio.run_process(["nix", …])` for `h.run_nix(...)` + `h.flake_ref_root()`
(plus `h.materialize(...)` / `h.deliver_image(...)` where they apply),
keeping their own error conventions. Backend ownership is the consumer's
(no process-global; a server holds one, a one-shot caller resolves per op).

- `fxtr_sandboxing/nix.py` — spawner; also `build_skopeo` becomes
  backend-aware (host backend keeps the pinned host-system build; the
  podman backend builds it in-store, in-container).
- `fxtr_workstation_mcp/nix_util.py` — MCP; the bare-workdir
  non-frozen semantics carry over unchanged because the working tree
  is mounted live (uncommitted tracked edits visible; the untracked-
  files notice stays host-side `git`, untouched).
- `nix_configurable_dispatch/nix.py` — dispatcher; drvPaths from the
  eval round-trip into the same backend for the batched build
  (in-container store paths are opaque strings to the host — fine).

**Materialization is internal to the wrappers.** The wrappers are
internal helpers, not an exposed API — they may be rewritten freely so
long as observable behavior is unchanged. On the podman backend each
wrapper calls `backend.materialize(...)` itself and keeps returning
**host-readable paths exactly as today** (identity on the host backend,
so callers are unaffected on either backend). The **only** new thing a
consumer sees is, for **layer-producing** calls (MCP
`retrieve_nix_entity`, dispatcher preset `typeEntity` layers), an extra
**remap handle** threaded into the v2 reader (see the reader hook).
Self-contained outputs (wasm `sitePackages`, renderer `.mjs` — no
parents) come back as plain host paths with no remap.

`fxtr_nix_runner` stays a lightweight dep (anyio; no fxtr2) so all
three can take it.

## Stage-8 carry-overs (2026-06-06, post-stage-8 implementation)

Landed code this stage should build on rather than duplicate:

- **`fxtr_sandboxing/_podman.py`** — fxtr2-free (anyio only, the same
  dep posture `fxtr_nix_runner` wants): host-side API-socket
  discovery (`resolve_podman_socket`), remote detection
  (`podman_is_remote`), the content-hash-tagged ensure-image idiom
  (now with a cross-process build flock — same-host concurrent
  cold-store builds serialize to one build — and a baked
  `fxtr-role=<role>` image label so `clear-images` can sweep dangling
  build debris by label; both 2026-06-06), and the label +
  debris-sweep pattern. Chunks 3–4 should lift/share these — likely
  by moving them into `fxtr_nix_runner` and having `fxtr_sandboxing`
  depend on it (dependency points that way anyway). Builder-container
  ensure (lazy `run -d`) should reuse the flock idiom — same
  parallel-spawn race shape as the sidecar image build.
- **`deliver_image`'s host arm is now literal**:
  `nix_container_runner._load_image_into_runtime` as landed (tarless
  podman branch + tar fallback), verified E2E on both branches.
- **Label convention**: stage 8 tags per-spawn pods/volumes
  `fxtr-spawn=<id>` and sweeps by label; builder containers should do
  the same (e.g. `fxtr-nix-builder=<repo hash>`) so the clear CLI is
  a label query, not name parsing.

## Chunk 1 — derisk spike (manual, gate)

Spike dir + NOTES.md, stage-7-style; findings folded back here.

a. **Podman socket into a container** (macOS): bind-mount the VM-side
   API socket into a running container; from inside, hit the API
   (`curl --unix-socket` or skopeo). The load-bearing assumption of
   in-container delivery.
b. **In-container skopeo delivery end-to-end:** inside an amd64 nix
   container, build/obtain skopeo-nix2container, `skopeo copy
   nix:<json> docker-daemon:localhost/...` via the mounted socket; run
   the image from the host. **DONE (with h) — `deliver_split_test.py`:
   tarless in-container skopeo→socket delivery works; delivered image
   runs host-side. Socket bind source = `podman info
   .Host.RemoteSocket.Path` (VM-side); deliverer needs `label=disable`.
   skopeo provenance must be fxtr-pinned (see Security posture).**
c. **Shared `/nix` volume:** two builder containers, concurrent
   `nix build`s against the shared volume; verify locking behaves.
   Also verify the copy-up initialization of `nixos/nix`'s `/nix`.
   **DONE — `concurrency_test.py`: 3 distinct derivations built
   concurrently against one shared volume all succeed (parallel
   writers OK); 2 identical concurrent builds both succeed with the
   same out path (nix build lock serializes, no double-build). Copy-up
   init exercised by every spike run.**
d. **git+file flake fetch from an RO bind mount** with mismatched
   uid/ownership (libgit2-based fetcher vs git's safe.directory
   rules); plus the bare-path dirty-tree behavior the MCP relies on.
   **DONE — `repo_fetch_test.py`: clean pass, no mitigations needed.
   No `safe.directory`/ownership error (rootless virtiofs ownership
   aligns; `safe.directory=*` via env is a validated harmless RO-safe
   fallback); no `git` binary needed (libgit2 covers it — builder image
   needs no extra git); RO mount fine (no index/lock writes); dirty
   `git+file:///workdir` surfaces uncommitted tracked edits (MCP
   non-frozen semantics) while `?rev=` pins to committed.**
e. **Materialize round-trip:** build an entity layer with a parent
   layer in-container, closure-copy out, read host-side with a
   hand-rolled remap (pre-v2-hook) to validate the design. **DONE —
   `materialize_test.py`: child's `parents.json` holds the absolute
   parent store path, which does NOT exist host-side (raw lookup fails)
   but the basename remap resolves it after a verbatim closure copy-out.
   Confirms the dumb copy-out + v2 reader remap-hook design is
   sufficient. Copy-out via bind-mounted scratch + `cp -a` is simpler
   than `podman cp`/tar-pipe (data point for that open question).**
f. **Build perf in the machine — was the headline gate, now mostly
   de-risked by the macOS 26 upgrade (2026-06-06).** History: this
   was "under Rosetta", stage 8 re-aimed it at qemu-user (Rosetta
   disabled + broken pre-macOS-26), and the upgrade re-opened it —
   the applehv machine now runs amd64 under Rosetta at near-native
   speed (see stage8_podman.md's "Rosetta re-opened" addendum; the
   one-time `enable-rosetta` marker step is in
   `fxtr_sandboxing/CODE_OVERVIEW.md`). **Decision (2026-06-06): not
   worth a dedicated spike.** Rosetta being back removes the headline
   risk — the gate spike's in-container `nix build`s ran fine under
   Rosetta with no pathological slowness — and the perf concern was
   really a qemu-user artifact, now just the macOS-<26 fallback. Treat
   perf as an *optional sanity check* during implementation (a
   representative uv2nix venv build vs the host-nix/virby baseline, if
   something feels slow), not a gate. The old escape options (host
   backend stays recommended; an aarch64-linux builder variant —
   breaks canonicalSystem; Linux-hosts-only scoping) remain
   contingencies only if real-world perf later disappoints.

g. **`sandbox = true` inside the builder container — THE GATE** (see
   the Security posture section; resolves the default-vs-option
   decision). Find the **minimum privilege rung** at which sandboxed
   builds succeed: try `nix build` of a normal derivation with
   `sandbox = true` in the amd64 `nixos/nix` container at escalating
   privilege — (i) no extra caps (rootless podman default userns),
   (ii) `--cap-add` the specific caps nix's sandbox wants (likely
   `CAP_SYS_ADMIN`; possibly `--security-opt unmask`/`seccomp`
   tweaks for nested-userns `unshare`/`mount`), (iii) `--privileged`.
   Report the lowest rung that works (or that none do). Test **both**
   single-user and multi-user-daemon (`nixbld` users) configs.
   **Positive controls — measure real isolation, not just "it ran":**
   a build step that tries to `curl`/open a socket must **fail**
   under the sandbox (network denied), while a **fixed-output
   derivation** (hash-pinned `fetchurl`) must **succeed** (FOD
   network allowed) — i.e. reproduce virby's network rule exactly.
   Also confirm the RO-store property (a build attempting to write a
   sibling `/nix/store` path fails). Map the result onto the ladder
   in the Security posture section. **DONE — see the Security-posture
   spike result for the full grid. Minimum rung: `SYS_ADMIN +
   label=disable + unmask=all` (tighter than `--privileged`); sandbox
   reaches full virby parity there; `sandbox-fallback=false` mandatory;
   `filter-syscalls` native-arch-only; rootful buys nothing (stay
   rootless). Narrowed `unmask=all` → `unmask=/proc/*` (keeps
   `/sys/firmware` masked; `/proc` must be whole — partial fails).**
   Remaining open sub-thread (lower priority): re-test the rung on a
   **native Linux host**, where the regime likely differs (and may hit
   outcome 1, sandbox at no/low privilege).

h. **Builder/deliverer split** (see Security posture). Untrusted build
   container with **no API socket** produces store paths into the
   shared `/nix` volume; a separate trusted deliverer container (holds
   the socket, runs fxtr-pinned skopeo) delivers them. **DONE —
   `deliver_split_test.py`: builder builds the image.json sandboxed
   with no socket; negative control confirms no API socket is reachable
   from the builder; the deliverer (only `label=disable`, sandbox=false)
   picks up the store output and delivers it via the mounted socket;
   host runs the result.** Remaining (lower priority): the stretch
   **filtering socket proxy** in front of the deliverer's socket
   (allowlist ping/version + image-load), to shrink even the
   deliverer's capability — same sidecar shape as stage 8.

## Chunk 1.5 — multi-user nix-daemon spike (gate)

**Status: DONE (2026-06-07, `multiuser_test.py`; full finding in the
spike's `NOTES_STAGE9.md`). Result: GATE PASS → multi-user is the
default**, with one image-pin rider.

A detached builder at the moderate rung (`SYS_ADMIN + label=disable +
unmask=/proc/*`) was bootstrapped multi-user in-container — `nixbld`
group + `nixbld1..4` users (appended to `/etc/passwd`+`/etc/group`; no
`shadow` tools needed, mirroring item d's "no git binary"); `nix-daemon`
launched detached; `/etc/nix/nix.conf` with `sandbox = true`,
`sandbox-fallback = false`, `filter-syscalls = false`,
`build-users-group = nixbld` — then every gate probe re-run through the
daemon (`NIX_REMOTE=daemon`):

- **Full first-line parity holds via the daemon:** normal-build network
  **DENIED**, FOD **fetches**, sibling `/nix/store` write **discarded**
  (real store clean) — and the build runs **non-root** (`uid=1000` in
  the sandbox namespace; build-user separation active since a root
  daemon with `build-users-group` set acquires a build user or fails).
  So multi-user adds its post-escape containment at **no extra privilege**
  over single-user — same rung.
- **Critical rider — bump the builder image pin.** The **2.24.9** pin
  (gate / stage-8) **crashes the daemon** the instant a build starts
  (`monitor-fd.hh:44 "Unexpected condition"` — the daemon's poll-for-hangup
  thread; a daemon-only path single-user never hits, almost certainly a
  Rosetta `poll()` artifact). **2.34.7 fixes it.** So the
  `docker.io/nixos/nix` pin must be a nix new enough to carry the
  monitor-fd fix (**2.34.7 is a confirmed safe floor**; exact fixed
  version not bisected). This supersedes the stale `2.24.9` examples
  elsewhere in this doc.

**Folded into the plan:** "Builder + deliverer containers" now specifies
**multi-user (`build-users-group = nixbld` + `nix-daemon`)** as the
builder config and a **≥2.34.7 image pin**; single-user stays a
documented fallback / future config knob.

## Implementation chunks (after the spike)

2. **v2 reader remap hook** + unit tests (pure v2 change, landable
   independently). **DONE (2026-06-07):** `ParentPathRemap` (prefix-pair)
   in `v2/fxtr2/data/layered_file_store/search.py`, applied in
   `_LayerSearchSet.expand_parents_of` (covers all depths, discovered
   parents only); threaded through `LayeredFileStoreReader(...,
   parent_path_remap=)`; re-exported from the package. Writer untouched
   (read-side only). Tests: `tests/test_layered_file_store/test_parent_remap.py`
   (5 cases incl. transitive + negative control); full LFS suite green.
3. **Project scaffold + lifecycle:** `projects/fxtr_nix_runner/`
   package; backend resolution (`FXTR_NIX_BACKEND` env + the minimal
   `nix_runner.toml`, env-wins-for-backend; initial default `host`); the
   **refcounted async-context-manager** builder lifecycle (one per repo
   per process, lazy start / last-out teardown) + the lazy paired
   deliverer; the host backend (trivial passthrough) + its tests. Set up
   the **parameterized backend fixture** here (see Testing strategy) so
   the host backend's tests are already the `host` variant of the shared
   matrix. **DONE (2026-06-07):** `projects/fxtr_nix_runner/` (anyio-only;
   `_config.py` env+toml resolution, `_backend.py` `Backend`/`Handle`/
   `NixResult`/`Materialized`, `_lifecycle.py` generic
   `RefcountedResourceRegistry`, `_host.py` passthrough, `_resolve.py`).
   The refcounted lifecycle landed as the **generic registry** (podman-free,
   unit-tested); the podman backend that *drives containers* through it is
   chunk 4 — so `resolve_backend("podman")` raises `NotImplementedError`
   for now and the sweep fixture skips that variant. 21 tests; precommit
   pyright/isort hooks added for the new project.
4. **Podman backend:** `run_nix` / `flake_ref_root` translation (incl.
   the moderate-rung sandbox flags + `filter-syscalls` arch detection +
   the **multi-user builder bootstrap** — `nixbld` users + `nix-daemon` +
   `NIX_REMOTE=daemon`, image pinned **≥2.34.7**, per Chunk 1.5); the two
   named volumes (`fxtr-nix` store + `fxtr-nix-cache` eval cache via
   `XDG_CACHE_HOME`); `materialize` (+ `nix copy` into the host
   bind-mount store); the crash-debris `clear` CLI. Tested as the
   `podman` variant of the shared backend fixture (skips without podman).
   **DONE (2026-06-07):** `_podman.py` (`PodmanBackend`/`PodmanHandle`,
   builder lifecycle via the chunk-3 registry, multi-user bootstrap,
   moderate rung, `filter-syscalls` auto via `podman info Host.Arch`,
   both volumes, `materialize` via `nix copy` + remap), `_clear.py` +
   `__main__.py` (`python -m fxtr_nix_runner clear`). Live-tested on the
   applehv machine: run-nix smoke, refcounted share/teardown, clear, and a
   build→materialize→host-read roundtrip all green (28 tests total).
   Podman container tests are serial (`-n0`, shared global podman state).
5. **Wrapper adoption:** the three wrappers route through the backend
   (host backend ⇒ zero behavior change; existing suites are the
   regression net). Spawner wasm arm + MCP + dispatcher consume
   `materialize` paths/remaps. **DONE (2026-06-07):** all three wrappers
   take a handle (consumer opens `builder_for(repo)`, holds a
   `resolve_backend()` instance — spawner/MCP/dispatcher each); wrappers
   materialize internally and the MCP/dispatcher thread the remap into
   `LayeredFileStoreReader(parent_path_remap=…)`. `fxtr-nix-runner` added as
   a dep to all three packages. Green under host backend: `test_nix_spawner`
   + `test_nix_container_spawner` (sandboxing), `test_nix_util` +
   `test_in_memory_e2e_retrieve_nix_entity` (MCP), `test_nix_dispatch_preset`
   (dispatcher). Container *delivery* still goes through the stage-8 host
   path (`_run_nix_container_internal`); the podman `deliver_image` arm is
   chunk 6.
6. **In-container delivery** (see "Abstraction boundary" + the
   `deliver_image` interface). Relocate the **store-side delivery
   machinery** into `fxtr_nix_runner` — the skopeo pin + build,
   `resolve_podman_socket`, the image-ref + platform-from-manifest
   helpers, and the host skopeo→runtime load (the old
   `_load_image_into_runtime` body) — and add the **podman deliverer**
   (lazy, shares the builder's lifetime; reads the manifest + execs the
   pre-built skopeo against the VM socket). `Handle.deliver_image(image_json,
   *, runtime) -> DeliveredImage(ref, platform)`; `podman → non-podman`
   raises. Then `fxtr_sandboxing`: the spawner's container arm calls
   `h.deliver_image(...)` inside `builder_for` and `_run_nix_container_internal`
   becomes **run-only** (takes `image_ref` + `platform`, no skopeo/delivery);
   `nix.build_skopeo` folds into `deliver_image` and is removed. `get_runtime`
   + `podman_is_remote` stay in `fxtr_sandboxing`. **DONE (2026-06-07):**
   `fxtr_nix_runner._delivery` (skopeo pin, `resolve_podman_socket`,
   `image_ref_for`, `platform_from_manifest`, `host_load_into_runtime`);
   `DeliveredImage(image_ref, platform)`; host arm builds skopeo on host nix +
   loads the host-readable manifest; podman arm builds skopeo in the builder,
   reads the manifest in-container, and execs it from a **lazy deliverer that
   shares the builder's lifetime** (`_Builder.deliverer_id`, torn down with
   the builder) against the VM socket (`podman → non-podman` raises).
   `_run_nix_container_internal` is now run-only; `build_skopeo` removed.
   Live-tested on podman (build image + skopeo in builder → deliverer → runs
   "delivered-ok") and host backend (`test_nix_container_spawner` green).
   Delivery unit tests moved to `fxtr_nix_runner/tests/test_delivery.py`.
6.5. **Make the podman backend genuinely host-nix-free** (surfaced running
   chunk 7: the E2E "passed" on podman only because the harness pre-created
   `flake.lock` with *host* nix, and the workdir's flake pins its lib input to
   a local path the builder can't see). Two gaps:
   - **Local-path flake inputs unreachable in-container.** A workdir flake's
     inputs can be `git+file://`/`path:` references to *other* local paths
     (e.g. a local lib checkout/mirror) — unreachable when only `/workdir` is
     mounted. Fix: a **`mountable_paths`** whitelist in the toml (`[podman]`),
     bind-mounted **RO at their abspaths** (skip-missing). A flake input under
     a mounted dir resolves like normal; nix's error names an unreachable
     path. *Config-driven, not lockfile-driven* — we can't parse a
     (host-nix-created) `flake.lock` in a no-host-nix world.
   - **No way to create/refresh `flake.lock` without host nix.** Frozen builds
     (spawn/preset) require a committed lock; agents adding inputs need to
     relock. Fix: **`Handle.lock()`** — host: `nix flake lock <workdir>`;
     podman: `nix flake lock /workdir` in the builder, which mounts `/workdir`
     **read-write** (parity with the host backend, where the workdir is
     inherently writable; nix's `sandbox=true` keeps the *untrusted build* out
     of `/workdir` — it builds from a store copy — so only nix-the-tool writes
     there, just `flake.lock`). The lock lands on the host workdir via the
     bind mount; no transient container.
   - Wire-up: an MCP **`nix_lock` tool** calls `h.lock()`; the
     **`fxtr-workdir-harness` locks via the configured backend** (not host nix
     directly) so the E2E uses host nix only in host mode. (Chunk 7 finalizes
     *how* — see its supersession note: the harness takes a `fxtr_nix_runner`
     dep and always locks through `resolve_backend()`, rather than the sync
     `flake_locker` hook below.)
   - Test/CI hermeticity: **`FXTR_NIX_RUNNER_CONFIG`** env = a full config-file
     override (supersedes the default file + `FXTR_NIX_BACKEND`); the E2E
     points it at a temp toml with `backend = "podman"` + `mountable_paths`.

   **DONE (2026-06-07) — surfaces:** `Handle.lock()` (host + podman, `/workdir`
   mounted RW; live-tested on podman); `PodmanConfig.mountable_paths` (RO
   abspath mounts, skip-missing) + `FXTR_NIX_RUNNER_CONFIG` full-override;
   the `fxtr_nix lock [path]` / `fxtr_nix clear` CLI (console script;
   live-tested on podman, no host nix); the MCP top-level **`nix_lock`** tool;
   and a (provisional) `flake_locker` hook on `prepare_workdir_repo`. **Note:
   chunk 7 supersedes that hook.** The sync callback never got connected to a
   real (async) backend locker, and keeping the harness dependency-free is no
   longer worth it now that `fxtr_nix_runner` is the shared nix dep — so chunk 7
   gives the harness a `fxtr_nix_runner` dep, makes `prepare_workdir_repo`
   `async`, and **always** locks via `resolve_backend()` (auto-configured from
   env: host → host `nix flake lock`, podman → in-podman). No `flake_locker`
   arg, no switches. It also **reorders to lock against a real git repo** —
   `git init` + `git add -A`, *then* lock, then commit — so the harness
   exercises the same mode production does (`Handle.lock()` via the `nix_lock`
   flow always runs against a committed git workdir, never a plain dir; this
   also exercises `builder_for`'s git-repo-root keying instead of the no-repo
   fallback). First attempt: lock with the flake **added-but-not-committed** on
   the fresh repo (single snapshot commit); if nix rejects an unborn HEAD, add
   an initial commit before the lock. Chunk 7 then runs the suites genuinely
   host-nix-free.
7. **E2E + test cleanup.** Run the stage-7/8 suites
   (`test_nix_spawner.py`, `test_nix_container_spawner.py`, transport
   + nested-spawn tests, MCP `retrieve_nix_entity` tests, dispatcher
   `test_nix_dispatch_preset.py`) under the podman backend, **genuinely
   host-nix-free**. Wiring is **pure env, no per-suite changes**: the harness
   now always locks via the configured backend (see the 6.5 supersession
   note), so a podman run is just `FXTR_NIX_RUNNER_CONFIG=<temp toml>` with
   `backend = "podman"` + `mountable_paths = [<lib-mirror dir>]` (on a jj box
   the workdir flake's `fxtr` input is `git+file://<mirror>?rev=…`, so the
   builder must mount that mirror at its abspath). Document how the suites
   select the backend (CI/dev docs). E2E suites are **env-selected, not swept**
   (see Testing strategy) — backend parameterization lives in the direct
   `fxtr_nix_runner` tests (chunks 3–4).

   - **Prove the no-host-nix property — ad-hoc, not a suite fixture.** After
     the wiring the podman path is host-nix-free *by construction* (lock →
     in-podman; `uv lock` → uv, not nix; all builds → podman backend), so the
     proof is a one-time confirmation we didn't miss a path — not an ongoing
     invariant worth a permanent PATH-scrubbing fixture (which would also have
     to avoid breaking the `fxtr_nix_runner` host-backend sweep, whose `host`
     variant legitimately needs real nix). Instead add a small
     **`run_with_masked_nix`** tool in `fxtr_nix_runner` (Python: a temp dir of
     `nix` / `nix-*` shims that log their argv to a tripwire file and exit
     non-zero; prepend it to `PATH`; run the wrapped command; `finally`-clean
     the temp dir; if the tripwire fired, report the leaked calls and exit
     non-zero — so it both masks *and* verifies). Run the podman E2E under it
     (`run-with-masked-nix -- env FXTR_NIX_RUNNER_CONFIG=… pytest …`) to confirm
     zero host-nix invocations; document the recipe in the relevant test module
     docstring(s).
   - **Test cleanup — stop nuking the shared cache.** The chunk-4
     `clear` test currently shells out to real podman — `clear()`
     `podman volume rm`s the shared `fxtr-nix` store, throwing away the warm
     nixpkgs/closure cache and making every later test cold + slow, and its
     label-sweep can kill a concurrent test's builder. Rewrite it as a **pure
     unit test** that mocks the podman layer (patch `_clear._run` +
     `shutil.rmtree`; assert `clear()` issues the right
     `ps`/`rm`/`volume rm`/scratch-wipe sequence), dropping `requires_podman` —
     no separate live real-clear test (the chunk-4 live verification already
     covered the real path). Audit the other `fxtr_nix_runner` podman tests for
     shared-state mutation: the materialize/deliver tests only *populate* the
     store (cache-warming, fine) and builders are keyed per-repo-hash (no
     cross-test collision), so `clear` is the only genuine offender.

   **DONE (2026-06-08).** Test cleanup (mocked `clear` unit test); the
   `run_with_masked_nix` tool (+ tests); the harness async git-order lock
   wiring (zero per-suite changes — `prepare_workdir_repo` always locks via
   `resolve_backend()`); and a `container_runtime`-sweep skip so non-podman
   runtime variants drop out under the podman nix backend. **Full E2E sweep
   green under the podman backend with host nix masked** (`run_with_masked_nix`,
   tripwire empty on every run): wasm spawner, container spawner, wasm +
   container transport, nested-spawn, MCP `nix_spawn` / `retrieve_nix_entity`
   (entity-layer materialize + reader remap) / `set_preset`, dispatcher preset
   — `[container]` runtime variants skip as designed. The flow surfaced + fixed
   **three** real bugs: (1) `PodmanHandle.lock` was hardcoded to `/workdir`
   (wrong for a subpath flake once the workdir is a git repo keyed on its
   root) → now `flake_ref_root()`; (2) `_mkGuestSitePackages` shipped
   `__pycache__` whose .pyc dragged the full linux python3/glibc/ncurses
   closure into the wasm output (broke `materialize` on the case-insensitive
   macOS host store) → stripped to a zero-dependency closure; (3) the
   container_runtime sweep tried the Apple-Container variant under the podman
   nix backend (unsupported delivery) → now skips. Host-backend regression of
   the same suites: see the run notes. (Transient GitHub-archive 504s on the
   nix2container nixpkgs fetch cleared on retry — infra, not code.)
8. **Stage close-out + the backend-selection decision.** **Decided: no
   implicit default backend.** Rather than guess host-vs-podman (or
   re-litigate the security tradeoff per machine), fxtr requires an *explicit*
   choice — a config file or an env override — and otherwise errors pointing
   to a one-stop **setup wizard**. The full design — a unified backend+runtime
   config, the no-default resolution, and the detect/nudge/recommend wizard
   (the no-host-nix quickstart, the agent `nix_lock` workflow, the config
   surface, and the security/convenience tradeoff all surface *there*) — is
   **deferred to `stage9post1_backend_config_wizard.md`**; until it lands the
   interim code default stays `host`. Stage-9 close-out docs to do now:
   - **the host-nix-free verification recipe** (from chunk 7): how to run the
     downstream E2E suites against the podman backend with host nix proven
     absent — a temp `FXTR_NIX_RUNNER_CONFIG` toml (`backend = "podman"` +
     `mountable_paths = [<lib mirror>]`) run under `run-with-masked-nix`
     (`uv run python -m fxtr_nix_runner._mask_nix -- … pytest …`); note that the
     `container_runtime` sweep's non-podman (`[container]`) variants **skip**
     under the podman nix backend (the backend only delivers into podman). This
     is the chunk-7 "document the recipe" item, landed here.
   - **the `materialize` case-insensitivity limitation** (chunk 7): `nix copy`
     reproduces a closure verbatim, so a closure with case-colliding names
     (classically linux `ncurses` terminfo) fails on a case-insensitive
     `materialize_root` (default macOS APFS). Mitigation in practice: keep guest
     build outputs **dependency-free** — `_mkGuestSitePackages` strips
     `__pycache__` (whose .pyc embed origin store paths that drag in the linux
     python3/glibc/ncurses closure), so wasm guests recompile bytecode at
     import; a closure that genuinely needs case-colliding paths would require a
     case-sensitive `materialize_root` volume. (Already noted in
     `PodmanHandle.materialize`'s docstring.)
   - CODE_OVERVIEWs (fxtr_nix_runner's own, fxtr_sandboxing, MCP, workstation),
     `overview.md` stage map, `followups.md` (collapse-the-escape-hatch +
     deferred items: filtering socket proxy, native-Linux rung, lock for a
     dirty *input*, persistent deliverer; **builder binary-cache substituter**
     to cut the GitHub-archive dependence — chunk-7 runs hit transient
     `archive/<rev>.tar.gz` 504s on the nix2container nixpkgs fetch that only
     cleared on retry; **case-sensitive `materialize_root` volume** as the
     general fix for case-colliding closures).
   Update CODE_OVERVIEW.md as the final step.

   **DONE (2026-06-08).** Decision recorded (no implicit default → explicit
   config + setup wizard, deferred to `stage9post1_backend_config_wizard.md`).
   Doc close-out trimmed to what actually went stale this session:
   `fxtr_nix_runner/CODE_OVERVIEW.md` (added `run-with-masked-nix` / `_mask_nix`
   + its test, fixed the now-subpath-aware `lock()` line); `overview.md` (stage
   9 marked done + the post-1 follow-on added); `followups.md` (the two chunk-7
   deferrals). The fxtr_sandboxing / MCP / workstation CODE_OVERVIEWs were
   already current — chunks 3–6.5 updated them as they landed, and this session
   only touched their test fixtures / `await`s. The verification recipe + the
   materialize limitation stay captured in the `_mask_nix` /
   `PodmanHandle.materialize` docstrings + this plan. **Stage 9 complete;
   backend selection continues in post-1.**

## Testing strategy — backend parameterization

Mirror stage 8's runtime sweep (`conftest.py`'s parameterized
`container_runtime` fixture, `stage8post1_runtime_sweep_tests.md`):

- **Tests that directly exercise `fxtr_nix_runner`** (the backend's own
  unit/integration suite — `run_nix`, `flake_ref_root`, `materialize`,
  `deliver_image`) are **parameterized over both backends** (`host` and
  `podman`) as variants, via a shared backend fixture. Each variant
  **skips** when its environment is unavailable — `host` skips with no
  host nix, `podman` skips with no podman machine / runtime — so a dev
  box with both runs the full matrix and CI runs whatever it has, with
  neither backend able to rot silently (same rationale as the stage-8
  runtime sweep). The fixture overrides `FXTR_NIX_BACKEND` per variant
  (and warns if an ambient value is set, like the stage-8 fixture does
  for `FXTR_CONTAINER_RUNTIME`).
- **Downstream E2E suites** (the stage-7/8 spawner/transport/nested-spawn
  tests, MCP `retrieve_nix_entity`, dispatcher `test_nix_dispatch_preset`)
  do **not** sweep — they run against whatever backend the environment
  selects, keeping their cost bounded. The **host** variant uses
  `FXTR_NIX_BACKEND` (default); the **podman** variant uses a temp
  `FXTR_NIX_RUNNER_CONFIG` toml (it needs `mountable_paths` for the harness's
  local lib-mirror input) and runs **with host nix scrubbed from `PATH`** to
  prove the no-host-nix property (see chunk 7). The podman E2E variant also
  needs the moderate-rung sandbox builder (see Security posture). CI/dev docs
  say how to select each.

## Open questions (to resolve during spike/implementation)

- ~~Exact socket path discovery + `DOCKER_HOST` plumbing for skopeo~~
  **Resolved (spike a/b/h):** the deliverer bind-mounts the **VM-side**
  socket from `podman info --format '{{.Host.RemoteSocket.Path}}'`
  (applehv rootless: `/run/user/501/podman/podman.sock`; `-v` sources
  resolve VM-side), and skopeo targets it via `--dest-daemon-host
  unix://…` — **no `DOCKER_HOST` plumbing needed**. The deliverer needs
  `--security-opt label=disable` for the socket (SELinux). (Host-side
  skopeo on the host backend still uses `machine inspect` /
  `fxtr_sandboxing._podman.resolve_podman_socket`, unchanged.)
- ~~`podman cp` vs tar-pipe for closure copy-out (perf + symlink
  fidelity).~~ **Resolved: use `nix copy` into a host bind-mount chroot
  store (closure-aware + dedup + GC + preserves structure) — see
  Materialization. Neither podman cp nor tar-pipe needed.**
- ~~Whether `nixos/nix`'s image needs git installed for the bare-path
  (dirty-tree) fetcher, or libgit2 covers it.~~ **Resolved (item d):
  libgit2 covers it — no `git` binary needed in the builder image.**
- ~~Builder-container idle policy~~ **Resolved:** refcounted async
  context manager, one builder per repo per process, last-out tears
  down (see Builder + deliverer containers → Lifecycle). No idle
  sprawl; `clear` CLI is crash-debris only.
- ~~Eval-cache warmth~~ **Resolved:** a second shared named volume
  (`fxtr-nix-cache`) mounted as `XDG_CACHE_HOME`, shared across builders,
  `/nix` kept pristine (see Mounts + volumes).

## End-of-stage check

- On a machine with **no host nix** (verified by scrubbing `nix` from
  `PATH`): podman machine + the repo + the podman backend is sufficient for
  the full loop — `nix_lock` (creates `flake.lock` in-podman),
  `retrieve_nix_entity`, wasm spawn, container spawn (image delivered from
  inside the builder; no host skopeo), `set_preset` — with any local-path
  flake inputs declared in `mountable_paths`.
- All existing nix-path suites green under the podman backend *and*
  under `FXTR_NIX_BACKEND=host` (no regression; host-nix + Apple
  Container combo still works as a supported non-default path — kept
  honest by the `stage8post1_runtime_sweep_tests.md` sweep).
- A layer entity whose layer has parents reads correctly host-side
  through the remap hook.
- `fxtr_nix_runner clear` empties the store + eval-cache volumes +
  scratch (and sweeps any builder/deliverer debris by label); the next
  build cold-starts correctly.
