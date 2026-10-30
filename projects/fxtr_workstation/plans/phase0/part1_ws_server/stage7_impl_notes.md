# Stage 7 — implementation notes

Per-letter log of things that came up during implementation. Format:
each section captures *what was unexpected, awkward, or worth
revisiting*, not a play-by-play of the work itself.

## 7a — `SystemSource`

### Notes

- **`Registration.source` was the only widening needed.**
  ``EntityRegistration.source`` stayed narrow
  (``RPCConnectionRecord``) because no current or near-term
  use case needs a workstation-internal entity registration. If a
  future stage wants to mount entities under a system source (e.g.
  some kind of "always-loaded constants" namespace), the same
  widening pattern + a parallel
  ``EntitySystemSource`` (or just sharing ``SystemSource``) will be
  needed. Worth flagging at that time; not premature today.

- **`describe_registration_source` helper.** I added a small helper
  (``describe_registration_source(source) -> str``) for use in
  external-name conflict error messages. This kept the three
  conflict-message sites in ``service_impl.py`` (one for services,
  two for entities) uniform. The helper accepts the union directly
  even though the entity sites can today only see
  ``RPCConnectionRecord``; that future-proofs them if entity
  registrations ever go system-sourced.

- **Pyright "unreachable code" noise.** After widening
  ``Registration.source``, basedpyright started flagging the
  ``# System-sourced ...`` *comment line* immediately after the
  ``isinstance(..., RPCConnectionRecord)`` block in
  ``release_registration`` as unreachable code (alongside two
  pre-existing unreachable-code flags at unrelated callsites).
  False positive — the comment is followed by the next branch, not
  any actual statement inside the dead branch. Did not silence; the
  rest of the file already has similar pyright artifacts.

- **Test cycle gotcha.** My first pass at
  ``test_release_registration_for_system_source`` called
  ``register_service`` directly (kept the release-cb ref unused,
  intending to let the session-scope drain auto-release it on
  teardown). The test deadlocked on workstation shutdown because
  of the cycle described in
  ``WorkstationServiceProto.register_service``'s docstring —
  exactly the failure mode that motivates ``expose_service``.
  Switched both new tests to use ``expose_service`` for the
  RPC-side mint; clean teardown. **Worth remembering**: anything
  that writes a *unit* test exercising registration mechanics on
  a live ``WorkstationServer`` should default to ``expose_service``
  even when the test only cares about the registration's existence,
  not its release.

- **Frozen `SystemSource` with a `dict` field.** I used
  ``@dataclass(frozen=True)`` with ``metadata: dict[str, str]``.
  The dict itself is mutable, so technically the "frozen" contract
  only covers the two field references — not the dict's contents.
  In practice, callers never mutate ``metadata`` after construction.
  If we ever need a strictly-immutable contract (e.g. for
  hashing in a set), we'd switch to ``Mapping`` + a frozen
  alternative. Not worth doing yet.

### Things to revisit

- **No** — current shape feels right.

- *Possible* future cleanup: if 7d's debug UI wants to render
  ``system_source_kinds`` *and* their metadata (e.g. show the
  namespace name for a persistence cap), we'll need to either
  promote ``metadata`` into the snapshot (as a parallel
  ``system_sources: list[{kind, metadata}]`` field) or look it up
  separately. Today the snapshot just lists kinds. Cross that
  bridge in 7d if the UI needs more detail.

## 7b — Multi-namespace no-prov persistence cap

### Notes

- **Inheritance chain just worked.** The big design bet — that
  the namespace's deep-fetch source plus an
  ``into_scope=call_scope, copy_into_scope=False`` retrieve would
  Just Work via inheritance — paid off without surprises. The
  three-link chain (``call_scope`` → ``service_scope`` →
  ``root_scope`` → ``ns.scope``) lets retrieved entities load
  through the inheritance bridges that ``register_deep_fetch_source``
  already sets up on each namespace. No special bridging code in
  the impl itself.

- **External-name conflict at startup vs. mid-flight.** The cap
  worker installs its Registration *after* awaiting
  ``bridge_ready``. If an RPC peer raced in and grabbed the same
  ``external_name`` between bridge-ready and our install, the cap
  worker raises and the just-minted ``TrackedServiceRecord`` is
  rolled back via ``maybe_release_service_record``. In practice
  it's a startup-only race (we ``await background_tg.start`` for
  each cap before yielding control to the user), but the
  defensive rollback is worth keeping for safety.

- **`ns.scope` is mountable but we don't check.** The workstation
  drops ``ns.scope`` straight into ``root_scope.include`` without
  asserting it's mountable. ``FxtrNamespace`` always uses
  ``isolate_scope=False`` by default (per the constructors of
  ``SQLiteFxtrNamespace`` / ``GitFxtrNamespace``), so we're fine
  in practice — but it might be worth a defensive check at config
  validation time. Not blocking; revisit if a user trips over it.

- **`PreserveProvPersistenceImpl` API surface stability.** Stage
  7b ships every preserve-prov method as a stub raising
  ``NotImplementedError``, so the cap "lights up" (visible in the
  services snapshot, resolvable by external name, has a stable
  type ID) even before 7c implements the bodies. The test
  exercises this — config + bring-up + invocation-raises — so the
  Type ID and dispatch path are locked in early. 7c only needs
  to swap method bodies.

- **`Note` fixture's `.store(session.scope)` works at every
  level.** I used the existing ``Note`` DataclassEntity from
  ``_fixtures.py`` for every test. That made it easy to pin /
  retrieve / load round-trips without inventing test-specific
  entity types.

- **Pyright `Import ".persistence" could not be resolved`.**
  Pyright's import-resolution against the v2 venv is broken in
  this workspace (every fxtr2 import shows as unresolved); my
  new ``.persistence`` import inherited the same noise. Tests
  pass cleanly via ``uv run pytest``, so this is purely an editor
  signal. Same pre-existing situation we've been ignoring across
  the package.

