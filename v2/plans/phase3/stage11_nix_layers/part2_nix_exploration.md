# Stage 11 — Part 2: Nix exploration (prototype)

> **Status update (2026-05-21):** Implementation paused mid-nix-flake.
> Python side (lib, fake_remote, preload script) works end-to-end via
> plain uv. Nix flake hit uv2nix's inability to handle path-deps that
> reach outside its workspace root — fxtr2 and fxtr v1 both do. See
> [part2_nix_exploration_findings.md](./part2_nix_exploration_findings.md)
> for what we tried, what we found, and proposed paths forward
> (uv-in-derivation, hybrid uv2nix + hand-written derivations,
> single-flake-at-repo-root, git-URL deps, etc.). The work-in-progress
> spike lives at
> `/Users/daniel/extgit/spikes/2026_05_21_nix_fxtr_prototype/`
> (moved out of the fxtr repo during exploration).
>
> **Plan v2 (2026-05-21):** see ["Plan v2: git-source rework"](#plan-v2-git-source-rework-2026-05-21)
> below. We're rebuilding the spike around git-sourced fxtr / fxtr2
> dependencies (Option D from the findings doc), with a single
> flake.nix at the prototype root that imports per-derivation nix
> modules. The original implementation steps (further down) are
> superseded by the Plan v2 step list.

## Plan v2: git-source rework (2026-05-21)

The goal is unchanged (see "Goal" further down): an end-to-end
fake-remote → preload-FOD → augment-derivation → demo-read flow.
What changes is *how the build pulls in fxtr / fxtr2*.

### The structural change

- **fxtr is on GitHub** at `https://github.com/TransluceAI/fxtr`,
  branch `push-uxypqqlzwtrk` (jj-generated bookmark; we'll point at
  it directly for now, swap later if needed).
- Each spike `pyproject.toml` declares fxtr2 and fxtr v1 via
  `tool.uv.sources` **git URLs** (with `subdirectory = "v2"` for
  fxtr2). uv resolves the branch to a SHA at lock time; the SHA
  lands in `uv.lock`. uv2nix then sees git-source entries, not
  path entries, and avoids the whole `projectRoot = workspaceRoot
  + localPath` codepath that broke us in v1.
- **Two separate uv workspaces** — `preload/` and `augment/` —
  each with its own `pyproject.toml` + `uv.lock`. Models the
  distributed setting we ultimately want; deliberately *does
  not* share a workspace just because both consume fxtr2.
- **One flake.nix at the prototype root**, with one `flake.lock`.
  The flake's `outputs` import two helper nix modules
  (`nix/preload.nix`, `nix/augment.nix`) that each
  `loadWorkspace` their own subdir and emit `{ venv, derivation }`.
  Augment takes preload's derivation as a `buildInput`.
- **`fake_remote/` and `demo/` are isolated** in their own subdirs
  with their own pyproject + uv.lock. They are *not* nix-built —
  they're just run via `uv run`. The reason for moving them out
  of the spike root is to keep concerns separate (and to make
  the root pyproject-less, since nix sees no python project at
  the root).

### Target layout

```
nix_fxtr_prototype/
├── flake.nix              # single flake, single flake.lock; imports per-workspace .nix files
├── flake.lock
├── lib/                   # shared nix_prototype_lib package (sibling path-dep)
│   ├── pyproject.toml
│   └── src/nix_prototype_lib/{__init__.py,entities.py,dataset.py}
├── fake_remote/           # uv workspace; own uv.lock; not nix-built
│   ├── pyproject.toml
│   └── src/...
├── preload/               # uv workspace; own uv.lock; nix-built
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── preload.nix        # { pkgs, lib, inputs, system } -> { venv, derivation }
│   └── src/...
├── augment/               # uv workspace; own uv.lock; nix-built
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── augment.nix        # { pkgs, lib, inputs, system, preloadDerivation } -> { venv, derivation }
│   └── src/...
└── demo/                  # uv workspace; own uv.lock; not nix-built
    ├── pyproject.toml
    └── demo.py
```

Each workspace is self-contained: its `pyproject.toml`, `uv.lock`,
source tree, and (if nix-built) its `*.nix` module live together.
The root `flake.nix` just imports `./preload/preload.nix` and
`./augment/augment.nix`.

### Shared `nix_prototype_lib` package (sibling path-dep)

The lib lives at `lib/` under the flake root and is consumed by
each workspace via `nix_prototype_lib = { path = "../lib", editable = true }`.

**Why this should work despite previous path-dep pain.** The v1
failure was specifically about uv2nix's `projectRoot = workspaceRoot
+ "/" + localPath` over-normalizing when localPath had enough `..`
to walk past the flake source root (fxtr v1 was four levels up from
`preload_flake/` and ended up resolving to `/`). Here, both the
workspace and the lib live *inside the same flake source*, so the
math is:

- workspaceRoot = `/nix/store/<hash>-source/preload`
- localPath = `../lib`
- projectRoot = `/nix/store/<hash>-source/preload/../lib`
  → normalizes to `/nix/store/<hash>-source/lib` ✓

One level of `..` stays inside the store path. The failure mode
doesn't apply.

**If this surprises us anyway**, fallbacks are:

- **Option F** — symlink `_shared_lib -> ../lib` inside each
  workspace and declare `path = "./_shared_lib"`. Recorded localPath
  has no `..`. Tests uv's symlink behavior.
- **Option I** — share a single `entities.py` file (not a package)
  via symlink into each workspace's source tree. Avoids all path-dep
  machinery; each process imports its own module instance.
- **Option B** — single uv workspace at the flake root with two
  members. Loses the "two separate lockfiles" preference but is
  known-working.

`nix_prototype_lib` exposes the shared `LinkedNode` (with
`NS_UUID = 9c2d4e6f-1a8b-4c5d-9e7f-3b6a2c1d4e5f`) and
`build_demo_dataset(scope) -> FxtrID`. fake_remote, preload,
augment, and demo all import from it.

### `pyproject.toml` shape (each workspace)

```toml
[tool.uv.sources]
fxtr2 = { git = "https://github.com/TransluceAI/fxtr", branch = "push-uxypqqlzwtrk", subdirectory = "v2" }
fxtr  = { git = "https://github.com/TransluceAI/fxtr", branch = "push-uxypqqlzwtrk" }
```

uv lock resolves the branch to a SHA and pins it. To pick up new
commits on that branch:
`uv lock --upgrade-package fxtr2 --upgrade-package fxtr`.

### Auth for the private fxtr github repo

The `TransluceAI/fxtr` repo is private. Both uv (at lock time) and
nix (at build time) need to fetch from it. We use the same plain
HTTPS URL for both; auth comes from local config that lives
**outside** the spike and is **never committed**.

- **For uv (local lock)**: relies on the user's git credential
  helper (e.g., `osxkeychain` on macOS). One-time setup is to run
  `git ls-remote https://github.com/TransluceAI/fxtr.git` and enter
  GitHub username + PAT (as password) when prompted — the keychain
  caches it. uv shells out to git, picks up the cached creds.
- **For nix (build-time fetch)**: a PAT in `~/.config/nix/nix.conf`
  under `access-tokens = github.com=<PAT>`. nix's `builtins.fetchGit`
  / `fetchTree` reads this and authenticates with github for
  `https://github.com/...` URLs.

The same PAT can serve both — fine-grained, scope `Contents: read`
on the repo, no organization-level perms needed. Token never enters
the spike directory; it's purely user-local config.

Spike `README.md` will document the dance. We picked this over
ssh-URL-based auth because nix builds run in a sandbox that doesn't
have access to `~/.ssh/`, making ssh URLs effectively unusable for
nix fetches.

### `flake.nix` shape

```nix
{
  inputs = { nixpkgs, pyproject-nix, uv2nix, pyproject-build-systems, ... };
  outputs = inputs@{ self, nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      mkPkgs = system: nixpkgs.legacyPackages.${system};
    in {
      packages = forAllSystems (system:
        let
          pkgs = mkPkgs system;
          preload = import ./preload/preload.nix { inherit pkgs system inputs; };
          augment = import ./augment/augment.nix {
            inherit pkgs system inputs;
            preloadDerivation = preload.derivation;
          };
        in {
          preload = preload.derivation;
          preload-venv = preload.venv;
          augment = augment.derivation;
          augment-venv = augment.venv;
        }
      );
    };
}
```

Each workspace's `*.nix` module does its own
`uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; }`
(loading the workspace it lives in), builds its venv with
`mkVirtualEnv`, and emits a stdenv derivation. Augment's module
takes the preload derivation as an argument.

### Open verification points

These are the things we should expect to learn during
implementation:

1. **Does uv2nix actually handle git sources with `subdirectory`?**
   uv.lock records git sources cleanly; uv2nix is designed to
   consume uv.lock and *should* pass git sources through to nix's
   fetcher. Expect this works; verify on first nix build.
2. **Does nix's fetcher need credentials for the github repo?**
   It's a public repo, so no. But pure-eval evaluation will still
   need network access via the standard nix fetch mechanism (which
   it has).
3. **fxtr2's dep on fxtr v1** — fxtr2's pyproject declares
   `fxtr = { path = "../" }` (or similar). When uv installs fxtr2
   from a git source with `subdirectory = "v2"`, that path dep
   resolves to a sibling-of-`v2` inside the fetched git tree —
   which *is* fxtr v1 (same repo). Should work, but worth
   confirming.
4. **fxtr v1's editable-vs-built shape** under a git source. Editable
   path-deps installed via git become regular non-editable
   installs. Hopefully fine for our use; fxtr v1 doesn't do anything
   unusual.

### Implementation steps (Plan v2)

- [x] V2.1 — **Update this plan doc** with the Plan v2 section.
- [x] V2.2 — **Wipe the spike's current contents.**
- [x] V2.3 — **Create the four workspaces** with pyproject +
      minimal src/ trees.
- [x] V2.4 — **Run `uv lock` in each workspace.** Lockfiles record
      git revisions cleanly. nix_prototype_lib path-dep recorded as
      `editable = "../lib"` — one-`..`, no over-normalization.
- [x] V2.5 — **Implement Python source** for each workspace.
- [x] V2.6 — **Write `flake.nix` + `preload/preload.nix`; nix build
      `.#preload`.** Required: `[tool.uv.extra-build-dependencies]`
      hints for cbrrr/sqlitedict/mashumaro/fxtr/fxtr2; SSL_CERT_FILE
      in the FOD derivation for httpx. Final preload hash baked in
      after fakeHash refresh.
- [x] V2.7 — **Write `augment/augment.nix`; nix build `.#augment`.**
      First try succeeded after V2.6's pyproject fixes propagated
      to augment.
- [x] V2.8 — **Implement `demo/demo.py`.** Full end-to-end run
      passes; chain walks to `["new_head", "x", "y", "b", "c", "d",
      "e"]`.
- [x] V2.9 — **`README.md`** at the prototype root.
- [x] V2.10 — **Capture findings v2** (above).
- [x] V2.11 — Update CODE_OVERVIEW.md in the fxtr repo.

The original step list (1–10) further down is superseded; it's
preserved as the historical record of the path-dep-based approach.

---

## Status

**Exploratory prototype.** The intent is a hands-on, working demonstration —
not permanent tooling. By the end we'll have running code we can poke at,
plus a writeup of what worked / what was awkward, feeding into a future
"real tooling" stage that introduces `fxtr_imports.toml` + a reusable nix
helper. This document is detailed enough to implement straight through.

## Goal

Stand up an end-to-end demonstration that:

- A **fake remote entity store** (HTTP) is serving a known dataset.
- A **preload flake** with a fixed-output derivation (FOD) reaches the
  remote, downloads a *subset* of the entities, and emits a layered file
  entity store. Other entities are recorded as `mark_as_remote`.
- An **augment flake** with a pure (sandboxed) derivation takes the
  preload's output as a parent layer, loads what it can reach, builds
  new entities that extend the original data, and emits a new layered
  store with the augmented data pinned. References back to the
  un-preloaded entities ride through to the preload layer's
  `mark_as_remote` precomp files.
- A **demo runner** (`demo.py`) orchestrates the whole flow and reads
  the final augment-flake output using a *different* remote (an
  in-memory `DeepFetchable` wrapping a fresh scope holding the same
  dataset). Proves the layered store's remote-of-record is dynamic at
  read time.

Together this exercises: build entities → ship over HTTP → preload into
a nix store path → augment in a sandboxed build → resolve the
remote-tagged tail through a *different* remote at read time. That's
the full fxtr/nix interop loop.

## Where this lives

```
examples/nix_prototype/
├── README.md                 # how to run, what to look for
├── lib/                      # shared Python: entity types + helpers
│   ├── pyproject.toml
│   ├── uv.lock
│   └── nix_prototype_lib/
│       ├── __init__.py
│       ├── entities.py       # LinkedNode definition + namespace UUID
│       └── dataset.py        # build_demo_dataset() — populates a scope
├── fake_remote/              # the HTTP server
│   ├── pyproject.toml        # depends on `nix_prototype_lib` (path)
│   ├── uv.lock
│   └── serve.py
├── preload_flake/            # FOD that downloads a subset
│   ├── flake.nix
│   ├── pyproject.toml        # depends on `nix_prototype_lib` (path)
│   ├── uv.lock
│   └── src/
│       └── preload.py        # the FOD's builder script
├── augment_flake/            # pure derivation that extends entities
│   ├── flake.nix
│   ├── pyproject.toml        # depends on `nix_prototype_lib` (path)
│   ├── uv.lock
│   └── src/
│       └── augment.py        # the pure derivation's builder script
└── demo.py                   # end-to-end runner
```

Each `pyproject.toml` is its own uv workspace. Cross-references to
`nix_prototype_lib` use uv path dependencies (`tool.uv.sources`). Each
flake uses uv2nix (modeled after `2026_05_15_nix_spike/sample-app/`) to
build a venv that has fxtr2 + `nix_prototype_lib` + the workload script
on `PATH`.

The example is **not** wired into CI. Run-by-hand only, with nix
installed. The `README.md` walks through invocation.

## Concrete demo content

A **linked list** structure. One entity type:

```python
NS_UUID = uuid.UUID("9c2d4e6f-1a8b-4c5d-9e7f-3b6a2c1d4e5f")  # pinned

@dataclass(frozen=True)
class LinkedNode(DataclassEntity, fxtr_type=define_type(NS_UUID, "linked_node")):
    label: str
    next: EntityRef[LinkedNode] | None  # weak ref — fine for this demo
```

`dataset.py::build_demo_dataset()` builds a 5-node chain
``a → b → c → d → e`` in a given scope, returning the head id (`a`).

**Preload FOD**: preloads `a` and `b` as **data**, marks `c` as
**remote** (precomp file only, no data). Does *not* touch `d` or `e`.
At read time, the layered-store arena's "known-remote" set kicks in:
loading `c` (precomp-marked) goes to the remote, which returns `c`'s
deps including `d`; `d` joins the known-remote set transitively;
loading `d` then goes to the remote and pulls `e` in the same way. So
the preload layer encodes "I supply a/b directly; everything past
that is the remote's job."

Pins in the preload layer:
- `"head"` → id of `a`.

**Augment derivation**: extends the chain by prepending two new nodes
and joining partway in. Specifically:

- Opens a `LayeredFileStoreReader` on the preload output. No remote —
  the sandbox has no network.
- Retrieves `"head"` (id of `a`). Loads `a` → gets its data,
  including its `next` field (an `EntityRef[LinkedNode]` for `b`).
- Stores two new `LinkedNode` entities, joining the new chain at `b`
  (not `a`!) — i.e., we skip `a` and connect into the middle:

  ```python
  y = scope.store(LinkedNode(label="y", next=a_loaded.next))  # y.next = b
  x = scope.store(LinkedNode(label="x", next=y))
  new_head = scope.store(LinkedNode(label="new_head", next=x))
  ```

- Writes a new layer with the preload output's nix store path as
  parent. Pins:
  - `"head"` → `new_head.entity_id`. Walk persists `new_head`, `x`,
    `y` as data; finds `b`'s id already covered in the parent layer
    and stops.
  - `"original_head"` → `a.entity_id`. Walk finds `a` already covered
    in the parent; records pin only.

**Demo runner** opens the augment output with an in-memory
`StubDeepFetchable` wrapping a fresh dataset scope (the "second
remote"). It then:

- Retrieves `"head"`, walks the full chain, asserts the labels are
  `["new_head", "x", "y", "b", "c", "d", "e"]`. This proves:
  - `new_head`, `x`, `y` came from the augment layer (data files
    there).
  - `b` came from the preload layer (data file there).
  - `c` came from the remote (it had only a precomp file in preload).
  - `d` and `e` came from the remote transitively (no precomp file
    anywhere for them; they joined the known-remote set after `c`'s
    remote load).
- Retrieves `"original_head"`, loads `a`, asserts label = `"a"`.
- Pretty-prints the chain.

## Implementation pieces

### `lib/` — shared library

- `nix_prototype_lib/entities.py`: define `LinkedNode` and
  `NS_UUID = uuid.UUID("…")` (one-off, generated). Types are
  registered globally at class-definition time via
  `fxtr_type=define_type(NS_UUID, "linked_node")` (the standard
  pattern from `examples/simple_store_and_load.py`) — no
  per-scope setup needed.

- `nix_prototype_lib/dataset.py::build_demo_dataset(scope) -> FxtrID`:
  builds and stores the 5-node chain in `scope`'s active arena;
  returns the head's id.

  ```python
  def build_demo_dataset(scope: FxtrScope) -> FxtrID:
      e = scope.store(LinkedNode(label="e", next=None))
      d = scope.store(LinkedNode(label="d", next=e))
      c = scope.store(LinkedNode(label="c", next=d))
      b = scope.store(LinkedNode(label="b", next=c))
      a = scope.store(LinkedNode(label="a", next=b))
      return a.entity_id
  ```

### `fake_remote/serve.py` — HTTP server

A tiny script that:
1. Opens a `FxtrScope`.
2. Calls `build_demo_dataset(scope)`.
3. Pins the head as `"head"` in an in-memory pins dict.
4. Calls `create_fxtr_app(scope, pins)` (from `fxtr2.experimental.http.asgi_app`).
5. Serves on a hardcoded port (e.g., `8765`) via Hypercorn.

CLI: `python serve.py [--port 8765]`. Sleeps until interrupted.

### `preload_flake/` — fixed-output derivation

The `flake.nix` follows the uv2nix pattern in the spike. Provides:

- `packages.<system>.venv`: the uv2nix-built venv with fxtr2 +
  `nix_prototype_lib` + a `preload` entrypoint.
- `packages.<system>.default`: the FOD itself. `stdenv.mkDerivation`
  with:
  - `outputHashMode = "recursive"`
  - `outputHashAlgo = "sha256"`
  - `outputHash = lib.fakeHash` (first build prints the real hash; user
    pastes it back).
  - `buildCommand = "${venv}/bin/preload ${remoteUrl} $out"`.
  - `__noChroot = false` (default); network access *is* allowed because
    it's an FOD.

The remote URL is **hardcoded** in the flake (e.g.,
`http://localhost:8765`). Demo runner is responsible for starting
`fake_remote/serve.py` on that port before invoking `nix build`. If
the port is busy, the demo fails loudly — fine for a prototype.

`src/preload.py` (the `preload` entrypoint):

```python
import anyio, httpx, sys
from fxtr2.experimental.http.client import HttpFxtrReadNamespace
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from nix_prototype_lib.entities import LinkedNode

async def main(url: str, out_dir: str):
    async with httpx.AsyncClient(base_url=url, timeout=120) as client:
        async with HttpFxtrReadNamespace(client) as http_ns:
            head_ref = await http_ns.retrieve("head", LinkedNode)

            async with LayeredFileStoreIsolatedWriter(
                out_dir,
                parents=[],
                remote=http_ns.as_deep_fetch_source(),
            ) as writer:
                # Mark the head as remote (writes precomp/a), pin it
                # by name (fast path — precomp covers it).
                await writer.mark_as_remote(head_ref.entity_id)
                await writer.pin_to_this_layer("head", head_ref.entity_id)

                # Preload twice. Each `preload(ref)` returns the
                # decoded entity AND writes data + precomp for the
                # uncovered next-hop as a side effect:
                # - preload(head_ref) -> a: writes data/a, deletes
                #   precomp/a, writes precomp/b (b is a's uncovered
                #   dep). Returns the LinkedNode for a.
                # - preload(a.next) -> b: writes data/b, deletes
                #   precomp/b, writes precomp/c (c is b's uncovered
                #   dep). Returns the LinkedNode for b. The
                #   transitive-attach machinery means we never
                #   explicitly mark_as_remote on b — it became
                #   reachable via the wiretap when a was loaded.
                a = await writer.preload(head_ref)
                _b = await writer.preload(a.next)

                # Net on-disk state: data/a, data/b, precomp/c, pin
                # "head" → a. Nodes d and e have no record here at
                # all; at read time, fetching c from a configured
                # remote pulls in d transitively (and d's load pulls
                # in e), per the layered arena's known-remote
                # expansion.

if __name__ == "__main__":
    anyio.run(main, sys.argv[1], sys.argv[2])
```

### `augment_flake/` — pure derivation

`flake.nix` declares the preload flake as a flake input:

```nix
inputs.preload-flake = { url = "path:../preload_flake"; ... };
```

Provides:
- `packages.<system>.venv`: uv2nix-built venv with fxtr2 +
  `nix_prototype_lib` + an `augment` entrypoint.
- `packages.<system>.default`: the pure derivation.
  - `stdenv.mkDerivation` (no `outputHash` — content-addressed input).
  - `buildInputs = [ preload-flake.packages.${system}.default ]` (the
    preload layer's store path becomes an input).
  - `buildCommand = "${venv}/bin/augment $preloadLayerPath $out"`,
    where `$preloadLayerPath` is the input path.
  - Sandboxed; no network. `LayeredFileStoreReader` with `remote=None`
    is enough.

`src/augment.py` (the `augment` entrypoint):

```python
import anyio, sys
from pathlib import Path
from fxtr2.data.layered_file_store.reader import LayeredFileStoreReader
from fxtr2.data.layered_file_store.writer import LayeredFileStoreIsolatedWriter
from nix_prototype_lib.entities import LinkedNode

async def main(preload_path: str, out_dir: str):
    async with LayeredFileStoreReader(preload_path) as reader:
        head_ref = await reader.retrieve("head", LinkedNode)
        original_head_id = head_ref.entity_id

        # Load `a` (we need its `next` field to join the new chain at
        # `b` instead of `a`).
        a_loaded = await head_ref.load()
        assert a_loaded.next is not None, "demo expects a non-trivial chain"
        # a_loaded.next is an EntityRef[LinkedNode] for `b`.

        # Build the new prefix; y points at b (skipping a).
        # `scope.store(entity)` returns an `EntityRef[T]`, so we can
        # use the returned ref directly as the `next` field on the
        # following node — no need for any conversion.
        scope = reader.scope
        y = scope.store(LinkedNode(label="y", next=a_loaded.next))
        x = scope.store(LinkedNode(label="x", next=y))
        new_head = scope.store(LinkedNode(label="new_head", next=x))

        async with LayeredFileStoreIsolatedWriter(
            out_dir,
            parents=[Path(preload_path).resolve()],
        ) as writer:
            # new_head/x/y are fresh entities — pass scope so the walk
            # can copy their data into this layer.
            await writer.pin_to_this_layer("head", new_head.entity_id, scope)
            # original_head (a) is already covered in the parent layer's
            # data (preload wrote data/a). Fast-path pin: no scope
            # needed.
            await writer.pin_to_this_layer("original_head", original_head_id)

if __name__ == "__main__":
    anyio.run(main, sys.argv[1], sys.argv[2])
```


### `demo.py` — end-to-end runner

```python
async def main():
    # 1. Build the dataset in an in-memory scope. We'll use this both
    #    for the HTTP server (during nix builds) and as the "second
    #    remote" at read time (no HTTP involved).
    async with FxtrScope(name="dataset") as dataset_scope:
        head_id = build_demo_dataset(dataset_scope)
        pins = {"head": head_id}

        # 2. Start fake_remote in the background, then drive nix builds.
        async with serving_context(dataset_scope, pins, port=8765):
            preload_path = await run_nix_build(
                "examples/nix_prototype/preload_flake"
            )
            augment_path = await run_nix_build(
                "examples/nix_prototype/augment_flake"
            )
        # `serving_context` exits → HTTP server shuts down.

        # 3. Configure a *different* remote: an in-memory scope wrapped
        #    as a DeepFetchable via `_deep_fetch`. This proves the
        #    layered store doesn't bake in the remote endpoint.
        remote = ScopeDeepFetchable(dataset_scope)
        async with LayeredFileStoreReader(
            augment_path,
            remote=(remote, remote.lease()),
        ) as reader:
            head_ref = await reader.retrieve("head", LinkedNode)

            labels: list[str] = []
            current: LinkedNode | None = await head_ref.load()
            while current is not None:
                labels.append(current.label)
                if current.next is None:
                    break
                current = await current.next.load()
            print("Loaded chain:", " → ".join(labels))
            assert labels == ["new_head", "x", "y", "b", "c", "d", "e"]
```

`ScopeDeepFetchable` is a small helper local to `demo.py` (we don't
re-import the one in `tests/test_layered_file_store/conftest.py`).
Body is just:

```python
class ScopeDeepFetchable(DeepFetchable[Any]):
    """Wraps a FxtrScope as a DeepFetchable via its `_deep_fetch`.
    Internal API use is fine here — this is a demo, not library code."""
    def __init__(self, scope: FxtrScope):
        self._scope = scope
    def lease(self) -> Lease[Any]:
        return self._scope._main_lease.clone()
    async def try_lookup(self, node_id):
        return await self._scope._deep_fetch(node_id)
```

`run_nix_build(flake_path)` calls
`nix build <flake_path>#default --print-out-paths --no-link` via
`anyio.run_process` (per the project convention of using anyio's
APIs over asyncio). `--print-out-paths` emits the output store path
to stdout (no parsing needed); `--no-link` skips creating a `result`
symlink so the demo doesn't leave artifacts in the working dir.
Returns the captured store-path string (or raises on non-zero exit).

## Implementation steps

Build incrementally so uv2nix / nix problems surface in isolation
rather than tangled with everything else. Each step ends with a
hands-on verification before moving on.

- [ ] 1. **Scaffold `examples/nix_prototype/`** with the directory
  layout above. NS_UUID is already pinned in this plan as
  `9c2d4e6f-1a8b-4c5d-9e7f-3b6a2c1d4e5f`; copy it verbatim into
  `entities.py`. Initialize each `pyproject.toml` + `uv.lock`
  (uv add `fxtr2` as a path dep to the v2 source tree; consumer
  pyprojects also add `nix_prototype_lib` as a path dep).
- [ ] 2. **Implement `lib/nix_prototype_lib/`** — entities (using
  `define_type(NS_UUID, ...)`) + `build_demo_dataset`. Verify by
  hand: `uv run python -c "from nix_prototype_lib.entities import
  LinkedNode; ..."`.
- [ ] 3. **Implement `fake_remote/serve.py`** — tiny Hypercorn
  server. Verify standalone: `python serve.py` + `curl
  http://localhost:8765/pin/head`.
- [ ] 4. **Implement `preload_flake/`** — uv2nix-managed `flake.nix`
  modeled after the spike, plus `preload.py`. First-build dance to
  resolve `fakeHash`. Verify standalone: start fake_remote in one
  terminal, run `nix build .#default` in another, inspect output
  with `tree $(readlink result)/`. This is where we expect to
  discover any uv2nix path-dep wrinkles (see open question on
  fxtr2 path deps); budget exploration time.
- [ ] 5. **Implement `demo.py` (partial)** + start orchestrating the
  preload flake from it. The demo starts fake_remote via
  `serving_context`, then calls `run_nix_build(...)` for the
  preload flake, captures the store path. Verify the captured path
  contains the expected layered store layout. No augment yet.
- [ ] 6. **Implement `augment_flake/`** — declares preload as a flake
  input (`inputs.preload-flake.url = "path:../preload_flake"`).
  `augment.py` does the load + build + write. Includes the
  macOS-sandbox network-ping diagnostic (a one-line
  `socket.create_connection(("1.1.1.1", 53), timeout=0.5)` probe
  that prints a warning if the connection succeeds — visible
  signal that sandbox isolation didn't apply, important on macOS
  where it's permissive). Verify: `nix build .#default` in the
  augment flake produces a layered store dir referencing the
  preload's store path.
- [ ] 7. **Finish `demo.py`** — add the augment build, the
  in-memory second-remote read, and the chain-walk assertions.
- [ ] 8. **Write `README.md`** — how to run, what each piece does,
  what the user should see. Document:
  - The port-8765 requirement ("the port must be free at demo
    time; if not, edit fake_remote/serve.py and the flake URLs to
    pick a different one").
  - The first-build fakeHash dance.
  - macOS sandbox caveat (the augment script's diagnostic will
    print a warning if the sandbox didn't apply).
- [ ] 9. **Capture findings + open questions** at the bottom of this
  plan during/after implementation. Note anything unexpectedly
  clean or unexpectedly painful.
- [ ] 10. **Update `CODE_OVERVIEW.md`** to point at the example.

## Implementation-time open questions

1. **Where exactly does the preload script live?** As a `[project.scripts]`
   entry in `preload_flake/pyproject.toml`, or as a module path
   (`python -m ...`)? The uv2nix-built venv exposes script entry
   points on `PATH`, so a `[project.scripts]` entry is cleanest.

2. **How does the preload FOD construct the remote
   `(DeepFetchable, Lease)` pair?** Resolved: added
   `HttpFxtrReadNamespace.as_deep_fetch_source(self) -> tuple[DeepFetchable, Lease]`
   in part 1. Caller takes ownership of the returned lease clone, per
   the standard lease idiom.

3. **Cross-flake input wiring.** `inputs.preload-flake.url = "path:../preload_flake";`
   works for in-repo prototypes. The flake.lock file records the input
   hash. Verify this composes cleanly — particularly the fakeHash
   refresh story (does updating preload's fakeHash force a follow-up
   on augment's lock?). Expectation: yes, but we'll see.

4. **`uv2nix` and `tool.uv.sources` path deps.** `nix_prototype_lib` is
   pulled in via a relative path. Verify uv2nix handles path
   dependencies cleanly. If not, we may need to publish it via a flake
   output or duplicate the source into each consumer.

5. **fxtr2 itself.** The flake.nix declares `fxtr2` as a path
   dependency (relative path to the v2 source tree). uv2nix's
   `loadWorkspace` should handle path deps in `tool.uv.sources` —
   we'll verify during step 4. If awkward, we can install fxtr2 from a
   built sdist instead.

6. **Hardcoded port collision.** Port 8765 is arbitrary. If the user's
   machine has it bound, the demo fails. We'll print a clear error
   message and let the user pick another port via env var. Not worth
   doing smart port selection for the prototype.

7. **MacOS sandbox.** On macOS, nix's sandbox is less strict by
   default — the pure derivation isn't reliably prevented from
   reaching the network. The demo still works (the augment script
   doesn't try to reach out); but it doesn't *prove* sandbox
   isolation on macOS. Mitigations:
   - The augment script optionally probes the network at start-up
     and prints a warning if the build can reach the internet (use
     `socket.create_connection(("1.1.1.1", 53), timeout=0.5)` as a
     cheap test). This makes the lack of isolation visible without
     failing the build.
   - Future: containerize the demo (Docker / podman) for true sandbox
     enforcement on macOS. Out of scope for this stage; flagging for
     follow-up.
   - Linux gives the real demonstration today.

8. **What if `nix build` itself fails?** The demo's
   `run_nix_build` should surface stderr. Common failure: fakeHash
   needs updating after the first run; the build fails with a
   "expected/got" diff, the user updates the flake, and re-runs.

## Findings v2 (Plan v2 implementation, in progress)

Captured while building out the Plan v2 spike. The original
"Capture findings" section below is from the abandoned v1 attempt
and is left for historical context. See also
[part2_nix_exploration_findings.md](./part2_nix_exploration_findings.md)
for the full v1 retrospective.

### What worked first try

- **Git-source resolution.** Declaring `fxtr2` and `fxtr` in each
  workspace's `[tool.uv.sources]` as `{ git = "https://github.com/TransluceAI/fxtr", branch = "...", subdirectory = "v2" }`
  worked cleanly. uv lock resolved the branch to a SHA and pinned
  it; uv2nix consumed those git sources without complaint at build
  time. No `subdirectory`-related quirks.
- **Sibling `nix_prototype_lib` path-dep.** Declaring it as
  `{ path = "../lib", editable = true }` from each consumer
  workspace worked. uv lock recorded `editable = "../lib"`. uv2nix
  resolved it during build to the sibling of the workspace inside
  the flake source — the one-`..` path stayed inside the store
  path, no over-normalization. The original "out-of-workspace path
  deps break" hypothesis was incorrect when the path stays inside
  the flake source.
- **No fake-`.git` interference.** Moving the spike outside the
  fxtr repo (to `~/extgit/spikes/`) avoids nix's auto-detection of
  the fxtr root's `.git/` marker. Plain `nix build path:$(pwd)#...`
  works.

### Auth setup that we landed on

- nix uses `~/.config/nix/nix.conf`'s `access-tokens = github.com=<PAT>`
  for fetching the private fxtr repo via `builtins.fetchGit`.
- uv uses the local git credential helper (osxkeychain on macOS).
  One-time `git ls-remote https://github.com/TransluceAI/fxtr.git`
  with PAT-as-password warms the keychain; uv inherits it.
- Same PAT can serve both; never enters the spike directory.

We rejected ssh URLs because the nix sandbox can't access `~/.ssh/`.

### Whack-a-mole: missing build-system deps

uv2nix's per-package build environments don't reliably get the
build deps declared in each sdist's `build-system.requires`. We
encountered the same pattern repeatedly: `ModuleNotFoundError`
for `setuptools` (for sdists like `cbrrr`, `sqlitedict`,
`mashumaro`) or `hatchling` (for the git-source builds of `fxtr`
and `fxtr2`). The fix is to add hints in the consuming workspace:

```toml
[tool.uv.extra-build-dependencies]
cbrrr      = ["setuptools"]
sqlitedict = ["setuptools"]
mashumaro  = ["setuptools"]
fxtr       = ["hatchling"]
fxtr2      = ["hatchling"]
```

(List grows as we hit each failure during the build. Documented in
the spike's pyprojects + README.)

This is brittle: every new sdist or git-source dep added upstream
(in fxtr2's `requires-dist`) could trigger another whack-a-mole
cycle. Worth raising with uv2nix upstream eventually — there may be
a more idiomatic way to declare "every package's
`build-system.requires` should actually be installed in its build
env" that we're missing.

### FOD-specific surprises

- **httpx + SSL_CERT_FILE in the sandbox.** httpx's `AsyncClient`
  eagerly constructs an SSL context at instantiation time, which
  reads `os.environ["SSL_CERT_FILE"]`. In the nix build sandbox
  that env var is missing → `FileNotFoundError` before the client
  even makes a request. Fix: set
  `SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"`
  in the derivation and add `pkgs.cacert` to `buildInputs`. Note
  we're talking to localhost over plain HTTP — the cert bundle is
  only there so httpx's eager-context construction doesn't blow up.
- **fakeHash dance worked exactly as expected.** First build
  reported the real hash; pasting it in produced a reproducible
  output. Output is stable across rebuilds (the layered store
  writer doesn't embed timestamps or other nondeterminism).

### Augment-specific surprises

- **Sandboxed augment build worked first try.** Reads the preload
  derivation's store path as a parent layer, builds new entities
  in a `LayeredFileStoreReader.scope`, writes them out via
  `LayeredFileStoreIsolatedWriter`. No network access needed since
  un-preloaded entities aren't traversed during the augment walk
  (only the head + its `next` field are loaded, both of which are
  covered by the preload layer).
- **macOS sandbox is permissive**, as expected. The probe at
  startup detects the leak; the build still succeeds because the
  workload doesn't actually try to use the network.

### End-to-end demo: works

Final state — `cd demo && uv run python demo.py`:

```
demo: fake_remote: serving on http://127.0.0.1:8765
demo: nix build .#preload
demo: preload output: /nix/store/.../-fxtr-preload-layer
demo: nix build .#augment
demo: augment output: /nix/store/.../-fxtr-augment-layer
demo: fake_remote: stopped

new_head → x → y → b → c → d → e

✓ chain matches expected ['new_head', 'x', 'y', 'b', 'c', 'd', 'e']
```

This proves the full loop:
- `new_head`, `x`, `y` came from the augment layer (data files there).
- `b` came from the preload layer (preloaded data).
- `c`, `d`, `e` came from a *different* in-memory remote configured
  at read time — the layered store's remote isn't baked into the
  layer files. (`c` had a precomp record in the preload layer;
  `d`, `e` joined the known-remote set transitively as `c` loaded.)

### Things to revisit later

- **uv2nix + extra-build-dependencies** is brittle whack-a-mole. A
  cleaner pattern (if any exists upstream) would help.
- **Single-flake with two uv workspaces** is working but unusual. The
  pattern feels right for a "distributed environments share a flake
  but not a lockfile" story. Worth comparing to a future "single uv
  workspace at root" simplification once we know if/when we want it.
- **Iteration loop on fxtr changes** is friction-heavy (commit, push,
  bump SHA in each spike pyproject, `uv lock --upgrade-package` ×
  N). Tolerable for a prototype; for future tooling we'd want
  ergonomic options for active-dev mode.
- **Hardcoded fakeHash refresh** is annoying. Future tooling could
  drive the dance automatically, or skip FOD-based preloads for
  workloads where IFD-based fetching is OK.

## Capture findings (v1 / historical)

_(Original section from the v1 plan; left intact. v1 work
described in
[part2_nix_exploration_findings.md](./part2_nix_exploration_findings.md).)_

## Out of scope

- `fxtr_imports.toml` / `.lock` design — future stage.
- A reusable `mkFxtrLayerDerivation` nix helper function — write ad-hoc
  for the prototype; abstract only when we know what shape it wants.
- Provenance round-tripping — separate concern; layered store is
  already bare-data-only by design.
- CI integration — no nix on CI for now.
- Failure-case demos (no remote, missing remote, network sandbox
  violation) — happy path only; mention these in the README as things
  the system supports.
