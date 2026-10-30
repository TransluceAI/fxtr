# Chunk 2 — durable records + capability surface

The sandboxed-binding **record** becomes real durable state, with its
full user-capability surface — but nothing serves it yet (resolution
of a sandbox-bound key faults not-ready via the existing
`HandleUnboundError` until chunk 3), so this chunk is pure state +
surface, testable without any sandbox machinery.

Design: `notes/sandboxed_bindings.md`. The decisions below were settled
in the chunk-start design round (see `discussion_log.md`).

## Settled constraints (from `notes/sandboxed_bindings.md`)

- **The record**: keyed on handle *key id*; fields = defn triple
  (`git_commit`, `flake_attr`, optional `flake_path`), dependency map
  (name → grid path + fully-bound coords + pinned expected key id),
  optional JSON config. Structured state-db columns, never an entity.
- **`bind_sandbox`**: reads each dependency cell in declaration order,
  requires it populated, pins its current key id; persists the bound
  key's closure from the call arena (liveness); rejects if the key is
  bound in *either* class (cross-class exclusivity, either order); no
  nix eval (lazy — bind is cheap and repo-independent). No release
  capability — removal is `remove_sandbox_binding`, a plain state
  mutation.
- **No currency/grounding marks** — bindings are not identity; but
  mutations emit events-log rows and the bindings change hint (the
  session's "Events:" audit-trail convention applies).
- **`list_bindings` / `BindingInfo`** grow to serve both classes.
- Errors introduced here: the double-bind rejections (both
  directions), unpopulated/ineligible-dependency-cell at bind, unknown-
  binding removal. (Repo/commit/pin faults are connect-time — chunk 3.)

## Decisions (chunk-start round)

1. **Schema — normalized, two tables** (bidirectional breakage
   reasoning; makes the chunk-4 TOML projection direct):

   - `sandbox_bindings` — one row per binding:
     `key_id BLOB PRIMARY KEY` (handle key id-string bytes; also the
     liveness anchor), `git_commit TEXT`, `flake_attr TEXT`,
     `flake_path TEXT` (nullable), `config_json TEXT` (nullable;
     canonical JSON text — see decision 6).
   - `sandbox_binding_deps` — normalized dependency rows:
     `key_id BLOB REFERENCES sandbox_bindings(key_id) ON DELETE
     CASCADE`, `dep_name TEXT`, `grid_path TEXT`, `coords TEXT`,
     `pinned_key_id BLOB`, PK `(key_id, dep_name)`.

   **Deps are an unordered map** keyed by the unique `dep_name`; no
   order column. There is no semantic reason to preserve declaration
   order — bind pins each cell to its own current value (order-
   independent), chunk-3 connect spawns the deps as independent
   `handles` (no cross-dep ordering), and where a stable order is
   needed (deterministic bind-error reporting, chunk-4 export, listing)
   the **canonical order is `dep_name`-sorted**, exactly as the codebase
   canonically sorts sets. Both `grid_path` / `coords` are **path text
   with no FK to `grids`** (mirroring `targets` / `user_sourcing`): a
   dependency of a since-deleted grid stays a fact of the binding
   (decision 5). `SCHEMA_VERSION` bumps 7 → 8.

2. **Dependency selections cross as `ViewSel`** (a `Mapping[str,
   ViewSel]`), resolved runner-side to absolute `(grid_path, coords)`
   via `_resolve_cell` exactly like `add_target` — keeping the "wire is
   token-based; absolute paths are holder-side" discipline. Each must
   be a **fully-bound handle cell** (open positions / non-handle grids
   rejected). Deps are an unordered map keyed by a unique name
   (decision 1). The bound handle itself stays a bare `FxtrID` (a
   binding is keyed by entity, not cell — the asymmetry is correct).

3. **Wire signatures**: each side declares a dataclass view; fxtr's
   codec converts. `bind_sandbox(handle: FxtrID, defn: SandboxDefn,
   deps: Mapping[str, ViewSel], config: JSONValue | None = None) ->
   None` (return `None`; `list_bindings` confirms).
   `remove_sandbox_binding(handle: FxtrID) -> None`. `SandboxDefn`
   (`git_commit`, `flake_attr`, `flake_path: str | None = None`) is a
   frozen dataclass in `model.py` (shared by the wire + storage);
   `JSONValue` is the alias from `fxtr_sandbox_types.binding_request`.

4. **Exclusivity via one lock**: the durable rows live in the db, the
   ephemeral registry is in-memory, so the both-ways check needs a
   single serialization point. Both `bind_*` operations acquire one
   `anyio.Lock` on `UserCapabilityImpl` (which holds both the registry
   and the session). Under the lock: check `registry.is_bound(key)`
   (sync) **and** `await session.has_sandbox_binding(key)` (db), raise
   `HandleAlreadyBoundError` on either, then mutate. Removals
   (`release`, `remove_sandbox_binding`) only free a key, so they need
   no lock. Defense-in-depth: `sandbox_bindings`' `key_id` PK also
   forbids a sandbox-sandbox double-bind at the db layer.

5. **Repo config — an `open_local` kwarg, not wire-settable, not
   validated in chunk 2**: `open_local(..., sandbox_repo: Path | None =
   None)`; stored on `BindingRegistry` (the binding authority; where
   chunk-3 serving reads it). Chunk 2 does **nothing** with the path —
   no filesystem/git/`fxtr_sandboxing` touch — it is stored purely for
   chunk 3 to pick up. No `set_sandbox_repo` method; not changeable /
   closeable while open. Eager-vs-lazy *use* is a chunk-3 call.

6. **Config canonicalization**: `config_json` is stored as canonical
   JSON text (sorted keys, compact separators — reuse
   `encoding._canonical_json` via a small exported helper) so chunk-4
   export diffs are deterministic. Object key order is normalized
   (arrays preserved) — fine for JSON semantics and consistent with
   identity text elsewhere.

7. **`BindingInfo` — one `BindingState` enum + a sandbox detail**
   (subsumes the class discriminator; expands in chunk 3):
   - `state: BindingState` — `Literal["external", "sandbox_launchable",
     "sandbox_repo_missing"]`. `external` = an ephemeral connector
     binding. `sandbox_launchable` = sandbox binding, runner has a repo
     configured, no known runner-level blocker (flake/commit/pins are
     verified at connect — chunk 3). `sandbox_repo_missing` = sandbox
     binding, no repo configured. Chunk 3+ adds
     `sandbox_launching`/`running`/`faulted` (+ maybe
     `sandbox_misconfigured`). The enum prefix is the class discriminator
     — no separate `binding_class` field.
   - `sandbox: SandboxBindingInfo | None` — present for `sandbox_*`
     rows: `defn: SandboxDefn`, `config: JSONValue | None`, `deps:
     tuple[SandboxDepInfo, ...]` where `SandboxDepInfo` =
     `(name, grid_path, coords, pinned_key_id, pin_status)` and
     `pin_status: Literal["attached", "mismatched", "missing"]` is
     derived by re-reading the dep cell (mirrors `list_sourcing`'s
     dangling derivation).
   - The **global repo-configured state is NOT on `BindingInfo`** —
     deferred to chunk 5's workflow-level watch field (see the chunk-5
     revisit note). `sandbox_repo_missing` already carries the
     per-row consequence.
   - Existing `key` / `cells` unchanged (cells via
     `handle_cells_holding` over all keys, both classes).

