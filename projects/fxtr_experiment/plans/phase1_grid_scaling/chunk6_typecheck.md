# Chunk 6 — typecheck the package: pyright in pre-commit + error cleanup

Scope: the package currently has no typechecking gate — only black/autoflake
run on it in pre-commit — and an ad-hoc pyright run (found during the chunk
1.1 ViewSpec split, 2026-07-05) shows **160 errors** across
`fxtr_workflow_3/` + `tests/`. The spec-split work leans on the typechecker
for its guarantees (discriminated-union narrowing, kind-specific fields), so
those guarantees should be *enforced*, not just available in an IDE. This
chunk adds a pyright pre-commit hook for the package (mirroring the existing
`pyright (v2)` / per-project hooks in `.pre-commit-config.yaml`, with the
package's own venv) and fixes the errors so it lands green.

Inventory (from the scoping run; counts will drift, the shapes won't):

- **`encoding.py` (49 errors, the whole file's worth).** The tagged-JSON
  decode helpers take `object` trees and index/`.get` into them after
  `_is_map` / `_is_seq` checks that return plain `bool`. Fix at the root:
  make the two predicates `TypeGuard` functions (`TypeGuard[AbcMapping]` /
  `TypeGuard[AbcSequence]`) so the existing checks narrow; the handful of
  remaining sites (e.g. `len()` on a just-checked value) follow from that.
  No behavior change — this is annotation-only.
- **`tests/test_encoding.py` (42).** Tests index into decoded step payloads
  typed as the broad `FxtrServiceData` union (`__getitem__` flagged on
  every non-container member: `int`, `float`, `FxtrID`, refs). Fix with
  small typed accessors or `assert isinstance` narrowing in the tests —
  arguably also a hint that a typed payload-inspection helper would be
  nice, but don't build one just for tests.
- **View-surface ergonomics in tests (~50 across `test_user_surface.py`,
  `test_ops_engine.py`, `test_view_runtime.py`, `test_poisoning.py`,
  `test_run_step.py`, `test_task_service.py`, `test_executor_steps.py`).**
  Two recurring shapes:
  - `TaskView.output()` returns `DataView | DataFrameView`; callers using
    `.read(...)` on it trip over the frame half. Consider per-kind typed
    selection (e.g. `output()` overloads keyed off the declared spec, or
    `output_entity()` / `output_frame()` sugar) vs. plain narrowing in the
    tests — decide when fixing; whatever the tests need, real task code
    will need too.
  - `attach` / `build_view` return `GridView`, and callers pass/use them
    kind-specifically (`.read`, `.write`, `.read_key`, `HandleView`
    params). Tests should narrow (`assert isinstance`) — or `attach` could
    grow a typed variant. Again decide at fix time; prefer the smallest
    honest thing.
- **Service-framework override pattern (3).** `_target_type_annotation`
  impls annotated `-> Any` / `-> object` where the base declares
  `ServiceTypeAnnotation[T]`; mechanical signature fix at each impl
  (package + one test helper).
- **The one real seam mismatch (`task_service.py`).** The
  `view_runtime.ViewBackend` seam types `run_step`'s `step` param as
  `object` while `RunCapabilityProto` types it `ServiceRef[UnknownService]`,
  so the resolved proxy fails the structural check where it's passed to
  `build_view`. Align the seam's parameter type (the seam exists to be the
  structural subset — it should actually be one).
- **Misc (<10).** `mode: str` args where `SliceMode` literals are expected
  (tests); `step_type_id` attribute access on a plain function type (test);
  a few `Optional` subscripts in tests; the deprecated
  `@asynccontextmanager -> AsyncIterator` annotation form (pyright warns;
  switch to `AsyncGenerator`).

A recurring gotcha to watch for while fixing (bit twice during chunk 1):
pyright **widens literal types on inference** — a comprehension over
`Literal`-typed elements infers `list[str]`, and an instance attribute
assigned from a `Literal`-annotated parameter widens to `str` — unless the
variable/attribute is *explicitly declared* (`kept_kinds: list[AxisKind] =
...`, `self.value_kind: AxisKind = kind`). Anywhere an `AxisKind` /
`SliceMode` / kind-literal flows through an inferred binding, expect to add
the declaration.

Landing shape: configure pyright for the package (a `[tool.pyright]` block
in `pyproject.toml` or a `pyrightconfig.json`, pinning the venv), add the
pre-commit hook, fix in a few mechanical commits, and keep the hook green
from then on.

Also worth doing around the same time (deliberately not scoped here): the
package has outgrown "spike" — consider moving it out of `spikes/` into
`projects/` once phase 1 stabilizes. The typecheck gate is a natural
prerequisite/companion for that move; scope the move when we get here.

Notes/log (implementation, 2026-07-06):

- Landed as one commit: `[tool.pyright]` in the package's pyproject
  (mirroring v2's shape — basic mode, `reportMissingTypeStubs = false`,
  venv pinned — plus `tests/` in the include, per the inventory), pyright
  in the dev group, the pre-commit hook, and the cleanup — 234 errors
  (the inventory's 160 had drifted up with chunks 2–4) → 0.
- The fix-time decisions the inventory deferred:
  - **`output_entity()` / `output_frame()` typed sugar on `TaskView`**
    (delegating to `output()` + an isinstance check raising
    `ViewAxisError` on declared-kind mismatch); tests use it — real task
    code gets the typed path too. `output()` keeps the union return.
  - **`attach`/`build_view` stay `GridView`** — attach is inherently
    dynamic; tests narrow with `assert isinstance`. One exception where
    the code *does* know: `Workflow.open_handle` now honestly returns
    `HandleView` (it always opens a handle grid).
  - **`TaskFnLike` / `StepFnLike`** (`task_api.py`): a concrete task fn
    naming its rule's args (`async def f(ctx, /, *, inp)`) is statically
    *narrower* than the protocols' `**args` shape and hence not
    assignable to them — the adapters/decorators (and
    `CheckpointView.run_step`) now accept the documented union
    `Protocol | Callable[..., Awaitable[...]]`; name-binding and return
    shape stay call-time-enforced (the only place they can be). The
    protocols remain the documented contracts.
  - `encoding.py` fixed at the root with `TypeGuard` predicates as
    planned, plus local bindings where `.get()`-then-subscript defeated
    narrowing and two `cast`s for literal kinds (`in`-tuple checks don't
    narrow `object`); behavior unchanged.
  - The `ViewBackend.run_step` seam mismatch fixed by typing `step` as
    `ServiceRef[UnknownService]` (the seam is now the structural subset
    it claims to be).
  - Two test sites passed `Entity` where `DataView.write` declares
    `FxtrID` (worked via the engine's tolerant persist) — tests now store
    first; the declared contract is the honest one.
- The literal-widening gotcha bit once more, in reverse: equality/`in`
  checks never narrow `object` down to a Literal — hence the two casts in
  `encoding._binding_from_json`.
- The spikes/ → projects/ move remains deliberately unscoped (deferred to
  after this phase's review pass).
