# Part 1 — `fxtr_nix_runner` becomes the fxtr2-aware flake manager

> Status: RESHAPED DRAFT (2026-06-13). Supersedes the old
> `part1_build_rehost.md`. Design context: `discussion_topics.md`
> "Decided (fourth sync)" (RUNNER-1..6), `design/05` §1,
> `design/07_build_rehost_networking.md`, part5 note
> `07_remote_entity_store.md`.
>
> **Prerequisite — LANDED:** `v2/plans/phase3/stage14_repo_store_refactor/overview.md`.
> The repo entity store now exists as a standalone `DeepFetchable`
> (`fxtr2.data.repo_storage.RepoEntityStore`); `GitNamespaceBackend` →
> `TsvNamespaceBackend` (module `fxtr2.data.tsv_namespace`), marker
> `fxtr_namespace.toml`, CLI `fxtr-tsv-ns`. Part 1 builds on the shipped API
> (signature notes inline in §3/§6).

## Goal

Promote `fxtr_nix_runner` from a pure nix transport to the **manager** of the
fxtr nix flake system. Concretely, this part delivers:

1. A **two-tier** package: the existing `Backend`/`Handle` transport stays
   anyio-only and fxtr2-free *internally*, gaining only generic hooks; a new
   fxtr2-aware **manager** tier sits on top.
2. **Repo-store discovery + create-if-absent**: given a repo, the manager
   finds (or creates) its repo entity store and opens it read-only.
3. The **unconditional build-time rehost** over that store, with
   `FXTR_REMOTE_BASE_URL` / `FXTR_REMOTE_BEARER_TOKEN` delivered into FODs on
   **both** backends (host + podman).
4. The **entities read adapter**: a `FxtrReadNamespace`-shaped view over a
   flake's `fxtr.entities.*`, whose readers carry the repo store as `remote=`
   so remote-pointer entities resolve transparently (this is the piece the
   MCP can later adopt — RUNNER-8).

Deliverable tests: (a) a trivial FOD that fetches one entity's bytes from the
rehost and writes them to `$out`, passing on host (and podman where
configured); (b) the entities adapter retrieving an entity whose layer holds
a remote pointer, resolving it through the repo store with no error.

## Moving parts

### 1. Verification spikes — DONE (2026-06-13)

The `design/07` spikes are **resolved** (full results in
`design/07_build_rehost_networking.md` "Spike results"). Option 1 wins on
both backends; no reverse tunnel / sidecar bridge needed. The load-bearing
outcomes this part builds on:

- **Networking works on both backends** from inside a FOD build (host:
  `127.0.0.1`; podman: `host.containers.internal` → gvproxy → host, **rehost
  must bind `0.0.0.0`**). Same output hash both ways.
- **Env delivery = `--option impure-env "K=V …"` per build**, confirmed
  end-to-end on **both** backends. On a multi-user daemon it needs **two
  daemon-side settings**: (1) the invoking user be a **trusted user**, and
  (2) **`configurable-impure-env` in the *daemon's* `experimental-features`**
  (a client flag does *not* propagate — the daemon silently drops `impure-env`
  without it). Podman owns both (root + the `nix.conf` we write). Host
  multi-user requires both set + a daemon restart (documented prerequisite).
  Single-user nix gets client-env `impureEnvVars` free (neither needed).
- **Podman builds need `filter-syscalls = false`** under Apple-silicon
  emulation (the "arch-resolved filter-syscalls" the backend already sets —
  ensure it applies to the build, not just skopeo).
- **Env is delivered per-build, not per-builder** — so the hooks sit on the
  build call, not `builder_for` (resolves the old TODO below).

### 2. Package shape — transport tier vs. manager tier

**Transport tier (unchanged philosophy, anyio-only, fxtr2-free):**
`Backend`/`Handle` keep `run_nix` / `lock` / `materialize` / `flake_ref_root`
/ `deliver_image`. They gain **generic** hooks only (no fxtr2 types):