8. **Hints/events**: sandbox bind/remove sync bodies emit the existing
   `("bindings", "")` change hint via `note_change` (the workflow
   watch's `bindings` recompute already consumes it; `change_hints.py`'s
   docstring is updated — bindings can now come from session bodies,
   not only the registry). Events-log rows: `bind_sandbox` (payload =
   key id + defn triple), `remove_sandbox_binding` (key id).

9. **Naming**: `bind_sandbox` / `remove_sandbox_binding` kept;
   `set_sandbox_repo` dropped (decision 5).

## Serving stays inert (no change needed)

Both consumption paths — the executor prologue
(`self._bindings.spawn(fn_key, …)`) and step `connect`
(`BindingRegistry.spawn`) — raise `HandleUnboundError` (fault →
interrupted) when no *ephemeral* binding exists. Chunk 2 does not teach
`spawn` about sandbox records, so a sandbox-bound-but-not-ephemeral key
faults not-ready automatically. A regression test locks this in.

## Implementation steps

- [x] **`storage.py`**: bump `SCHEMA_VERSION` 7 → 8; add the two tables
      to `SCHEMA_SQL`; add a "Sandboxed bindings" section to the module
      docstring (normalized deps, un-FK'd path refs, liveness on
      `key_id` + `pinned_key_id`, canonical `config_json`, no
      currency/grounding marks) and correct the "What is deliberately
      absent" note (ephemeral bindings absent; sandbox bindings now
      durable).
- [x] **`model.py`**: add `SandboxDefn` (defn triple, frozen dataclass).
- [x] **`errors.py`**: add `HandleAlreadyBoundError`,
      `SandboxDependencyError` (empty / non-handle / missing dep cell at
      bind), `UnknownBindingError` (remove of an unbound key) — all
      `WorkflowError`, interactive/user-surface.
- [x] **`encoding.py`**: export a small canonical-JSON helper (thin
      wrapper over `_canonical_json`) for `config_json`.
- [x] **`state_store.py`**: sync bodies + async wrappers —
      `add_sandbox_binding(key_id, defn, deps={name: (grid_path,
      coords)}, config_json)` (one transaction: read+validate each dep
      cell in `dep_name`-sorted order, pin its value id, insert rows;
      note `("bindings","")`; append event),
      `remove_sandbox_binding(key_id)` (delete + cascade;
      `UnknownBindingError` if absent; hint; event),
      `has_sandbox_binding(key_id) -> bool`, `list_sandbox_bindings()`
      (deterministic: by key id, deps `dep_name`-sorted). Each body's
      docstring declares its "Events:" line and "Marks: nothing
      (bindings are not identity)".
