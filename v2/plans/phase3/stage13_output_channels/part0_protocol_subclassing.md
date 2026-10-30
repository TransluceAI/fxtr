# Part 0: Enable service-protocol subclassing

Standalone prep stage in `fxtr2.services` (lands before part 1):
make `@service_protocol` Protocols genuinely subclassable, relax the
`Subscriber` / `EventSource` payload bounds, and migrate the existing
re-declare-workaround sites. After this, "a Protocol that wants to
*be* an event source cannot subclass `EventSource`" stops being true,
and stage 13's `FxtrChannelStorage` subclasses `EventSource`
for real instead of becoming the third copy of the workaround.

## Background: where the rejection lives

Not at registration — at **lookup**.
`TypeAnnotationHandlerRegistry._finish_with_type`
(`conversion/base_registry.py:436`) walks the queried class's MRO,
collects every registered handler (most-specific first), and raises
"Conflicting handlers" unless each *shadowed* handler was registered
with `allow_implicit_overrides=True`. The flag is already plumbed
end-to-end: `register_service_resolver(for_type, resolver,
allow_implicit_overrides=False)` (`data/entity_ref_scope.py:961`)
passes it straight to `register_handler`; `@service_protocol` just
never sets it (`services/typed_service.py:629`).

So today: registering a subclass protocol *succeeds*, and every
subsequent `resolve()` of the subclass type throws.

## Change 1: the opt-in flag (small)

- `@service_protocol` gains `allow_protocol_subtypes: bool = False`,
  passed through as
  `register_service_resolver(p, resolver,
  allow_implicit_overrides=allow_protocol_subtypes)`.
- Decorate the intended base protocols with it: `EventSource`,
  `Subscriber` (in `subscription_types.py`) and
  `WorkstationDispatcherProto` (in the workstation, change 4).
- Semantics after the flip: a decorated subclass registers its own
  resolver; lookup picks the most-specific handler (the MRO walk
  starts at the queried class). Lookups for the base type itself are
  unaffected. An *undecorated* subclass finds the base's resolver via
  the MRO walk (single handler, no conflict) and resolves with the
  base vocabulary — acceptable fallback; document it. Relatedly,
  `_fxtr_service_protocol_metadata` is a plain class attribute, so an
  undecorated subclass *inherits* the base's metadata object — audit
  the `_METADATA_ATTR` readers and add an `in vars(p)` check anywhere
  that distinction matters. (Audit so far: fxtr2 itself never reads
  the attribute back — the metadata is closed over in the registered
  resolver; the only external readers are `getattr`-based lookups in
  the sandbox bridge, `fxtr_ws_mcp_sandbox_bridge/builtins.py:865` and
  `:1041`. For those, an undecorated subclass inheriting the base's
  metadata gives base-vocabulary dispatch — consistent with the
  resolver fallback, so likely fine as-is; confirm at impl time.)

## Change 2: TypeVar substitution for inherited methods (the real work)

`_walk_protocol_methods` (`typed_service.py:445`) already walks the
full MRO and collects inherited methods (first-wins by name). The gap
is in wrapper building: `_build_method_wrappers` resolves the
inherited method's annotations *as written* — referencing the
**defining class's** TypeVars — while the wrapper dataclass is
parameterized by the **owner's** `__parameters__`
(`typed_service.py:290`). For
`class Foo[V](EventSource[V], Protocol)`, inherited `subscribe`'s
base-`T` never appears in the wrapper's params, so mashumaro silently
resolves it to its bound at codec build instead of the subclass's
binding.

Fix, contained to `typed_service.py`:

1. `_walk_protocol_methods` additionally reports each method's
   *defining class* (it already iterates per-MRO-class — return
   `(defining_cls, method)` pairs).
2. When `defining_cls is not owner_cls`, compute the TypeVar
   substitution map by walking `types.get_original_bases` from the
   owner down to the defining class, composing maps hop by hop
   (`Foo[V]` → `EventSource[V]` yields `{T_eventsource: V}`; multi-hop
   chains compose).