```python
async with h.expose_host_endpoint(host_port) as ep:   # name TBD
    ep.url_for_builds      # FXTR_REMOTE_BASE_URL value valid inside builds
# host backend: identity (http://127.0.0.1:<port>)
# podman backend: whichever design/07 option wins
await h.run_nix(argv, extra_build_env={...})   # delivery mechanism per spike
```

`extra_build_env` carries the opaque `(url, token)` pair into builds; the
transport tier never learns what's behind the URL.

**Manager tier (new, fxtr2-aware):** a `FlakeManager` (name TBD) holding a
backend + a repo + the opened `RepoEntityStore`. It owns: repo-store
discovery, the rehost, wrapping builds with the rehost env, and the read
adapters. This is the single object the publishing serving script and
(eventually) the MCP talk to.

**[TODO: confirm the manager↔backend boundary once code exists — RUNNER-1
explicitly leaves "collapse the tiers if simpler" open. If keeping `Handle`
fully fxtr2-free makes the manager awkward, the fallback is parameterizing
the `Handle`/`Backend` constructor with the repo-store `DeepFetchable`.]**

### 3. Repo-store discovery + create-if-absent

The manager, given a repo path, calls
`RepoEntityStore.open(repo, create=True)` (stage 14, shipped): discovers
`.fxtr/` (via `repo_storage_config.load_repo_store_config`), opens the by-id
store, mints an empty one if the repo has none (fresh clone). Read-only from
the runner's side (RUNNER-3; no write paths).

**Signature wrinkle (verified against the shipped API).** `RepoEntityStore.open`
is `open(repo_path, *, create=False, read_only=False)`, and **`create=True` and
`read_only=True` are mutually exclusive** (raises `ValueError` — a read-only
session skips schema init, so the store must already exist). So "create-if-
absent *and* enforce read-only" is not one call. **DECIDED (2026-06-14):** the
manager opens `create=True` (a writable session) and simply never calls a write
path — convention-only read-only, matching RUNNER-3 ("read-only from the
runner's side"). One open, no enforcement; the two-pass (mint writable → reopen
`read_only=True`) was rejected as not worth the second open.

The manager never accepts a store handed in by a caller — callers shouldn't
have to care (RUNNER-3). (A future optimization could let a process that
already holds a `TsvNamespaceBackend` pass its `.store` — that path exists now
via `RepoEntityStore.from_session`; still out of scope here.)