- [x] **`task_service.py`**: `BindingRegistry` gains a stored
      `sandbox_repo: Path | None` (constructor kwarg; chunk-2 use is
      exposure only); class docstring notes it is the ephemeral registry
      **and** holds the runner sandbox-repo path, while sandbox binding
      *records* live in the session.
- [x] **`user_api.py`**: `bind_sandbox` / `remove_sandbox_binding`
      protocol methods (full docstrings: pin semantics, exclusivity,
      inert-until-chunk-3); `BindingState`, `SandboxBindingInfo`,
      `SandboxDepInfo`, `PinStatus`; grow `BindingInfo` with
      `state` + `sandbox`.
- [x] **`user_service.py`**: `_bind_lock` on `UserCapabilityImpl`;
      `bind_sandbox` impl (resolve dep `ViewSel`s → handle cells,
      persist key, exclusivity check under lock, `add_sandbox_binding`);
      `bind_handle` impl gains the under-lock cross-class check;
      `remove_sandbox_binding` impl; `list_bindings` merges both classes
      with `state` + per-dep `pin_status`; `open_local` /
      `BindingRegistry` construction gain `sandbox_repo`; `Workflow`
      wrapper convenience forwards + `sandbox_repo` passthrough.
- [x] **`change_hints.py`**: docstring — `("bindings","")` is now also
      emitted by session bodies (durable sandbox mutations).
- [x] Update `CODE_OVERVIEW.md` (storage/model/errors/task_service/
      state_store/user_api/user_service lines; the phase-6 TODO notes
      chunk 2 done, chunks 3–5 remaining).
- [x] **Tests** (`tests/test_sandbox_bindings.py`, extending
      `boundary.py` as needed): schema round-trip; `bind_sandbox`
      persists the key closure + rows + declaration-order pins;
      empty/missing/non-handle dep cell → `SandboxDependencyError`;
      cross-class exclusivity both directions → `HandleAlreadyBoundError`;
      `remove_sandbox_binding` + `UnknownBindingError`; `list_bindings`
      merges classes with the right `state` and `pin_status`
      (`attached` → rewrite dep cell → `mismatched` → delete →
      `missing`); the `("bindings","")` hint reaches a workflow watch;
      a sandbox-bound key still faults not-ready through executor + step
      connect; RPC transit of `bind_sandbox` (`ViewSel` deps + `defn` +
      `config`).

## Deviations and findings

- **All 322 suite tests pass; pyright clean.** No changes to the executor
  / step-connect spawn path were needed — a sandbox-bound key faults
  not-ready through the pre-existing `HandleUnboundError`, exactly as
  intended.
- **`SandboxBindingRow` / `SandboxDepRow`** (state-db read DTOs) landed in
  `state_store.py` beside `StateDbError` (co-located with the producer,
  like the other `_list_*` bodies), not in `introspect.py`.
- **The capability canonicalizes `config`** (via `encoding.canonical_json_text`)
  and passes canonical *text* to the session; the session body stays a
  pure text-storage concern. `list_bindings` parses it back with
  `json.loads`.
- **`add_sandbox_binding` reads + validates the dep cells in the session
  transaction** (one atomic read-pin-insert), given the capability
  resolves the `ViewSel`s → `(grid_path, coords)` first. It also re-checks
  the sandbox class (defense in depth + the `key_id` PK); the cross-class
  (ephemeral) check stays in the capability under `_bind_lock`.
- **The fault test demands via `t.output_entity().read()`** after
  `add_target`, not `wf.demand(t)` — the wrapper's `demand` reads a task
  cell without an output selection and raises `ViewAxisError` at the view
  layer before execution; the output-view read is the path that reaches
  the prologue spawn (→ `HandleUnbound` → interrupted → `CellFaultError`).
- **RPC transit**: a real cross-process test is included, but note the
  local typed-service dispatch *already* serializes args + returns through
  the same codec, so the local tests independently exercise the
  `SandboxDefn` / `Mapping[str, ViewSel]` / `JSONValue` / nested
  `SandboxBindingInfo` codec paths.
- **`remove_sandbox_binding` errors on an unknown key** (`UnknownBindingError`)
  per the plan — deliberately *unlike* `remove_target`'s idempotent
  removal. Flagged during design; revisit if the asymmetry proves
  annoying.
- **`config_json` normalizes object key order** (canonical JSON) — arrays
  preserved; documented on `encoding.canonical_json_text` and the schema.
- **Import/export untouched** (chunk 4): the new tables aren't exported
  yet, so a `bind_sandbox` followed by an `import_state` drops the binding
  — expected, and wired in chunk 4.
