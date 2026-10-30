# Chunk 0 — open-modes design round (decision gate)

**Status: complete.** Outcome (a): the simplification landed in this
chunk (suite + pyright green; the reworked mode-rejection tests assert
the new enforcement points — shed-on-slice, write-through-window faults,
internals attaches read-only). The settled design ("opens follow the
chain"):

- **Opens carry no mode**: every open/attach resolves to the chain's
  authority (the current "inherit" resolution becomes the only one).
  Creates and writes are enforced against chain authority, exactly where
  the structural rules already live (sliced ⇒ read via ``bind_keeps``,
  internals ⇒ read from the top, child ≤ grant, handle doctrine
  kind-keyed).
- **`read_only()` is a holder-local privilege drop**: a flag on the local
  view wrapper (the existing ``Shadow.mode``), enforced by the local API,
  *not* a capability-level fact — the true spec keeps chain authority.
  The ``read_only()``-before-``at()`` ritual is dropped: ``at()`` on a
  subtree view is allowed directly and sheds create authority itself
  (locally and at the wire, which already did).
- **Wiring populates from the local flag** (unchanged mechanics):
  ``WiredArg.mode`` = the forwarded view's effective (flag-adjusted)
  mode; ``SliceBinding.mode`` stays the one identity-bearing site and is
  enforced for the *child* at the capability level. Boundaries enforce;
  self-discipline advises.
- **Consequence, accepted: the wiring default flips to write** for
  undropped creator-side views. Mitigations: write-mode subtree inputs
  fail loud at ``open_task`` (full-frame check), and the linter gains a
  tightening hint ("write-granted input no generation ever wrote" —
  added to chunk 5). Sticky-identity caveat noted (tightening a binding
  later is a rule change = a new grid in v0).
- **Consequence, accepted: ``asserted_grid`` broadens** to "open through
  write authority" — a reader through its own write chain is a
  co-claimant, which is *faithful* (that open literally would create the
  grid in an empty world; whoever runs first creates, the rest attach).
  ``notes/grounding.md`` wording updated accordingly; would-create =
  opened-through-write-authority, recorded monotone-OR per generation
  (chunk 1).
- **The one explicit-affordance remnant**: top-level handle creation
  (authority minting is deliberate) keeps an explicit create flag on the
  user ``open_handle``; task-side handle opens stay attach-only read.
- Users gain ambient write authority through every view (root is write);
  accepted — conflicts stay loud and writes auto-source under phase 2.

A design round, not an implementation chunk: settle how open modes should
work *before* chunk 1 bakes a mode-derived bit into the schema. Modes
have repeatedly gotten in the way (mode-inheritance rules, downgrade
special cases, the escalation bugs the phase-1 rounds fixed), and there
is a suspicion they carry less real semantic weight than their surface
area suggests — every open already asserts a full canonical config
regardless of mode; mode only adds the create-if-missing question and
authority ceilings.

## The question

Should opens keep explicit caller-declared modes, or should the system
infer the most capable mode the holder's authority allows ("auto-max"),
with mode disappearing from the calling convention?

Invariants any outcome must preserve (the settled doctrine):

- **Slicing sheds create authority** (`view_spec.bind_keeps`); sliced
  windows are look-only all the way down.
- **Child mode never exceeds the subtree grant's mode** — internals
  browsing stays read-only (`view_spec.open_child`).
- **Tasks never mint handle authority** (no handle-grid creation or
  handle writes from task code).
- **Read-xor-write** is about *performed ops* per grid per generation,
  not declared modes — expected untouched, confirm.
- The `read_only()`-before-`at()` requirement is intent-visibility, not
  enforcement — decide whether auto-max keeps, replaces, or drops it.

## The chunk-1 coupling: the would-create bit

Chunk 1 records, per `grid_config` observation, **would-create**: "if
this open were re-run in an empty world, would it create the grid?" —
which is what `asserted_grid` in `notes/grounding.md` consumes. The bit
is well-defined under either mode design (explicit modes: the requested
mode is create-capable; auto-max: the holder had create authority at the
path), so this round does not block chunk 1 on the refactor itself — but
it must settle:

- **The hit-matching rule.** Within one generation, two opens of the same
  grid with different capability (e.g. read-mode then write-mode): is
  would-create part of the record payload (mismatch →
  `NondeterminismError`), or does the record upgrade to the max
  capability with replay comparing against the final record? The safety
  pass's set comparison needs the answer to be stable across replay.
  (Under auto-max the question may dissolve — same holder, same
  authority, same bit.)
- That user-side grounding is insulated either way: user sourcing is
  explicit records, never open-derived.

## The gate

Two acceptable outcomes; pick at the end of the round:

- (a) The simplification is small enough to land here (view layer +
  canonicalizer + wire), with the existing axis-semantics suites as the
  safety net.
- (b) The decision is recorded (in the canonical docs), the would-create
  pencil is confirmed compatible with it, and the refactor is deferred to
  its own later work.

Either way chunk 1 proceeds immediately after.

## Inventory: every place declared modes change behavior (2026-07-06)

Line numbers are a snapshot; the function names are the stable reference.

**A. Identity-bearing (stored, participates in config equality) — one site:**

- `model.SliceBinding.mode` — every rule input stores the granted mode,
  inside `TaskGridConfig.rule` → the config fingerprint. Captured by the
  canonicalizer from the forwarded view's *effective* mode
  (`view_spec.canonicalize_open_task` → `translate_arg`), which crosses
  the wire as `runner_api.WiredArg.mode` and is validated as a downgrade
  of the grant (`spec_registry.wired_spec`). This is where mode does
  irreplaceable semantic work: **the rule declares which inputs the task
  may write** — the task's statically-auditable write surface — and it is
  observable by task code (a write on a read view fails), so the
  observability principle makes it identity-bearing.

**B. Runtime enforcement of effectful ops (all consult the *effective*
mode of the spec behind the token):**

- Cell writes require write mode: `ops_engine` (`_write_cell` guard),
  `user_service` (same guard), checkpoint writes (`open_checkpoint`
  guard).
- Creates require a write-mode subtree: `ops_engine._open` /
  `user_service._open` (attach path is mode-free; create path checks).
- Read-xor-write (`ReadWriteExclusionError`) is **op-level over the
  record** — which ops the generation performed per grid — and never
  consults declared modes. Confirmed untouched by any mode redesign.

**C. Structural rules keyed on mode (the invariants' homes):**

- `view_spec.check_subtree_full_frame` + `open_child`: write-mode subtree
  views must be total (batch invariance); read-mode may be sliced
  (browsing windows).
- `view_spec.bind_keeps`: slicing a subtree spec downgrades it to read —
  **slicing sheds create authority** (wire-level enforcement; the view
  layer's `read_only()`-before-`at()` is intent-visibility on top).
- `view_spec.open_child` + both `_open`s: **child mode never exceeds the
  grant's**; mode-less opens (nested subtrees, `attach` subtree children)
  *inherit* the grant's mode.
- Internals: the executing cell's own internals root is minted write
  (`view_spec.internals_spec`); the top level forces read everywhere
  under internals roots (`user_service.attach` / `attach_internals`).

**D. Calling-convention surface (mode as API parameter / rewrite):**

- `mode=` parameters on `open_data` / `open_dataframe` / `open_handle` /
  `attach` (default `"read"`); `open_subtree` takes none (inherits).
- `views.GridView.read_only()` → `view_runtime.Shadow` effective-mode
  downgrade (pure, holder-local; upgrades don't exist). Required before
  `at()` on subtree views (visibility, not enforcement — see C).
- Handle creation demands explicit `mode="write"` at the top level
  (`user_service._open`'s handle branch); tasks attach handles read-only
  (authority doctrine).
- Fixed-mode views: coord views and output views are read by
  construction; task grants are read-shaped.

**E. Wire:**

- `runner_api.ViewGrant.mode` — shadow bookkeeping so the holder can
  enforce/display locally; the registry's true spec is authoritative.
- `runner_api.WiredArg.mode` — the one identity-bearing wire crossing
  (feeds A).

## Analysis: the auto-max direction against the inventory

Mode is doing **two different jobs**, and auto-max separates them
cleanly:

1. **Plumbing authority** (B, C, D): "may this view write/create here" is
   already *determined by the grant chain* — root/binding authority,
   child ≤ parent, sliced ⇒ read, internals-from-top ⇒ read. The explicit
   `mode=` parameters mostly restate what the chain implies; enforcement
   could key off chain authority directly. This is where auto-max works
   and deletes real surface (the `mode=` params, most effective-mode
   threading).
2. **Declared write-intent in rule wiring** (A): auto-max here would be
   wrong — it would make every rule maximally write-capable wherever the
   creator's ambient authority allowed, coupling config identity to
   ambient authority and destroying the static write-surface audit (which
   grounding's claims story leans on). Write-intent must stay an
   explicit, identity-bearing declaration.

**The crux the round must decide**: today, wiring mode rides the
forwarded view's effective mode (author calls `read_only()` before
wiring; default open mode is read, so the safe default is read). Under
auto-max plumbing, views default to their most capable mode — so if
wiring kept riding view mode, rule inputs would *default to write*, a
flipped safe default. So auto-max forces the write-intent declaration to
move to the wiring site itself (e.g. a per-arg `Write(...)` wrapper or
`writes={...}` in `open_task`), with views otherwise mode-less. That is
arguably where it always belonged (declared exactly where it is
identity-bearing), but it is a real API change to `open_task` and the
view layer.

Invariant check under auto-max + explicit wiring intent:

- Slicing sheds create authority: unchanged (`bind_keeps` keys on
  sliced-ness, not declared mode).
- Internals look-only from the top: unchanged (keyed on internals-ness).
- Tasks never mint handle authority: unchanged (kind-keyed); top-level
  handle creation keeps an explicit create affordance (today's
  `mode="write"`, or a `create=` flag).
- Read-xor-write: untouched (op-level).
- `read_only()`-before-`at()`: the visibility rule becomes moot for
  slicing (auto-shed); `read_only()` survives only if wiring intent
  keeps using it (it doesn't, under the explicit-wiring-site option).
- New exposure to note: the *user* gains ambient write authority through
  every view (root is write). Accidental writes are loudly conflicted
  (`WriteConflictError` without `overwrite`) and auto-sourced, so the
  risk is bounded; worth an explicit decision.

**Would-create under either outcome**: "the opener had create authority
on the subtree at this open" — explicit mode today, chain authority under
auto-max. The hit-matching question exists *regardless of the outcome*
(one generation can open the same grid through a write chain and a read
window): resolve by making asserted-ness **monotone per generation** —
the record accumulates OR over the generation's opens ("some open would
have created"), replay re-accumulates the same OR (determinism), and the
safety pass compares the final accumulated record. No per-op payload
mismatch rule needed.

Notes/log: inventory + analysis above (2026-07-06); decision pending
discussion — the crux is whether to move wiring write-intent to the
`open_task` call site and make views mode-less (the full auto-max
outcome), or keep view-carried modes and only delete the open-call
`mode=` params.
