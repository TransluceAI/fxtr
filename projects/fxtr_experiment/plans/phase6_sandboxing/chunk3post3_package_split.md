# Chunk 3 followup — the physical task-side package split

*Own commit, after chunk 3 (or at latest when the first non-container
guest becomes real). Chunk 3's module split + import-boundary test are
the prerequisite and held the line until then (the test retires with
this followup — see the Test suites decision). Rationale: the chunk-3
round in `discussion_log.md` (this directory). Scoped to
implementation-ready in the 2026-07-21 review round (the decisions and
audit findings below), corrected at implementation start on 2026-07-31
(the boundary prep was under-scoped — see that decision); the audit
facts: ~19 runner-side modules import task-side modules, plus the demo
drivers (`examples/fxtr_experiment_demo`), the two workdir guest
fixtures, and the test suite (~100 import lines) — all mechanical
churn.*

## Why physical, why not now

A sandboxed task-fn guest needs the task-side runtime
(`TaskFnAdapter`, the view runtime, the wire types) but must not drag
in the runner side (sqlite/state-db machinery) or a hard polars
dependency (no wasm wheel). The decision: a **physical split into two
packages**, not a `fxtr2-portable`-style force-include twin — that
pattern was a bootstrap hack justified by full-fxtr2's heavy deps, and
nothing here needs it: the task side splits cleanly with zero
duplication (the runner package imports it).

Not in chunk 3 because it is pure packaging/import churn with zero
behavior change and no consumer until a non-container guest exists —
and chunk 3 is already the phase's largest chunk.

## Shape

- **Task-side package** (`fxtr-experiment-task`):
  `model.py`, `errors.py`, `views.py`, `view_spec.py`, `task_api.py`,
  `runner_api.py`, `view_runtime.py`, `task_adapter.py`, `_ns.py`,
  and `tables.py` behind a **`[frames]` extra** (tables is the sole
  eager-polars module; entity-only guests never import it). Deps:
  fxtr2, frozendict, mashumaro (+ polars via the extra) — anyio was
  in the sketch but nothing task-side imports it directly (it arrives
  via fxtr2).
  Deliberately **not** `fxtr-sandbox-types`: nothing task-side imports
  it (`BindingConnectRequest` is produced by `bindings.py` runner-side
  and consumed by guest *entrypoints*, which declare the dep
  themselves — the `binding_taskfn` fixture already does).
- **Runner package** (`fxtr_experiment` keeps the name): everything
  else, depending on the task package with `[frames]`. It also
  **retires chunk 3's `[sandbox]` extra**: that extra exists only
  because guests and runners currently install the same distribution
  (a task-fn guest must not inherit the host sandbox machinery);
  post-split, guests install only the task package, so the runner
  package hard-depends on `fxtr-sandboxing` plainly (keeping
  `open_local`'s lazy launcher import is still fine — a repo-less
  runner needn't pay the import — but it stops being load-bearing
  for packaging).
- **fxtr2 dep stays plain `fxtr2`**: container guests install it
  directly. A wasm/pyodide install inherits fxtr2's own portability
  story — either the `[fxtr2]`/`[fxtr2-portable]` extras dance
  (`fxtr_sandbox_rpc_adapter` precedent) or fxtr2's planned native
  decoupling, whichever lands first. Deliberately not this commit's
  problem.

## Decisions (2026-07-21 scoping round, review-confirmed)

- **Namespace: own top-level name** — dist
  `fxtr-experiment-task`, import `fxtr_experiment_task`. Continues
  chunk 3's no-re-export-shims precedent (the `task_adapter` move:
  importers update, one canonical name per module). The namespace-
  package alternative (splitting `fxtr_experiment.*` across two
  distributions) is REJECTED: it needs the shared package root to
  ship from both dists with no `__init__.py`, interacts subtly with
  editable installs and hatch packaging, has no precedent in this
  repo, and fails confusingly under version skew — all to avoid a
  one-time mechanical import sweep.
