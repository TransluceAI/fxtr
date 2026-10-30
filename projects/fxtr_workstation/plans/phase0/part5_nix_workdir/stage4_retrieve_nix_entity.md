# Stage 4 — `retrieve_nix_entity` + MCP rewrite

Add the sandbox-side builtin that bridges nix-built **DATA entities**
into scripts, add the `current_commit()` builtin, wire up the MCP-side
on-demand build plumbing, and retire `prepare_package_bundle` from the
agent surface.

> **Revised (2026-05-30):** `retrieve_nix_entity` is now a DATA-only
> affordance. Services reach the spawner as git-referenced
> `NixWasmServiceDefn`s (Stage 3), **not** as retrieved entities — so
> the demo's spawn flow constructs the defn by hand rather than
> retrieving a bundle entity.

## Design decisions (pinned 2026-05-31)

These resolve the open questions in `04_retrieve_nix_entity.md` /
`08_build_trigger.md` / `11_failure_and_ux.md` for the v0 implementation.
A few **deviate** from the design notes; the deviations are intentional
and listed under "Deviations" at the bottom.

1. **Return shape: bare `FxtrID`, no `result_type` (yet).** The signature
   is `retrieve_nix_entity(name, *, nix_location="fxtr.entities")` and it
   always returns a bare `FxtrID`, exactly like `lookup_entity`. The agent
   wraps it with `await EntityRef(fid, T).load()` to read data. The
   `result_type`/mashumaro-decode overload from note 04 is dropped for now
   (note 04's `EntityRef[Any]` return was never a real option — it's a
   doc artifact); it can be added later as a separate change. `nix_location`
   is **keyword-only** for future flexibility.

2. **No whitelist on `nix_location`.** `retrieve_nix_entity` resolves
   whatever attr the caller names (`<nix_location>.<name>`), as long as it
   evaluates to an **entity-shaped** attrset (`{ type, layer, layerKey }`).
   Shape is validated by the metadata eval (see below); a non-entity attr
   fails the eval and surfaces as a build/eval error. (Replaces note 04's
   `"fxtr.entities"`-only whitelist.)

3. **Per-call eval + build, no structure cache.** Each
   `retrieve_nix_entity` call runs its own `nix eval` of the one attr's
   metadata + `nix build` of its `.layer`. No per-script structure map is
   cached; we trust nix's own eval cache (the stance of note 08). (Replaces
   note 04's "structure map cached for the script lifetime".)

4. **No `.tmp_outputs/` stderr dump.** `NixEntityBuildFailed` is raised
   *inside the script* with the **full** nix stderr in its message; it
   flows through the existing `_format_script_error` compression like any
   script exception (inline shows only the message's first line; the full
   text is retained for `get_full_exception`). The file-on-disk dump
   machinery from notes 04/11 is **dropped** for Stage 4 and deferred to a
   follow-up (it was overly complicated for what the existing in-script
   exception path already gives us).

