# Chunk 1 — the request contract (the setup commit)

The standalone wire change: `ConnectorProto.connect()` goes from
no-argument to `connect(request: BindingConnectRequest)`, so bound
implementations see which key they serve. Independently useful (one
connector can serve a family of handles whose key entities differ only
in args) and the single protocol break for the whole phase — the
request shape is final from the start, with `config` / `handles`
dormant until chunks 2–3 populate them. Design: decisions 6 and 13 of
`discussion_log.md`; the contract statement is the "connect contract"
section of `notes/sandboxed_bindings.md`.

## The dataclass

`BindingConnectRequest` lives in **`fxtr_sandbox_types`** (new module,
e.g. `binding_request.py`, re-exported at the package root), documented
as the standard interface `fxtr_experiment` consumers implement
against — both external connectors and (later) sandbox guest
entrypoints receive it. A plain (frozen) dataclass, mashumaro/
codec-acceptable, importable under **both** flavor extras
(`[fxtr2]` and `[fxtr2-portable]` — mirror how `entities.py`
structures its imports):

```python
@dataclass(frozen=True)
class BindingConnectRequest:
    key: FxtrID                 # the handle key entity being served
    config: <JSON value> | None # binding-level non-identity config (dormant)
    handles: Mapping[str, ServiceRef[UnknownService]]  # pre-spawned deps (dormant)
```

Decisions taken here:

- **`key: FxtrID`**, not `EntityRef` — per the standing boundary
  doctrine (cell values cross as bare `FxtrID`; arena coverage ships
  on the wire). The `EntityRef` upgrade remains the design overview's
  open question 3 and stays purely additive.
- **`config` typing**: reuse the package's existing JSON type alias
  (the `JSONTypes`-style union already used by the object streams);
  plain JSON values only, no entities/refs — enforced by the type,
  documented on the field.
- The docstrings state the field contracts now (key = the fingerprint
  identity; config = the binding's non-identity blob; handles = the
  binding's pinned, pre-spawned dependencies), with a TODO on the
  dormant fields referencing `plans/phase6_sandboxing/overview.md`
  (chunks 2–3), to be removed when those chunks land.

## Changes, by module

- **`fxtr_sandbox_types`**: the new module + root re-export.
- **`fxtr_experiment/pyproject.toml`**: dependency on
  `fxtr-sandbox-types[fxtr2]` + the `[tool.uv.sources]` path entry
  (`../fxtr_sandboxing/fxtr_sandbox_types`, editable).
- **`task_service.py`**:
  - `ConnectorProto.connect(request)` — docstring carries the field
    contracts (the wire protocol is the place the contract lives).
  - `_SingletonConnector.connect(request)` ignores the request
    (docstring says so); `wrap_singleton` unchanged otherwise.
  - `BindingRegistry.spawn` assembles the request:
    `BindingConnectRequest(key=handle, config=None, handles={})`
    (it already holds the handle `FxtrID`). Docstring notes the
    assembly; scope-discipline comments updated for the new arg.
- **Callers/implementors** (mechanical signature updates):
  - demo drivers: `examples/fxtr_experiment_demo/experiment_runner.py`,
    `behavior/behavior_runner.py`, `experiment_demo_helper_service.py`.
  - tests: the connector impls across `test_task_service.py`,
    `test_executor.py`, `test_executor_steps.py`, `test_run_step.py`,
    `test_user_surface.py`, `test_introspection.py`, and any
    `boundary.py` touchpoints.
- **Docs**: `user_api.py`'s binding passages mention the request
  contract; `CODE_OVERVIEW.md` (task_service line + the new dep).

## Verification points (beyond mechanical test updates)

- A connector *receives the key it was bound for* on both consumption
  paths (executor task-fn prologue; step `ctx.connect`).
- The singleton connector ignores the request (existing behavior
  preserved end to end).
- **Wire transit**: the request crosses an RPC boundary intact — in
  particular that the typed codec handles the dataclass with a
  `Mapping[str, ServiceRef]` field (empty for now) and that the bare
  `FxtrID` key arrives with arena coverage so a remote connector could
  load it. Cheapest form: extend the existing RPC smoke coverage with
  a connector that loads the key entity through the wire.

## Open items to resolve while implementing

- Exact module name in `fxtr_sandbox_types` and whether the JSON alias
  needs re-exporting alongside.
- Whether `frozendict`/plain-dict is wanted for `handles` under the
  codec (follow whatever the decorated protocols already do for
  mapping args).
- Confirm `fxtr_experiment` resolves the new dep cleanly in CI (uv
  lock ripples in the demo workdirs are NOT expected — the demo
  drivers are host-side).

## Implementation steps

- [x] `BindingConnectRequest` in `fxtr_sandbox_types` (+ root
      re-export; importable under both flavor extras; unit test for
      codec round-trip).
- [x] `fxtr_experiment` dependency + uv sources entry (lock update).
- [x] `ConnectorProto` / `_SingletonConnector` / `BindingRegistry.spawn`
      signature + docstring updates.
- [x] Update all connector implementors (demo drivers + test suite) —
      **no updates were needed**; see deviations.
- [x] New verification tests (key arrival on both paths; RPC transit).
- [x] Doc sweep: `user_api.py` binding passages (the dormant-field TODO
      qualifiers stay by design — chunks 2–3 retire them).
- [x] Update CODE_OVERVIEW.md (fxtr_experiment + fxtr_sandboxing).

## Deviations and findings

- **The implementor ripple was nil.** `ConnectorProto` had exactly one
  implementation (`_SingletonConnector`) and one call site
  (`BindingRegistry.spawn`); every demo driver and test binds through
  `wrap_singleton`, whose signature is unchanged. The anticipated demo
  edits didn't exist.
- **Latent typing gap found and fixed**: connector refs arriving over
  the wire (`bind_handle`) decode as ``ServiceRef[UnknownService]``,
  so `spawn` resolved an *untyped* proxy — fine for a no-arg
  ``connect()``, but an untyped proxy can only encode
  ``FxtrServiceData`` args and rejects the request dataclass.
  `BindingRegistry.bind` now normalizes every incoming ref via
  ``replace_service_type(ConnectorProto)`` (changes neither the handle
  nor the wire ``service_type_id``, so tracking/bridging are
  unaffected). Caught by the pre-existing e2e tests the moment the
  request arg landed.
- **`key` stays `FxtrID`** (per the boundary doctrine); `config` is
  typed by a new codec-safe ``JSONValue`` alias in
  ``binding_request.py`` — the existing ``JSONTypes`` was unusable for
  codec purposes. Two mashumaro constraints discovered, documented on
  the alias, and regression-tested
  (``fxtr_sandboxing/tests/test_binding_request.py``): the recursion
  must be *bare* (a quoted ``"JSONValue"`` forward ref breaks the
  typed-service codec builder with a ``NameError``), and the union
  member order matters (``Mapping`` before ``Sequence``, else a dict
  decodes to its key list; ``bool`` before ``int``; ``str`` before the
  collections).
- The new RPC-transit test (`test_connect_request_crosses_a_real_wire`)
  proves the *full* final shape early — key closure over the wire,
  JSON config round-trip, and a ``handles`` ServiceRef resolving on
  the far side back to the caller's service (what chunk 3's dependency
  resolution relies on). It must signal the server *inside* the
  client's rpc scope (the concurrent-drain rule from the user-surface
  rpc smoke).