- **Location: nested sub-dist inside the project**
  (`projects/fxtr_experiment/task_package/` — `pyproject.toml` naming
  the dist `fxtr-experiment-task` + package dir
  `fxtr_experiment_task/`). The project root stays the runner dist
  unchanged; the runner adds a `uv.sources` path entry
  (`path = "task_package", editable = true`). Precedent for the
  shape: `v2/portable/` (a nested second dist inside a dist root —
  here with real files, no force-include) and `frontend/` (this
  project dir is already multi-package). Rejected: a sibling
  `projects/` dir (it is the same project) and the full
  fxtr_sandboxing-style restructure (every existing path — pre-commit
  directories, fixture path-deps, CI, docs — would move for layout
  symmetry alone). The task dist stays lean: pyproject + package
  only — no own lock, venv, or test dir (guests bring their own
  locks; see the test/wiring items). Small own `CODE_OVERVIEW.md`
  (the task-side surface + a pointer to the runner's, which keeps the
  deep module details).
- **Boundary prep** — the only non-mechanical part of the commit.
  `tables.py` is the sole task-side module that still reaches the
  runner side, and it does so twice: `encoding.py` (`EncodingError`,
  `validate_frame_schema`) *and* `frame_encoding.py` (`FrameRow`,
  `FrameEntity`, `build_frame_entity`) — and `frame_encoding` itself
  eagerly imports `encoding`, so moving `tables` untouched would drag
  both along. (Invisible today only because `tables` sits in the
  boundary test's banned list rather than in its task-side list.)
  Four relocations cut it clean; afterwards `tables.py` imports
  exactly polars + `model` + `errors`:
  - `EncodingError` → task-side `errors.py` (the taxonomy module;
    `encoding.py` re-imports it — runner→task is the legal
    direction). Note in the docstring that it is a plain `ValueError`,
    not a `WorkflowError` — it predates and sits outside the
    poisoning taxonomy.
  - `FrameRow` (`frame_encoding.py`) → `model.py`: it is
    `tuple[TableValue, ...]` and `TableValue` already lives there.
  - `validate_frame_schema` (`encoding.py`) → `model.py`, **not**
    `tables.py`. It has three consumers, not one: `tables`,
    `frame_encoding.build_frame_entity`, and `encoding.store_config`.
    Homing it in `tables` would make `encoding.py` — which nearly
    every runner module imports — pull the sole eager-polars module,
    i.e. **polars would become eager runner-wide**, destroying the
    laziness property `CODE_OVERVIEW` documents. `model.py` is
    upstream of all three and polars-free.
  - `canonical_frame_from_ipc` (`tables.py`) → `frame_encoding.py`:
    it is the identity-minting funnel, and no task-side caller exists
    by doctrine (`task_adapter`: "no canonical-encoding code runs
    task-side"). `frame_encoding` — "the dataframe identity spec" —
    is the better home regardless. Its `tables` import is
    function-local, like every other frame-op call site, so
    `frame_encoding` stays polars-free to import. Three call sites move
    (`executor`, `ops_engine`, `user_service`), plus doc pointers in
    `CODE_OVERVIEW.md`, `encoding.py`'s docstring, and
    `notes/dataframes.md`.

  `encoding.py` itself stays runner-side as planned (nothing else
  task-side touches it).
- **`_ns.py` keeps its underscore** in the task package: the five
  runner modules that need `WORKFLOWS3_NS` import
  `fxtr_experiment_task._ns`. Cross-package access to a private name
  is accepted here — it is one project, and the marker still means
  "not for external consumers"; renaming buys nothing and churns more.
  The mirror comment in
  `nix/fxtr_lib_type_ids/src/fxtr_lib_type_ids/__init__.py` gets the
  new path.
- **Test suites**: the runner package keeps the whole existing suite
  in place (it depends on the task package, so everything stays
  covered, in the one shared venv). `tests/test_task_side_imports.py`
  is **deleted** (decided 2026-07-31, after a round of trying to keep
  it). Both halves of what it guarded are better covered elsewhere
  post-split:
  - The *runner-absent* half becomes largely structural. Pre-split it
    guarded an **invisible** mistake — inside one package,
    `from fxtr_experiment.encoding import …` looked like every other
    import — and that was the whole reason it earned its keep. Now a
    task module reaching the runner has to name a different
    distribution, which is a visibly odd thing to write, and is simply
    absent in a guest install.
  - The *polars* half is a real constraint with no packaging
    enforcement (`[frames]` is an extra of the same dist, so `tables`
    is on disk regardless) — but the honest check is the **gated
    container e2e**, whose task-fn fixture installs
    `fxtr-experiment-task` with no extras. If any task-side module
    imported polars eagerly, that guest fails.
  What the deleted test actually asserted was a proxy that had drifted
  from both properties: a hand-maintained module list (a new task-side
  module would be silently uncovered), a hand-maintained *blocklist*
  rather than the real "imports satisfiable from declared deps"
  property, and the wrong environment (a venv containing everything).
  Making it honest would have meant either a transitive allowlist —
  the same rot in a different hat, since importing fxtr2 legitimately
  pulls anyio/cbor2 — or running the probe in an isolated
  task-dist-only env, which is what the e2e already does for real.
  The removal is its own commit; no doc outside this file explains the
  absence, and none names the test that replaced it (code does not
  describe its own tests).
- **Wiring**: uv sources entries in every consumer
  (`fxtr_experiment` itself, demo drivers' pyprojects, the
  `binding_taskfn` fixture) — required even where the consumer only
  path-deps on `fxtr-experiment`, since uv honors only the *root*
  project's `[tool.uv.sources]` and the task package arrives as a new
  transitive path dep. The runner's existing pyright config includes
  `task_package/fxtr_experiment_task` too, so the existing pre-commit
  hook covers both dists in the one venv (no new hook, no second
  venv). Committed locks that regenerate: `fxtr_experiment`'s and the
  demo's — *not* the fixtures', which commit no `uv.lock` at all (the
  workdir harness generates one per workdir; the earlier
  "committed-lock gotcha" note was wrong about these).
- **Guest fixture**: `binding_taskfn` installs `fxtr-experiment-task`
  **without** `[frames]` — confirmed at implementation start: its task
  fn is `return await inp.read(x="a")`, entity-only, so it never
  touches `tables`, which is exactly what proves the extra — instead
  of full `fxtr_experiment`; the container image shrinks accordingly.
  Verification = the existing container e2e.
- **Wasm/pyodide task-fn e2e: NOT this commit** (review-confirmed):
  the split removes the fxtr_experiment-side blockers (runner import
  graph + hard polars), but a wasm guest still needs the fxtr2
  dependency satisfied by fxtr2-portable — the extras dance
  (`fxtr_sandbox_rpc_adapter` precedent) or fxtr2's native
  decoupling, per the Shape section. Noted from review: **pyodide may
  be the cheaper first non-container guest** — it takes emscripten/
  PEP-783 wheels, so full fxtr2 might resolve as-is, skipping the
  portable dance entirely; also deferred. The non-container task-fn
  e2e is the acceptance test of that followup; this commit's
  done-criterion is the container e2e + the task package importing
  clean on its own.

## Implementation steps

Grouped into **three commits, each green on its own** (suite + pyright;
the two that change a guest install set also run the gated container
e2e). The grouping is not free-form: once the modules leave
`fxtr_experiment/`, every runner module, the whole test suite, and the
demo are broken until all the sweeps land — so the move and its sweeps
are necessarily one commit, and the only clean seam after it is the
guest-facing flip, which the fixture survives untouched.

### `.1` — boundary prep (landed)

- [x] **In place, before anything moves** — a behavior-preserving
      refactor of the current package, green on the existing suite on
      its own: the four relocations above (`EncodingError` → `errors`;
      `FrameRow` + `validate_frame_schema` → `model`;
      `canonical_frame_from_ipc` → `frame_encoding`), their call sites,
      and the doc pointers (`CODE_OVERVIEW.md`, `notes/dataframes.md`,
      `encoding.py`'s docstring).

### `.2` — the physical split (one atomic commit)

- [x] Create `task_package/` (pyproject: fxtr2, anyio,
      mashumaro/frozendict as actually needed; `[frames]` extra =
      polars; no `fxtr-sandbox-types`); move the task-side modules
      (`_ns.py`, `model.py`, `errors.py`, `views.py`, `view_spec.py`,
      `task_api.py`, `runner_api.py`, `view_runtime.py`,
      `task_adapter.py`, `tables.py`).
- [x] Runner package: imports sweep (~19 modules), dep on
      `fxtr-experiment-task[frames]`, `uv.sources` path entry, drop
      polars/dev leftovers. (The `[sandbox]` extra stays for now — see
      `.3`.)
- [x] Consumers sweep (~100 import lines): tests, demo drivers, the
      `fxtr_lib_type_ids` `_ns` mirror comment; the runner pyright
      config includes the task package dir.
- [x] **uv sources in every non-root consumer.** uv honors only the
      *root* project's `[tool.uv.sources]`, so a path-dep on
      `fxtr-experiment` does not carry its source entry for the new
      transitive `fxtr-experiment-task`. Both the demo
      (`examples/fxtr_experiment_demo`, committed `uv.lock` regenerates)
      and the `binding_taskfn` fixture (comment-flip convention: active-
      in-template relative path + commented active-in-workdir git line,
      plus the `[tool.uv.extra-build-dependencies]` hatchling hint) need
      their own entry **in this commit**, before the dep itself flips.
      Note: the fixture templates commit no `uv.lock` (the harness
      generates one per workdir), so there is no fixture lock to
      regenerate — only the demo's.
- [x] ~~Rewrite `tests/test_task_side_imports.py`~~ — done in `.2` as
      two probes, then **superseded**: the test is deleted in `.3` (see
      the Test suites decision).
- [x] Docs: both `CODE_OVERVIEW.md`s (the task package gets its own,
      small: the task-side surface + a pointer to the runner's).
- [x] Verification: full suite; pyright both packages; the gated e2e
      sweep — the container image's install set *does* change here (the
      guest still installs `fxtr-experiment`, which now pulls the task
      package), so the image rebuilds; `jj pre`.

### `.2post` — drop the import-boundary test

- [x] Delete `tests/test_task_side_imports.py` and every reference to
      it: `task_adapter.py`'s docstring, both `CODE_OVERVIEW.md`s, the
      task pyproject header, `notes/sandboxed_bindings.md`. The
      docstring and header state the *constraint* (polars stays lazy;
      the runner side is a different dist) and stop there — what
      exercises it is not their business.

### `.3` — the guest-facing flip

Separable because the fixture keeps working untouched through `.2`: it
depends on `fxtr-experiment`, which depends on the task package, so its
`task_adapter` import still resolves — it just installs more than it
needs. Splitting it off also keeps the one slow nix-rebuild verification
out of the big mechanical commit.

- [x] `binding_taskfn` installs `fxtr-experiment-task` *without*
      `[frames]` (its task fn is entity-only — this is what proves the
      extra) instead of `fxtr-experiment`; the container image shrinks
      accordingly.
- [x] Retire the `[sandbox]` extra — hard-dep `fxtr-sandboxing` on the
      runner (`open_local`'s lazy launcher import stays as a
      convenience, but stops being load-bearing for packaging). Tied to
      this commit, not `.2`: the extra exists precisely so guests
      installing the runner dist don't inherit the host machinery, and
      the last such guest is the fixture above.
- [x] Docs: `notes/sandboxed_bindings.md`'s runtime-split passage (the
      polars blocker is now the extra), the phase-6 overview checkbox,
      this file's checkboxes.
- [x] Verification: full suite; pyright; the gated e2e sweep (image
      rebuilds against the smaller install set — the done-criterion for
      the whole followup, together with the task package importing clean
      on its own); `jj pre`.