5. **`current_commit()` errors are normal Python exceptions.** Dirty tree
   (any uncommitted change *including untracked files*) → a dedicated
   `WorkingTreeDirty`; not-a-git-repo → a plain error. Both raised
   in-script (re-raised by the builtin from the host's IRException).

6. **Demo data entity uses `mkEntityFromPython` over a full-`fxtr2` host
   venv.** The intended public API surface. A tiny new
   `packages/demo_data/` package (own `pyproject.toml`, host-runnable —
   *not* the wasm32-pinned resolver `hello_service` uses) exposes one
   entity object built from the existing `Note` type; the flake declares
   `fxtr.entities.demo_data` via `mkVenv` + `mkEntityFromPython`.

## Architecture recap (how it fits the existing code)

- **Sandbox builtins** live in
  `sandbox/fxtr_ws_mcp_sandbox_bridge/.../builtins.py`; they call host
  RPC methods declared on `McpHostProto` (`.../protocols.py`) and
  implemented by `McpHostImpl` (`fxtr_workstation_mcp/.../host_impl.py`).
- A returned `FxtrID` is bridged back into the sandbox by the fxtr-RPC
  return path (same as `lookup_entity`). So `retrieve_nix_entity`'s host
  method resolves a `FxtrID` *into `script_scope`* and returns it.
- MCP-side nix calls already live in `fxtr_workstation_mcp/.../nix_util.py`
  (`eval_flake_structure`, `build_attr`, typed `NixError` subclasses).
  Git calls are new.

## Implementation plan (chunks)

Each chunk is independently reviewable. Check off as completed.

### Chunk 1 — host-side nix + git helpers ✅

- [x] **`nix_util.py` (renamed from `nix.py`): switch to the bare workdir path.** Change `_flake_ref`
      from `f"path:{working_dir}#{attr}"` to the **bare** `f"{working_dir}#{attr}"`
      so nix auto-selects `git+file://` inside a repo (tracked-file edits
      live, untracked invisible) and `path:` otherwise — **non-frozen**
      (no `--frozen` / `--no-update-lock-file`; the data loop accepts a
      dirty tree + soft lock, unlike the spawner's code path). Applies to
      both the eval and `build_attr`.
- [x] **`nix_util.py`: add `eval_entity_metadata(working_dir, attr_path) ->
      EntityMetadata`.** Runs
      `nix eval --json <workdir>#<attr_path> --apply (ent: { inherit (ent) type layerKey; })`.
      The `--apply` accessing `.type`/`.layerKey` is what enforces the
      entity shape — a non-entity attr throws → non-zero exit → `NixEvalError`.
      Reuses the existing `EntityMetadata` dataclass. (Leave
      `eval_flake_structure` in place but unused by Stage 4 — it's harmless
      and may still be wanted; note it as possibly-dead in CODE_OVERVIEW.)
- [x] **New `fxtr_workstation_mcp/.../git_util.py`.** Small `anyio.run_process`
      wrappers, all read-only:
      - `async head_commit(working_dir) -> str` (`git rev-parse HEAD`).
      - `async is_git_repo(working_dir) -> bool`
        (`git rev-parse --is-inside-work-tree`).
      - `async is_dirty(working_dir) -> bool` (`git status --porcelain`
        non-empty — includes untracked).
      - `async untracked_count(working_dir) -> int`
        (`git ls-files --others --exclude-standard`, count lines).
      Typed exceptions (`NotAGitRepo`, `WorkingTreeDirty`) live here.

### Chunk 2 — `McpHostProto` + `McpHostImpl`: new RPCs, drop `prepare_package_bundle` ✅

- [x] **`McpHostProto`** (`protocols.py`): add
      - `async retrieve_nix_entity(name: str, nix_location: str) -> tuple[FxtrID, str]`
        — the resolved `FxtrID` plus the untracked-files notice (`""` when
        not in a git repo / no untracked files). The `FxtrID` bridges fine
        nested in the tuple; one round-trip, no separate notice RPC.
      - `async current_commit() -> str`
      Remove `prepare_package_bundle`.
- [x] **`McpHostImpl.retrieve_nix_entity`**:
      1. `meta = await nix.eval_entity_metadata(working_dir, f"{nix_location}.{name}")`.
      2. `store_path = await nix.build_attr(working_dir, f"{nix_location}.{name}.layer")`.
      3. `async with LayeredFileStoreReader(store_path) as reader:
         fid = await reader.retrieve(meta.layer_key, into_scope=self._script_scope, copy_into_scope=True)`.
      4. `return (fid, await self._workdir_untracked_notice())`. The reader
         copies the entity (+ its self-contained closure) into `script_scope`,
         so the RPC return bridge can lift it into the sandbox. Map errors:
         `NixError` subclasses →
         `IRException(category="NixEntityBuildFailed", message=...)` with the
         **full** nix stderr (no tail — `_compress_script_traceback` only
         shows the message's first line inline anyway, and the rest is kept
         for `get_full_exception`; truncating would lose the error since
         there's no `.tmp_outputs` dump); reader `KeyError` (missing
         `layerKey`) → `IRException(category="LookupError", ...)`.
         *(Watch item: if the cross-scope copy trips a type-descriptor
         closure miss, wrap in a transient sub-scope that also includes
         `GLOBAL_SCOPE`, mirroring the old `prepare_package_bundle`
         handling. Expected unnecessary — the layer is self-contained — but
         flagged.)*
- [x] **`McpHostImpl.current_commit`**: via `git_util.py` — error if not a repo,
      error `WorkingTreeDirty` if dirty, else return `head_commit`. Map to
      `IRException(category="WorkingTreeDirty" | "NotAGitRepo", ...)`.
- [x] **`McpHostImpl._workdir_untracked_notice`** (private helper, not on
      the proto): if `is_git_repo` and `untracked_count > 0`, return the
      count-only notice (`"note: N untracked file(s) are not visible to nix …"`);
      else `""`. Called inside `retrieve_nix_entity` to fill the tuple.
- [x] **Remove `prepare_package_bundle`** from `McpHostImpl`, plus
      `WORKSPACE_TRUST_ROOTS`, `_compute_workspace_trust_roots`, `_REPO_ROOT`,
      and the `from fxtr_sandboxing.deprecated.prepare import prepare` import.

### Chunk 3 — sandbox builtins: new builtins + exceptions, drop `prepare_package_bundle` ✅

- [x] **Exception classes** (in `protocols.py`, re-exported into
      `_fxtr_mcp_builtin`): `NixEntityBuildFailed(Exception)` and
      `WorkingTreeDirty(Exception)`, so agent scripts can `except` them.
- [x] **`retrieve_nix_entity(name, *, nix_location="fxtr.entities") -> FxtrID`**:
      validate `name`/`nix_location` are strings;
      `fid, notice = await host.retrieve_nix_entity(name, nix_location)`;
      `print(notice)` if non-empty (lands in captured stdout → fenced into
      the tool result) — guarded by a closure-local "already printed" flag
      so repeat retrievals don't spam; catch `IRException` and re-raise
      `NixEntityBuildFailed` / `LookupError` by `category`; return `fid`.
- [x] **`current_commit() -> str`**: `await host.current_commit()`; catch
      `IRException` → re-raise `WorkingTreeDirty` / `RuntimeError` by
      `category`.
- [x] **Remove `prepare_package_bundle`** builtin + its entry in the
      returned mapping + the module docstring bullet. Add `retrieve_nix_entity`,
      `current_commit`, `NixEntityBuildFailed`, `WorkingTreeDirty` to the
      mapping + docstring.

### Chunk 4 — purge `prepare_package_bundle` from docs / fixtures / tests ✅

- [x] `fxtr_workstation_mcp/.../sandbox_factory.py` — drop the
      `prepare_package_bundle` mention in the `packages/` projection comment.
      (Its own `prepare()` call for the MCP's sandbox-runtime dep closure is
      unrelated and stays.)
- [x] Delete `fxtr_workstation_mcp/tests/test_prepare_package_bundle.py`.
- [x] `examples/basic_test_demo/agent_workdir/`: remove `prepare_package_bundle`
      usage from `preamble.py`, `packages/demo_pkg/.../__init__.py`,
      `packages/hello_service/.../__init__.py`, and `README.md` (replaced the
      spawn walkthrough with the `current_commit()` + `NixWasmServiceDefn`
      flow; added a `retrieve_nix_entity("demo_data")` walkthrough).
- [x] `claude_plugin/skills/use_fxtr_workstation/SKILL.md` — removed the
      `prepare_package_bundle` references; documented `retrieve_nix_entity` +
      `current_commit` and rewrote the "Shipping code to a sandbox spawner"
      section to the git-ref flow.
- [x] **Stage-6 OUT-OF-DATE notes.** `examples/fxtr_workstation_demo/`
      (the assembly demo, migrates in Stage 6) still references
      `prepare_package_bundle` (`agent_workdir/README.md`,
      `agent_workdir/packages/delayed_echo/.../__init__.py`). It uses the
      same plugin, so the path is dead after this stage. Add a top-of-file
      note to those files:
      ```
      OUT OF DATE: prepare_package_bundle was removed in Stage 4; this demo
      migrates to the git-ref / retrieve_nix_entity flow in Stage 6. See
      projects/fxtr_workstation/plans/phase0/part5_nix_workdir/stage6_workstation_demo.md
      ```

### Chunk 5 — demo data entity (package + flake) ✅

- [x] **New `examples/basic_test_demo/agent_workdir/packages/demo_data/`.**
      `pyproject.toml` (uv_build) depending on `fxtr2` (full, host-runnable),
      mirroring `nix/py_testing`'s `[tool.uv.sources]` + `extra-build-dependencies`
      (incl. the `fxtr` v1 source + `hatchling` build-dep, since `fxtr2`
      depends on `fxtr` v1; uv only honors the resolution root's sources, so
      it must be redeclared) and the `# fxtr-lib-sub:` comment-flip
      convention `hello_service` uses. **No** wasm `environments` pin — this
      venv runs on the build host. `src/demo_data/__init__.py` defines a
      **self-contained** `DemoDataEntity` (its own namespace) and exposes
      `DEMO_DATA = DemoDataEntity(...).store(GLOBAL_SCOPE)` (stored so the
      writer's `GLOBAL_SCOPE` pin finds it). *Not* reusing `demo_pkg.Note`:
      `demo_pkg`'s `__init__` pulls the sandbox-only bridge, so it can't be
      imported from a host venv (Option A — agreed 2026-05-31).
- [x] **Flake** (`agent_workdir/flake.nix`): added
      ```nix
      fxtr.entities.demo_data = fxtr.lib.fxtr.mkEntityFromPython {
        venv = fxtr.lib.fxtr.mkVenv { name = "fxtr-demo-data-venv"; workspaceRoot = ./packages/demo_data; };
        fromPython = "demo_data:DEMO_DATA";
        layerKey = "demo_data";
      };
      ```
      (`mkEntityFromPython` returns the full `{ type, layer, layerKey }`
      attr.) **Validated**: `prepare_workdir_repo` + `nix build
      .#fxtr.entities.demo_data.layer` succeeds, producing a layered store.

### Chunk 6 — tests ✅

All green: the new `test_in_memory_e2e_retrieve_nix_entity` + the updated
spawn test pass, and the full non-slow suite (211 passed, 1 known skip)
plus the other slow scenarios are unaffected.

- [x] **Spawn flow self-serves the commit.** `test_in_memory_e2e_nix_spawn`
      now constructs `git_commit=await current_commit()` in-sandbox (clean
      committed tree → succeeds). `head` retained as `_head` (unused).
- [x] **Retrieve round-trip test** (`@pytest.mark.slow`,
      `test_in_memory_e2e_retrieve_nix_entity`): `retrieve_nix_entity("demo_data")`
      → `EntityRef(fid, DemoDataEntity).load()` → asserts the title. Confirms
      the layer built + bridged.
- [x] **Untracked-notice test**: writes an untracked file, re-retrieves, and
      asserts the `untracked file(s)` notice appears. (Folded into the same
      test.)
- [x] **`current_commit()` clean + dirty**: returns HEAD on the committed
      tree; raises `WorkingTreeDirty` once the untracked file is present.
      (Folded in.)
- [x] **`NixEntityBuildFailed` test**: `retrieve_nix_entity("does_not_exist")`
      surfaces `NixEntityBuildFailed`. (Folded in.)
- [x] **New `.gitignore`** in the agent_workdir for `.fxtr_workstation.toml`
      (+ `.tmp_outputs/`, `result*`, `.venv/`, `__pycache__/`) — required so
      the runtime toml doesn't make the tree dirty for `current_commit()` /
      visible to the untracked-notice. (Discovered during testing.)

### Chunk 7 — docs / overview ✅

- [x] Updated `fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`: replaced the
      "Sandbox-bundle construction" + trust-roots sections with a "Nix DATA
      entities (`retrieve_nix_entity` + `current_commit`)" section; updated
      the file listing (`nix_util.py` bare-path + `eval_entity_metadata`,
      new `git_util.py`, builtins/exceptions, the demo `.gitignore` +
      `demo_data` package + flake), and the "How tests work" section. Noted
      `eval_flake_structure` is currently unused.
- [x] No `nix/lib` helper-surface change (`mkEntityFromPython` already
      existed) — no docs there to update.

## Deviations from the design notes (for reviewers)

- **No `result_type`** on `retrieve_nix_entity` (note 04 described one) —
  bare `FxtrID` only for now.
- **No `nix_location` whitelist** (note 04) — any entity-shaped attr.
- **No structure-eval cache** (note 04) — per-call eval, trust nix's cache
  (note 08's stance wins).
- **No `.tmp_outputs/` dump** (notes 04/11) — the **full** nix stderr
  rides in the in-script `NixEntityBuildFailed` message (so
  `get_full_exception` retains it; the inline view is just the first line
  via `_compress_script_traceback`). No tail/truncation — without a dump,
  truncating would lose the error. Dump deferred to a follow-up.
- **Unknown-attr surfaces as `NixEntityBuildFailed`** (carrying nix's
  "attribute missing" stderr), not the clean `LookupError(name)` note 04
  imagined — a consequence of dropping the structure-eval. `LookupError`
  is still raised for a missing `layerKey` *inside* a built layer.

## Sequencing note

Mostly MCP-side wiring on top of Stage 1 (`mkEntityFromPython`) + Stage 3
(`mkWasmService` / `NixWasmServiceDefn` / `prepare_workdir_repo`). The
`retrieve_nix_entity` builtin works because the agent's flake now has a
buildable `fxtr.entities.demo_data` layer.

## Relevant design notes

- `design_notes/04_retrieve_nix_entity.md` — signature, per-script flow,
  error surface (note the Stage-4 deviations above).
- `design_notes/01_flake_schema.md` — entity attr shape; bare-path resolution.
- `design_notes/08_build_trigger.md` — lazy per-call build; trust nix's cache.
- `design_notes/11_failure_and_ux.md` — failure surfaces (`.tmp_outputs`
  deferred).

## Relevant existing code

- `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md` — `McpHostImpl`,
  sandbox bridge builtins, `script_scope` chain, FxtrID return bridge.
- `fxtr_workstation_mcp/.../nix_util.py` — existing eval/build wrappers + `NixError`s.
- `v2/fxtr2/data/layered_file_store/reader.py` — `LayeredFileStoreReader`
  (`retrieve(key, into_scope=, copy_into_scope=)`).
- `nix/lib/mkEntityFromPython.nix`, `nix/py_testing/` — the proven
  `mkVenv` + `mkEntityFromPython` example to mirror for `demo_data`.

## End-of-stage check ✅

- [x] `retrieve_nix_entity` round-trips: flake declares `fxtr.entities.demo_data`
  → agent script retrieves it → loads the entity (`test_in_memory_e2e_retrieve_nix_entity`).
- [x] `NixEntityBuildFailed` surfaces correctly on a missing/broken entity attr.
- [x] `current_commit()` returns HEAD on a clean tree, raises `WorkingTreeDirty`
  on a dirty one; the spawn E2E uses it.
- [x] `prepare_package_bundle` no longer in the sandbox builtin namespace,
  `McpHostProto`, or `McpHostImpl`.
- [x] `basic_test_demo` in-memory tests pass with the new retrieve-based flow.
  Full package suite: **249 passed, 1 skipped** (pre-existing skip).

## Implementation notes / things to carry forward

- **The runtime toml must be gitignored (new `.gitignore`).** `current_commit`'s
  clean-tree guard + the untracked-files notice both see the workdir's
  `.fxtr_workstation.toml` (written post-commit, untracked) unless it's
  gitignored. Added `.gitignore` to `basic_test_demo/agent_workdir`. **Stage 6**
  (`examples/fxtr_workstation_demo/agent_workdir`) will need the same
  `.gitignore` when it migrates to `current_commit()`, or its spawn flow will
  raise `WorkingTreeDirty`.
- **`demo_data` (Option A, full host `fxtr2`) cost.** The flake builds a
  second, heavier uv2nix venv (full `fxtr2`, host-runnable) on top of the wasm
  one for `hello_service`. Because `fxtr2` depends on `fxtr` (v1), and uv only
  honors the resolution root's `[tool.uv.sources]`, `demo_data`'s pyproject
  must redeclare `fxtr` (v1)'s source + a `hatchling` build-dep (mirrors
  `nix/py_testing`). Cold build is minutes; cached after. Could migrate to a
  lighter `fxtr2-portable`-based entity later if the build cost bites.
- **Self-contained entity, not `Note` reuse.** `demo_pkg` is sandbox-oriented
  (its `__init__` imports the sandbox bridge), so it can't be imported from the
  host venv `mkEntityFromPython` runs. `demo_data` defines its own
  `DemoDataEntity` (Option A, agreed 2026-05-31).
- **Cross-scope copy watch-item resolved.** Copying the retrieved entity into
  `script_scope` (reader `copy_into_scope=True`) and returning the `FxtrID`
  worked without a `GLOBAL_SCOPE`-including transient scope — the layer is
  self-contained, so no type-descriptor closure miss. No fallback needed.