- **Test for "session invokes via InvokeOp" deferred.** The plan
  text said sessions invoke through ``InvokeOp`` and noted that
  the same impl serves both surfaces. I covered the cap end-to-end
  via RPC peers (much simpler test setup) and left a session-side
  invoke test for stage 7d if there's a unique behavior to verify
  (e.g. result-side ``TrackedEntityRecord`` minting through the
  invoke-result delivery pipeline). The cap impl itself is
  protocol-only — there's no session-vs-RPC branching inside it,
  so the session path almost certainly works by construction.

### Things to revisit

- **Mountable check on user-supplied `ns.scope`.** Optional. If a
  user opens a namespace with ``isolate_scope=True``, attaching
  it via ``include=[ns.scope]`` would silently fail to bridge —
  detection-at-startup would be friendlier than runtime debug.

- **Session-side InvokeOp test for the cap.** Optional. The cap
  doesn't branch on caller identity, so it's likely redundant
  with the RPC-peer tests; consider folding into 7d if some
  surface (refcount on retrieved entity?) actually needs a
  dedicated session-side test.

- **Provenance-bearing entity passed through no-prov pin.** I
  asserted in the plan that no-prov silently drops the prov
  half. We don't currently have a test that *demonstrates* this
  shape — the round-trip test stores a bare ``Note``. Adding a
  layered-FxtrID pin → retrieve → assert-bare test would tighten
  the contract; could land alongside 7c's preserve-prov tests
  for a useful contrast.

## 7c — Preserve-provenance mode

### Notes

- **Added `FxtrScope.create_u_node`.** Mirrors the
  ``step_back_p_ctx`` pattern: ``check_alive``, reject if input
  carries a ``_provenance_parent``, reject if input not tracked
  here, then call ``self._main_arena.create_u_node(lease, wrapped)``.
  Lives in ``v2/fxtr2/data/entity_ref_scope.py``. Four tests in
  ``test_provenance_context.py::TestCreateUNode`` cover the happy
  path, idempotency (u(d) is content-addressed), layered-input
  rejection, and untracked-input rejection. Full v2 suite (2323
  tests) stays green.

- **Layering decisions in `pin`.** ``_layer_for_storage`` handles
  the three input shapes:
  1. Layered ``(d, prov)`` → extract prov half, pin that.
  2. Bare ``d``-tag → wrap with ``create_u_node``, pin u(d).
  3. Bare prov-tag (``n``/``c``/``p``/``u``) with no prov_parent
     → reject as ambiguous. Caller must explicitly layer (via
     ``embed_as_provenance_parent``) or wrap (via
     ``create_u_node``) and re-call.

- **Restoring the layering on retrieve.** ``_restore_layered``
  validates the retrieved bare ID has a prov-tag, then calls
  ``embed_as_provenance_parent`` to produce the layered form.
  The cap relies on the documented invariant that
  ``embed_as_provenance_parent`` finds both halves tracked
  in-scope — see the upstream-bug note below.

### Upstream v2 bug — fixed

(Originally: cross-process restart durability of the preserve-prov
cap failed; in-process round-trip worked. The original diagnosis
pointed at ``MemoryStore._adopt_inheritance_entry``'s ``unwraps_to``
cascade — that turned out to be wrong.)

**Actual root cause** (diagnosed and fixed 2026-05-09): the
divergence wasn't in ``_adopt_inheritance_entry``. It was at the
cap_ir_arena packet-construction layer. When the cap returned a
layered ``(d, u(d))`` FxtrID over RPC, the framework's
``_emit_call_result_packet`` forked a sub_arena over only the
seed_ids (``{u(d)}``) and ran the precomp closure on the *source*
arena. The source's liveset gained ``d`` via the closure walk's
auto-tracking, but the sub_arena (a separate fork) didn't — so when
the client later requested ``load_flat(d)`` through the bridge, the
server's wrapped sub_arena rejected with "not tracked in arena".

**Fix**: see
``v2/plans/phase3/stage8c_user_caps/part5bugfix1_plan.md``. Three
layers:

1. Server-side closure walk runs on the sub_arena fork (renamed to
   ``compute_and_track_precomp_closure`` to make the auto-tracking
   contract explicit).
2. Client-side ``_refcounts`` accounts for precomp-value
   citations, so an abandon doesn't overshoot into ids the precomp
   keeps reachable.
3. Wire abandon ships an introduction-refcount per id (cap-IR
   ``DiscardMessage`` style) so abandon-vs-reintroduction races
   don't accidentally reclaim a re-introduced id.

**Test marker dropped**: ``test_preserve_prov_restart_durability``
no longer carries ``xfail``; it goes green alongside the v2-side
unit tests in ``v2/tests/test_cap_ir_arena.py
::TestPrecompTracksOnServer``.

### Things to revisit

- **More-than-one-level u-chains?** Not tested explicitly:
  ``u(u(d))`` (a u-node wrapping another u-node). The Layer 1 +
  Layer 2 design handles this by construction (the closure walk
  is BFS-recursive; the refcount cascade in ``_drop_to_zero``
  walks every precomp entry keyed by the dropped id), but worth
  adding a dedicated test if/when the use case appears.

- **The cap's provenance-mode contract.** The plan said
  "preserve-prov rejects retrieve of non-prov-tagged payloads".
  ``test_preserve_prov_rejects_non_prov_tag_payload_on_retrieve``
  exercises this by manually pinning a d-tag entity via the
  namespace API. The test passes today (the cap rejects when the
  retrieve resolves to a d-tag). Worth keeping — this is the
  cap's invariant, separate from the cap_ir_arena bug.