3. Apply the map to each resolved hint before building wrapper
   fields. Reuse `_substitute_typevars(annotation, subst_map)` —
   it already exists in `conversion/base_registry.py:44` (move or
   import it; it's generic machinery).
4. Resolve an inherited method's hints against its **defining
   class** (defining class's `__type_params__` in `localns`,
   defining module's globals — matters under `from __future__ import
   annotations`), *then* apply the object-keyed subst map. This is
   what makes the same-name-TypeVar case below fall out naturally
   instead of being fought in `_resolve_method_hints`.

Edge cases to handle + test:

- **Multi-hop**: `A[T]` → `B[U](A[U], Protocol)` → `C(B[int],
  Protocol)` — `A`-defined methods see `T → int`.
- **Unparameterized base**: `class X(EventSource, Protocol)` — no
  args in `__orig_bases__`; treat base TypeVars as unsubstituted
  (current bound-fallback behavior, now *explicit*).
- **Name collisions**: subclass reusing the base's TypeVar name
  (`class Foo[T](EventSource[T])`) — substitution must key on TypeVar
  *objects*, not names (also fixes the latent footgun in
  `_resolve_method_hints`'s name-keyed `localns` injection for this
  path — verify, don't rewrite that helper unless needed).
- **Override wins**: a subclass re-declaring an inherited method
  (e.g. purely to refine types or attach a docstring) uses its own
  annotations — already guaranteed by first-wins in the MRO walk.
- `Generic` / `Protocol` themselves are already skipped by the walk.

## Change 3: relax the `Subscriber` / `EventSource` bounds

(Moved here from part 1 — same file, same conceptual change-set.)

In `subscription_types.py`, change `Subscriber[T: FxtrServiceData]`
and `EventSource[T: FxtrServiceData]` to unbounded `[T]`, replacing
the bound with a documented contract: **T must serialize to
`FxtrServiceData`**. Wire conversion is asymmetric (sender's
annotation drives encode, receiver impl's drives decode), so a typed
receiver decoding dicts a dict-typed sender shipped is coherent.
`SubscriberHub[T: FxtrServiceData]` keeps its bound — it genuinely
stores wire values. Audit existing `Subscriber[...]` /
`EventSource[...]` usages for bound reliance (expected: none).

Also rewrite `EventSource`'s "cannot subclass" docstring note
(`subscription_types.py:109-114`) into its positive replacement:
subclass freely; the base is marked subclassable; re-declaring for
docstrings is fine.

## Change 4: migrate the existing workaround sites

All mechanical once changes 1–2 land; wire shapes are unchanged
throughout (dispatch stays by method name), so the frontend and any
persisted state are unaffected.

1. **`fxtr_workstation/dynamic_dispatch/dynamic_dispatch_types.py`**
   - `WorkstationDispatcherProto(EventSource[Any], Protocol)` — drop
     the re-declared `subscribe` body or keep a re-declaration purely
     for its (good) docstring; mark with `allow_subtype_protocols=True`
     (it's itself a base, see next).
   - `NixDispatcherProto(WorkstationDispatcherProto, Protocol)` —
     drop the duplicated `resolve` / `subscribe`, keep `set_preset`.
     Layer 1 alone suffices here (the base's methods are
     non-generic). Update the "Intentionally a standalone Protocol"
     docstring (`:213-222`).
   - Existing `replace_service_type(NixDispatcherProto)` call sites
     keep working unchanged.
2. **Workaround doc-comments** in
   `dynamic_dispatch/basic_dynamic_dispatch/service_impl.py:21` and
   `dynamic_dispatch/nix_configurable_dispatch/service_impl.py:22`.
3. **Demo workdir** `simple_tasks`
   (`examples/fxtr_workstation_demo/agent_workdir/packages/simple_tasks/`):
   `SimpleTaskSequenceSchedulerProto(EventSource[Any], Protocol)`,
   drop the re-declared `subscribe` (`__init__.py:156` area). (If
   part 3 lands first this merges into that edit; ordering either way
   is fine.)
4. **Agent plugin** (`projects/fxtr_workstation_agent_plugin/`):
   - `fxtr_workstation_mcp/src/fxtr_workstation_mcp/agent_mcp_proto.py`:
     `AgentMcpRunningSessionProto(EventSource[EntityRef[WsAgentLogEventChain]],
     Protocol)` — **typed payload upgrade**, not just `Any`: the impl's
     hub is `SubscriberHub[EntityRef[WsAgentLogEventChain]]` and
     `update_head` publishes exactly that ref type
     (`agent_mcp_impl.py:158,177-187`), so the inherited `subscribe`
     gets the substituted `Subscriber[EntityRef[WsAgentLogEventChain]]`
     param. Wire-identical (ref annotations drop on the wire). Keep a
     re-declared `subscribe` purely for its docstring (the
     seed-as-first-event / coalesce semantics are worth keeping
     in-place — now an honest override); delete the "can't subclass"
     caveat paragraph. Mirror the typed subscriber annotation on
     `AgentMcpRunningSessionImpl.subscribe` (`agent_mcp_impl.py:207`).
   - Test event source in
     `fxtr_workstation_mcp/tests/test_callback_e2e.py:539` — subclass
     instead of re-declare.
   - Workaround doc-comments in
     `fxtr_ws_mcp_sandbox_bridge/builtins.py:812-829` (comment-only).
5. **Workstation test fixture**
   (`projects/fxtr_workstation/tests/_fixtures.py:272`): subclass
   instead of re-declare.
6. **Audit sweep**: grep for `wire-compatible` / `re-declar` /
   `cannot subclass` across `v2/`, `projects/`, `examples/` for any
   site the above missed (skip `.cache/wheel_cache` vendored copies).

Run the affected suites: `v2/` pytest, `projects/fxtr_workstation`
pytest, agent plugin (`fxtr_workstation_mcp`) pytest, demo `tests/`.
⚠️ The MCP end-to-end tests are known to sometimes fail or hang when
run concurrently with each other — run that suite serially (no
`-n auto`), and don't read a hang as caused by these changes without
re-running serially.

## Tests (`v2/tests/test_services/`, new `test_protocol_subclassing.py`)

1. Subclass-of-marked-base resolves with its *own* resolver
   (most-specific wins); base-type lookups unaffected.
2. Subclass of an *unmarked* base still raises the conflict error at
   resolve (the guard is opt-in, not removed).
3. Inherited **non-generic** method round-trips over a real
   mount/resolve (the `NixDispatcherProto` shape).
4. Inherited **generic** method with subclass binding: base
   `emit(item: T)`-style method, subclass binds `T=<dataclass>` —
   the impl receives a real decoded instance (proves substitution,
   not bound-fallback). Both `Foo[V](Base[V])` (still-generic) and
   `Foo(Base[Concrete])` (fully bound) variants.
5. Multi-hop chain substitution; unparameterized-base fallback;
   same-name TypeVar collision.
6. Undecorated subclass resolves via the base's resolver (pin the
   documented fallback).

## Implementation checklist

- [x] Change 1: `allow_protocol_subtypes` flag on `@service_protocol`,
      plumbed to `register_service_resolver`; mark `EventSource` /
      `Subscriber`; metadata-inheritance audit note confirmed (no
      fxtr2-internal readers; sandbox-bridge `getattr` readers get
      base-vocabulary behavior on undecorated subclasses, consistent
      with the resolver fallback — no `in vars()` guard needed).
- [x] Change 2: TypeVar substitution for inherited methods in
      `typed_service.py` (`_walk_protocol_methods` reports defining
      class; subst map via `get_original_bases`; hints resolved
      against defining class; `_resolve_unbound` reports defining
      class too, so generic-base TypedServiceImpl subclasses get the
      same substitution; `_substitute_typevars` made public as
      `conversion.base_registry.substitute_typevars`).
- [x] Change 3: relax `Subscriber` / `EventSource` bounds; rewrite the
      `EventSource` docstring note.
- [x] Tests: `v2/tests/test_services/test_protocol_subclassing.py`
      (cases 1–6 above, 18 tests); `v2/` suite green (2497 passed).
- [x] Change 4.1: workstation dispatchers
      (`WorkstationDispatcherProto` / `NixDispatcherProto`) +
      doc-comments; `projects/fxtr_workstation` suite green (376
      passed). Also took the typed payload here:
      `WorkstationDispatcherProto(EventSource[EntityRef[ReloadEverythingNotification]])`
      — both impls' hubs publish exactly that ref type; impl
      `subscribe` annotations mirrored.
- [x] Change 4.2: demo `simple_tasks`
      (`SimpleTaskSequenceSchedulerProto(EventSource[FxtrServiceData])`,
      re-declared `subscribe` kept for docs; `simple_scheduler` impl
      annotation mirrored); demo `tests/` green (8 passed).
- [x] Change 4.3: agent plugin (`agent_mcp_proto` typed upgrade, impl
      annotation, e2e test fixture, bridge doc-comments). Full serial
      MCP suite showed a mid-run fixture-death cascade
      (`ClosedResourceError` setup errors after a per-test timeout) —
      every affected file passes in isolation
      (`test_workstation_message_e2e` 3/3, `test_typed_service_e2e`
      8/8, `test_tool_logging` 4/4, `test_stage7_buffer_e2e` 11/11,
      `test_mcp_in_memory` 5/5, `test_callback_e2e` 5/5), so the
      cascade is the known cross-test interference (live workstation
      MCP sessions were running on the same machine), not this
      change.
- [x] Change 4.4: workstation test fixtures (`HubEventSourceProto` +
      `FailingEventSourceProto` in `tests/_fixtures.py`) + audit
      sweep (remaining "wire-compatible" hits are cross-process
      protocol *mirrors* — sandboxing test mirrors, `prompt_task`,
      the sandbox bridge's `NixDispatcherProto` — which can't import
      the original module and stay standalone by design).
- [x] Update `v2/fxtr2/services/LOCAL_CODE_OVERVIEW.md`,
      `v2/CODE_OVERVIEW.md`, `v2/tests/LOCAL_CODE_OVERVIEW.md`,
      `projects/fxtr_workstation/CODE_OVERVIEW.md`,
      `projects/fxtr_workstation_agent_plugin/CODE_OVERVIEW.md`;
      marked the protocol-inheritance follow-up resolved in
      `projects/fxtr_workstation/plans/phase0/part5_nix_workdir/followups.md`.

## Implementation notes / sharp edges (post-impl)

- **Decorated intermediates must each be marked.** The resolve-time
  conflict check applies to every shadowed handler in the MRO, so a
  decorated chain `C -> B -> A` needs `allow_protocol_subtypes=True`
  on *both* `A` and `B` for `C` to resolve. The error message names
  the offending handler.
- **Undecorated subclasses that bind TypeVars: binding honored.**
  Initially the proxy builder only read `get_args(target_type)`
  (empty for a plain class), so `class Sub(Base[int], Protocol)`
  without `@service_protocol` silently degraded to `Any`-typed
  codecs. Fixed: `_proxy_parameterization` recovers the binding from
  the inheritance chain (composing `_typevar_subst_map` with any
  use-site args), so an undecorated bound subclass resolves with the
  same typed codecs as `Base[int]`. Method *vocabulary* is still the
  base's (an undecorated subclass's added methods aren't exposed —
  decorate to expose them). Pinned in
  `test_undecorated_subclass_keeps_typevar_binding` /
  `test_undecorated_generic_subclass_parameterized_at_use_site`.
- **`_resolve_method_hints` retains the name-keyed `localns`
  injection** — safe because every caller now passes the lexically
  defining class (documented with a warning in its docstring);
  callers added later must preserve that discipline.
- The MCP end-to-end suite interferes with concurrently-running live
  workstation MCP sessions (e.g. the Claude Code plugin's session on
  the same machine) — failures there cascade as
  `ClosedResourceError` setup errors once a shared server fixture
  dies. Re-run the affected files in isolation before attributing
  failures to a change.

## Relationship to stage 13

Part 1 consumes all three: `FxtrChannelStorage[T, V, R]` subclasses
`EventSource[ChannelEvent[T, V, R]]` genuinely (legal once the bound
is relaxed; `subscribe` is inherited with the substituted typed
param via change 2), adding only `status()`. The "faked EventSource"
workaround never enters the streams module.