**Exposure, not mutation.** The manager *exposes* its opened store as a
`(DeepFetchable, Lease)` **pair** (working name `mgr.repo_store` ≈
`(store, store.get_lease())`; `get_lease()` is the public accessor that hands
out a fresh clone of the session's root lease — `_root_lease` is private) so
consumers can mount it on **their own** scope. The manager never reaches into a caller's
scope to register sources on its behalf — the consumer owns its scope's
lifecycle and leases. (The entities adapter's `into_scope=` retrieve is the
same principle: the caller passes the scope; the manager attaches into it
explicitly, it doesn't hold or mutate a hidden one.)

The two consumption shapes are **fixed by the existing fxtr2 contracts** (read,
not spiked):
- **reader `remote=`** (`reader.py:59`) takes the `(DeepFetchable, Lease)`
  pair *directly* — this is what §6's entities adapter passes.
- **`scope.register_deep_fetch_source`** (`entity_ref_scope.py:4813`) takes
  `(source, bridge: LocalBridge)`, **not** a bare lease. Serving (part 5)
  wraps the source in an **`IndependentLocalBridge`** over a lease clone —
  "independent" (case 1) because the manager's store session outlives the
  consuming scope (the lifetime invariant below); same pattern
  `HttpFxtrReadNamespace` already uses.

**Lifetime invariant:** the store's session = the **manager's** lifetime
(`FlakeManager.open(...)` wraps build *and* serve), with the rehost as a
build-only sub-span underneath a longer-lived store. Serving holds the store
as a deep-fetch source for the whole serve, so the session must stay open that
long; scoping the store to the build span would break serving. This invariant
is what makes the bridge above `Independent`.

### 4. The rehost server (now in `fxtr_nix_runner`)

Home: **`fxtr_nix_runner`** (manager tier) — resolves the old OPEN-8
"where does this live" note: the runner now depends on fxtr2, so the rehost
colocates with the rest of the manager rather than in
`fxtr2/experimental/http/`.

- Reuse `create_fxtr_app(scope, pins)` from `fxtr2.experimental.http` rather
  than new routes: the rehost is "an app over a scope whose deep-fetch
  reaches the repo store". Register the `RepoEntityStore` (a `DeepFetchable`)
  as the scope's deep-fetch source; `/data/<id>` then resolves any id the
  store holds. `/pins`/`/pin` are unnecessary for FOD byte-fetches (the
  preload writer resolves names from TSVs — part 2), so a `/data`-only app
  suffices; serving the rest is harmless if convenient.
- **Bearer-token auth** wrapper (Starlette middleware checking
  `Authorization: Bearer <token>`; fresh `secrets.token_hex` per rehost).
- Ephemeral-port serving CM (mirrors `serving_context`,
  `fxtr2/experimental/http/server.py:96`):

  ```python
  @asynccontextmanager
  async def rehost_context(store, *, host="127.0.0.1") -> RehostInfo:
      # RehostInfo: base_url (real bound port), token, and the env dict
      # {FXTR_REMOTE_BASE_URL, FXTR_REMOTE_BEARER_TOKEN}
  ```

- Binding: `127.0.0.1` (host backend) vs `0.0.0.0` (podman, if the winning
  `design/07` option needs it — token is then the only access control;
  call it out). The manager picks per backend.
- **Unconditional (RUNNER-4):** the manager stands up the rehost for *every*
  managed build span and threads its env into `run_nix`, regardless of
  whether the build appears to declare preloads — we can't statically know
  whether a build/load needs the store bridged, so always-rehost is the only
  clean rule. Accept the small overhead on the MCP retrieve path.

### 5. `Handle` endpoint exposure + env delivery (host + podman)

The transport hooks from §2, implemented per the §1 spike outcome:

Resolved by the §1 spikes — concretely:
- Host backend: `expose_host_endpoint` → identity `http://127.0.0.1:<port>`;
  env delivered per-build via `--option impure-env` (requires the invoking
  user be a trusted-user on a multi-user daemon; single-user gets it free).
- Podman backend: `expose_host_endpoint` → `http://host.containers.internal:<port>`
  (rehost bound `0.0.0.0`); env delivered per-build via `--option impure-env`
  on the `run_nix` build call (root in the builder is trusted). The builder's
  `nix.conf` (which we write at container start) must include
  `configurable-impure-env` in `experimental-features` **and**
  `filter-syscalls = false` (emulation) — both daemon-side, in effect for
  builds. **Per-build, not per-builder** — no coupling to builder lifetime,
  env need not be known at `builder_for` time.

### 6. The entities read adapter

A `FxtrReadNamespace`-shaped (`fxtr2.experimental.read_namespace`) view over
a flake's `fxtr.entities.*` (RUNNER-5a):

- `list_pins(prefix)` — cheap `nix eval --apply` over `fxtr.entities` to get
  the attr names (the pin names). No builds.
- `retrieve(name, into_scope, ...)` — eval the one entity's metadata
  (`{type, layerKey}` / builtin `idString`; reuse the MCP's
  `_ENTITY_METADATA_APPLY` shape), build `<attr>.layer`, `materialize`, then
  open `LayeredFileStoreReader(mat.host_path, parent_path_remap=mat.remap,
  remote=(repo_store, lease))` and `retrieve(layerKey, into_scope=...,
  copy_into_scope=True)`. The `remote=` is the repo store, so a layer entity
  that is a remote pointer (or whose closure was left remote) resolves
  transparently instead of erroring (RUNNER-5 / the MCP's current gap).
- `rev` vs live (RUNNER-6): the adapter is constructed for either a committed
  rev (`git+file://…?rev=`) or the live workdir (bare path, non-frozen);
  blobs always come from the current repo store. Mirror the two existing ref
  forms (`design/03` §3/§4).
- Builtin short-circuit (`type=="builtin"`) needs no build — construct the
  `FxtrID` directly (as `retrieve_nix_entity` does today).

This is the adapter the MCP can later swap onto (RUNNER-8, part 6 optional
chunk). The **namespace** read view (`fxtr.namespaces.<name>`) is part 2.

### 7. Manager factory / consumer wiring

Consumers get a wired manager (backend resolved + repo store discovered +
rehost ready) from one factory rather than assembling it themselves —
"a backend connected to a remote entity store" is the out-of-the-box
experience (RUNNER-1/3). Sketch:

```python
async with FlakeManager.open(repo, rev=None) as mgr:   # discovers store, ready to rehost
    async with mgr.build_span() as span:               # stands up rehost, env wired
        ents = mgr.entities()                           # the §6 adapter
        ref = await ents.retrieve("report", into_scope=scope)
```

**[TODO: pin the exact surface once §2's boundary is settled — esp. whether
the rehost is per-`open` or per-`build_span`, and how the read adapters share
the build span's rehost.]**

## Implementation steps

- [x] Spikes — done 2026-06-13 (results in `design/07`; summarized in §1).
- [x] Transport hooks: `expose_host_endpoint` + `extra_build_env` on `Handle`
      (host backend first), generic/fxtr2-free; unit + host integration.
      *(Done 2026-06-14: `ExposedEndpoint` + `impure_env_nix_flags` in
      `_backend.py`; `expose_host_endpoint` + `run_nix(extra_build_env=)` on both
      host and podman handles — the logic is backend-neutral, so both got it to
      keep the `Handle` Protocol coherent/pyright-green; podman's builder
      `nix.conf` now carries `configurable-impure-env`. Host unit-tested
      (`test_backend.py`, `test_host_backend.py`); the podman path is
      spike-verified and gets live + FOD-E2E coverage in the dedicated podman /
      integration steps below.)*
- [x] `rehost.py` in `fxtr_nix_runner`: token middleware + `rehost_context` +
      `RehostInfo` over a `RepoEntityStore`. Unit tests (no nix): token
      reject/accept, ephemeral bind, `/data` round-trip against an in-process
      store.
      *(Done 2026-06-14: added the `fxtr2` dep to the runner pyproject
      (`../../v2` path source); `rehost.py` reuses `create_fxtr_app` over an
      isolated scope with the store as deep-fetch source + a pure-ASGI bearer
      middleware; ephemeral bind via pick-free-port + `host:port` (the `fd://`
      handoff had murky double-close semantics). `RehostInfo.build_env()` does
      the host-local→build-visible URL substitution the manager wires in step 3.
      Layering held: `import fxtr_nix_runner` stays fxtr2-free, verified in CI by
      eye + a check; tests in `test_rehost.py`.)*
- [x] Manager: repo-store discovery (`create=True`) + `FlakeManager.open` +
      `build_span` wiring the rehost env into `run_nix`.
      *(Done 2026-06-14: `manager.py` — `FlakeManager.open` resolves the backend
      + opens the store `create=True` (read-only by convention); `repo_store`
      exposes the `(DeepFetchable, Lease)` pair; `build_span` borrows a builder,
      stands up the rehost (bound per backend), composes the build-visible URL
      via `expose_host_endpoint`, and yields a `BuildSpan.run_nix` that threads
      the env in. Rehost = per-build-span, store = manager lifetime, as agreed.
      Host-backend tests in `test_manager.py` (repo_store resolves, create-if-
      absent, rehost reachable via its env). Entities adapter is the next step.)*
- [x] Entities read adapter (§6) + unit/integration tests, incl. the
      remote-pointer-resolves case.
      *(Done 2026-06-14: `entities.py` — `EntitiesReadAdapter`, FxtrReadNamespace-
      **shaped** not subclass (`retrieve`/`try_retrieve` + cheap
      `list_entity_names`, no id-valued `list_pins` — confirmed w/ Daniel). Mirrors
      the MCP's eval-metadata/build-`.layer`/materialize, but opens the reader with
      the repo store as `remote=` (RUNNER-5). `mgr.entities(span)` binds it.
      Unit-tested without nix (`test_entities.py`): a hand-built layer pinning a
      remote-only entity resolves through the in-process store; the full nix-flake
      E2E is the integration step below.)*
- [x] Podman backend: endpoint exposure + env delivery (per spike).
      *(Done 2026-06-14: the code landed in steps 1–2 (`host.containers.internal`
      URL, `configurable-impure-env` in the builder `nix.conf`, per-build
      `--option impure-env`); validated **end-to-end on a live podman machine**
      by `test_nix_e2e.py::test_fod_fetch_through_build_span[podman]` — a FOD
      fetched an entity's bytes from the rehost through the builder.)*
- [x] py_testing-style integration test: FOD fetch → `$out`, parameterized
      over the `nix_backend` sweep (podman skips when unconfigured; `-n0`).
      *(Done 2026-06-14: `test_nix_e2e.py` + the `_workdir_templates/rehost_fod`
      fixture (via `fxtr_workdir_harness`). FOD-fetch + entities-adapter builtin
      retrieve over the sweep. **Known limitation:** the host backend's
      `expose_host_endpoint` → `127.0.0.1` only works when builds run locally;
      on macOS host nix offloads linux builds to a remote builder VM that can't
      reach the mac-host loopback rehost, so the FOD test **skips on darwin
      host** (podman is the validated macOS path; host works on linux CI). Added
      `fxtr-workdir-harness` as a runner dev dep (dev-only cycle, editable).
      NOTE: the full remote-pointer entities-adapter E2E (a real preload-FOD
      layer read back through the adapter) lands in **part 2** — it can use
      `lib.fakeHash` manually-pinned without waiting for part 2b.)*
- [x] Update `projects/fxtr_nix_runner/CODE_OVERVIEW.md` (the new manager
      tier + fxtr2 dep). *(Done; the `create_fxtr_app` auth wrapper stayed
      runner-side in `rehost.py`, so no `v2/fxtr2/experimental` change was
      needed; no `v2/CODE_OVERVIEW.md` change either — the manager tier is
      entirely in `fxtr_nix_runner`.)*

## Discuss before starting / TODOs

- **Tier boundary** (RUNNER-1, §2): keep `Handle` fxtr2-free + manager-wraps,
  vs. parameterize `Handle` with the repo-store `DeepFetchable`. Take a stab
  with the former; revisit.
- **Rehost lifetime** (§4/§7): per-`open` vs per-`build_span`; and whether one
  rehost is reused across multiple builds in a span. (Env delivery is
  per-build regardless — see below — so this is purely a "how long is the
  HTTP server up" ergonomics call, not a correctness one.)
- ~~Per-build vs per-builder env on podman~~ **RESOLVED by the spike**:
  per-build via `--option impure-env` on the `run_nix` build call (both
  backends). No coupling to builder lifetime.
- **Host daemon prerequisites** (new, from the spike): on a multi-user host
  daemon, per-build env delivery needs **two** static daemon settings —
  the invoking user in `trusted-users` **and** `configurable-impure-env` in
  the daemon's `experimental-features` (single-user nix and the podman
  backend need neither). Decide how the manager surfaces a missing one (a
  clear error pointing at the fix, à la the `require_*` config accessors,
  ideally distinguishing the two causes) vs. silently assuming it. Leaning:
  detect + friendly error. Possibly fold a host-readiness check into the
  `fxtr_nix runtime` wizard.
- **fxtr2 dep mechanics**: adding `fxtr2` to `fxtr_nix_runner`'s pyproject —
  confirm no surprise transitive weight for the spawner (`fxtr_sandboxing`,
  which already depends on fxtr2) and that the runner's existing pure-transport
  tests don't need fxtr2 at import time (keep fxtr2 imports inside the manager
  module so `_backend`/`_host`/`_podman` stay importable without it).
- **httpx in writer venvs**: the preload FOD (part 2) fetches over HTTP from
  inside a writer venv — confirm httpx is a hard fxtr2 dep (so the
  `fxtr_nix_writer` venv gets it) or add it.
- Does the FOD-side fetch want `/pin` access? (Part 2 resolves names from
  TSVs, so likely no — `/data`-only rehost is fine.)
