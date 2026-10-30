# Stage 6: Design Decision Log

This document summarizes the design discussion that led to the fxtr entity bridge for Cap-IR.
It captures the initial ideas, alternatives considered, and the reasoning behind each
refinement.

## Problem Statement

Two fxtr2 programs need to exchange fxtr entities over Cap-IR's RPC system and an arbitrary
transport. The challenge is bridging two systems with different models:

- **Fxtr entities** are content-addressed CBOR blobs forming a DAG via strong/weak links.
  They live in scopes/arenas with explicit lifetime management.
- **Cap-IR** operates on IRValue trees with handles (pass-by-reference capabilities), pendings
  (pipelineable futures), tags (application-defined semantics), and primitive leaves.

Key tensions:
- Entities are graphs; IRValues are trees (handles are the only shared-reference mechanism)
- Entities need scopes/arenas for loading; Cap-IR doesn't know about scopes
- Entity serialization is CBOR; IRValue leaves are JSON-compatible primitives
- `RPCTagged` is the natural extension point for adding fxtr-specific semantics

## Initial Ideas

Three access patterns were initially considered for how entity data crosses the RPC boundary:

### 1. Connection-level scope/state

Cache entity IDs on a scope associated with the RPC connection. Users would bind IDs to a
particular scope before sending, and received entities would arrive into that scope.

**Pros**: Simple conceptual model — one scope per connection, entities just appear in it.
**Cons**: Users must manage the connection scope explicitly. Unclear how to handle scope
hierarchy when multiple connections are involved. Entity accumulation in long-lived connections.

### 2. Arenas as RPC capabilities

Explicitly pass arenas as tagged RPC handles. At the Cap-IR level, an arena would be a
capability implementing the arena loading methods.

**Pros**: Arenas are already designed to be passed around as leases on entities. Easy to create
an arena, convert it to a capability, pass it through, and copy data out on the other side.
**Cons**: The arena API is low-level and not intended for end users. Mounting an arena into a
scope requires promising the arena will outlive the scope.

### 3. FxtrEntityRPCBundle (initial user-facing proposal)

A per-entity wrapper coupled to an RPC context:
- **Creation**: Pass a FxtrID/EntityRef and a scope; the RPC context forks an arena to own it.
  Optional `copy_into_rpc_scope` parameter.
- **Usage**: `attach(scope)` to lazily mount into a child of the RPC scope, or `copy_into(scope)`
  to copy into an arbitrary scope.
- **Wire transfer**: Clone the arena → create an arena capability → tag and send → receive and
  wrap in a capability-backed arena → wrap in a new bundle.

The wire representation: each bundle holds its own arena. When sent, the arena becomes a
capability proxy. On the receiving side, a capability-backed arena is created that forwards
loading calls over RPC.

## Evaluation of the Bundle Approach

### Concern: Chattiness of lazy arena proxying

The biggest issue: when loading a received entity, its transitive dependencies (strong/weak
links) each require a round-trip over RPC. For an entity tree with M total nodes, that's O(M)
round-trips. Content-addressed immutable data doesn't inherently need lazy fetching.

**Counterpoint** (raised later): For very large entity trees (gigabytes), you may want to
manipulate only the root node without loading the full tree into memory. Lazy loading via
capabilities is essential for this use case — the ID comes with a capability to later load the
value if needed, without obligating you to load data you don't use.

**Resolution**: Lazy loading via capabilities is the right default. Chattiness can be mitigated
by (a) bulk loading methods on the arena capability, and (b) prefetching optimizations that can
be added later without API changes.

### Concern: Per-entity granularity is expensive and verbose

Each bundle gets its own arena, capability, and tagged wrapper. In a single RPC call with
multiple entities sharing dependencies, this means duplicated dep tracking and no deduplication.
On the user side, wrapping/unwrapping every entity in a bundle adds ceremony:

```python
# With bundles
bundle = FxtrEntityRPCBundle(my_ref, scope=scope)
result_bundle = await cap.dispatch([], Call(bundle))
result_ref = await result_bundle.copy_into(my_scope)

# vs. what it could be
result_ref = await cap.dispatch([], Call(my_ref))
```

### Concern: Scope hierarchy constraints

`attach()` requiring the target scope to be a child of the RPC scope constrains code structure.
If you have two RPC connections and want to use entities from both in one scope, that scope must
descend from both RPC scopes.

## Alternative Considered: Transparent Marshalling with Eager Transfer

An alternative was considered: make entity transfer invisible to users by having the marshalling
layer automatically handle FxtrIDs in IRValues.

- **Outgoing**: Walk the IRValue tree, collect all FxtrIDs and their transitive deps via
  `load_flat()`, replace FxtrIDs with tagged primitives, send all flat data as a batch payload.
- **Incoming**: Detect tags, bulk-load all flat data into a local arena, reconstruct FxtrIDs.

**Pros**: Zero ceremony, batched transfer (one payload per call), no round-trips, deduplication
across entities in the same call.

**Cons**: Eager transfer of the full transitive closure is expensive for large entity graphs.
For the gigabyte-tree-reshape use case, this is catastrophically wrong — you'd transfer gigabytes
of data to reshape only the root node.

**Verdict**: Eager-only transfer is not viable as the sole mechanism. However, eager transfer
could work as a **prefetching optimization** on top of a lazy capability model — send near
dependencies eagerly, use the capability as fallback for deeper deps. This can be added later
without changing the user-facing API.

## The Per-Call Scope Insight

A key realization: **RPC calls are natural scope boundaries**. They have clear ownership,
execute in a defined block, encapsulate a set of entities, and any further entity access
requires new RPC calls (which get their own scopes).

This eliminates the need for both per-entity bundles and connection-level scopes. Instead:

- Each RPC call creates a per-call scope on both the caller and callee sides
- Argument entities flow through the call's scope
- Result entities flow back through the return scope
- The scope lifetime is tied to the call's lifetime

This is cleaner than per-entity bundles (less ceremony, shared arena for all entities in a
call) and more precise than connection-level scopes (per-call isolation, clear lifetime).

## The FxtrRPCHandle / FxtrRPCPending Design

Rather than adding fxtr awareness to Cap-IR itself, the design lifts **out** of Cap-IR and
provides fxtr-aware wrappers:

- **`FxtrRPCHandle`**: Same ownership/lifetime as `RPCHandle`, but `dispatch()` takes a scope
  parameter (defaulting to active scope). The scope serves as both the source for argument
  entities and the lifetime anchor for the resulting pending.

- **`FxtrRPCPending`**: Bound to both the underlying `RPCPending` and the dispatch scope. Torn
  down if either goes down. `pull()` takes `into_scope` and `copy_into_scope` parameters
  mirroring `FxtrNamespace.retrieve()`.

The handles themselves don't carry scope bindings — they're pure capabilities. The scope only
enters the picture at dispatch and pull time.

## Pull Semantics Refinement

The pull semantics went through several iterations before settling on a two-mode design:

### Initial idea: always copy on pull

Always eagerly copy entity data when pulling. Simple, no scope constraints needed.

**Problem**: Prevents lazy loading for large entity graphs. If you pull a result containing
references to gigabytes of data, you'd have to copy it all even if you only need the root.

### Revised idea: lazy pull with scope constraints

Allow lazy mounting, but require the destination scope to be a child of the dispatch scope.

**Problem**: Doesn't account for RPC connection lifetime. If the connection tears down but the
dispatch scope is still alive, lazy-mounted entities become unfetchable, breaking fxtr's closed
subgraph invariant.

### Final design: two-axis constraint

**`pull(into_scope=t)` without copy**:
- `t` must descend from **both** the dispatch scope **and** the RPC connection scope
- This structurally guarantees both local backing data and the remote connection outlive `t`
- Entities are lazily mounted — loading triggers on-demand fetching from the remote side

**`pull(into_scope=t, copy_into_scope=True)`**:
- Only requires the dispatch scope and connection are alive at pull time
- `t` can be any scope
- Entity data is eagerly copied, making it independent of the connection

**Why this works**: The two-axis constraint (dispatch scope ∩ RPC scope) ensures the closed
subgraph invariant is maintained for lazy-mounted entities. The `copy_into_scope` escape hatch
handles cases where entities must outlive the call or connection.

**Practical note**: In the common case, user code runs in a working scope that's a child of the
RPC connection scope, and dispatches from that working scope. The two-axis constraint is
automatically satisfied — no special wiring needed.

## Teardown Semantics

`FxtrRPCPending` tears down when the dispatch scope exits, rather than keeping the scope alive.
This matches fxtr's convention: entity IDs don't keep scopes alive. The scope's lifetime is
managed by the user, and the pending simply becomes unusable when its scope is gone.

**Alternative considered**: Having the pending keep the scope alive. Rejected because it would
be surprising — the only other thing in fxtr that keeps scopes alive is explicitly opening other
scopes as context managers; there are no other values that keep scopes alive until they are deleted,
and this would make scope lifetime management harder to reason about.

## GetAttribute

`GetAttribute` has the same return-side semantics as `Call`, but since it carries no arguments,
there is no dispatch scope. The result's lifetime is tied only to the RPC connection scope.

This means `pull` on a GetAttribute result only requires the destination scope to descend from
the RPC connection scope (single-axis constraint).

**Optimization noted**: The same relaxation could apply to `Call` when the argument contains no
FxtrIDs, since there's no local backing data to keep alive. This can be added later.

## Callee-Side Design

Each incoming call runs in a fresh per-call scope on the remote side, which is a child of the
RPC connection scope. This scope:
- Contains the argument entities (backed by arena capabilities from the caller)
- Is the active scope for the handler
- Captures result entities that the handler stores
- Stays alive until the caller discards the corresponding `FxtrRPCPending`

The user handler is unaware it's inside an RPC call — it just works with FxtrIDs and the
active scope normally.

## Anchor Scope Discussion

A question was raised: should the dispatch scope and the lifetime anchor for the pending be
decoupled? For instance, dispatching with entities from scope `s1` but anchoring the pending to
a longer-lived scope `s2`.

**Decision**: Keep them coupled (dispatch scope = lifetime anchor). If you need a different
anchor, copy the argument entities into the desired anchor scope before dispatching. Putting
the "untying" at pull time (via `copy_into_scope`) rather than dispatch time is more flexible,
because at dispatch time you don't yet know where you'll want the results.

## Cross-Connection Entity Transfer

An edge case discussed: receiving an entity from connection A and sending it to connection B.
With per-call arenas, the data must transit through the local process (fetched from A's remote,
then sent to B's remote).

**Accepted limitation**: This is unavoidable when A and B have no direct communication channel.
However, when A and B are handles on the same connection, the existing Cap-IR bounce-back
optimization should eliminate the network round-trip. Direct bridging of two remotes could be
added later as a Cap-IR extension if needed.

## FxtrRPCContext Ownership and the Layered Stream Model

A final design question: how should `FxtrRPCContext` relate to the transport?

### Initial approach: FxtrRPCContext owns the transport

The first thought was that `FxtrRPCContext` should own and instantiate the transport (JSON or
simple JSON) to ensure it can detect teardown. This couples the fxtr layer to transport details.

### Concern: separation of concerns

The fxtr bridge layer's job is scope management, arena capabilities, and FxtrID marshalling.
The transport's job is wire serialization, import/export tables, and the message loop. These are
independent concerns that compose better as separate layers.

### Resolution: transport-independent FxtrRPCContext + abort on unexpected teardown

`FxtrRPCContext` takes a Cap-IR ObjectStream (not a transport). It monitors the capabilities
flowing through it for teardown. If the underlying transport dies while `FxtrRPCContext` is
still open, all handles/pendings tear down, and `FxtrRPCContext` detects this and enters an
error state — aborting its scope as a fast signal that the closed subgraph invariant is broken.

In natural usage, you nest `FxtrRPCContext` inside a transport context manager, so the transport
outlives the fxtr layer. If you do something unusual (close the transport while leaving the
fxtr context open), it aborts immediately rather than allowing silent dangling references.

### The ObjectStream abstraction

This led to a layered stream model where each layer lifts a bidirectional stream:

1. **Raw JSON stream** → Cap-IR transport → **Cap-IR ObjectStream** (IRValues with handles)
2. **Cap-IR ObjectStream** → FxtrRPCContext → **FxtrRPC ObjectStream** (IRValues with FxtrRPCHandles)

The ObjectStream at the Cap-IR level can be implemented as a thin main-interface wrapper:
each side exposes `{receive: (value) -> None}`, sending is calling `receive` on the remote
side. This is lightweight and was already a planned future addition.

**Key benefit**: `FxtrRPCContext` works with any transport — JSON, simple JSON, CBOR, or local
in-memory channels. For testing, the fxtr bridge can be exercised with local caps and no
network, using the same test suite.

### Top-level stream values vs call-level values

An important consequence: at the top level of the FxtrRPC ObjectStream, there are **no FxtrIDs**
(since there's no call scope for them to live in). FxtrIDs only appear inside calls dispatched
on `FxtrRPCHandle`s. The stream carries capabilities (handles); the capabilities carry entities.

## Implementation Refinements

Several refinements emerged during implementation planning:

### FxtrRPCValue type aliases instead of IRValue[FxtrLeaf]

Initially the plan used `IRValue[FxtrLeaf]` as the value type. This was revised: the conversion
should produce `FxtrRPCValue` / `FxtrRPCCallValue` directly, with `FxtrRPCHandle` and
`FxtrRPCPending` as members of the value type (not raw Cap-IR handles). This way the
wrapping of handles happens in the same pass as encoding/decoding FxtrIDs, and users never see
raw Cap-IR types. Two aliases enforce the invariant that bare FxtrIDs only appear inside calls.

### Single concrete FxtrRPCHandle wrapping a Cap-IR handle

Rather than having separate local/remote implementations of `FxtrRPCHandle`, there is one
concrete type that wraps a Cap-IR `RPCHandle`. The difference between local and remote is how
the underlying Cap-IR handle was created — `LocalFxtrRPCContext.wrap()` creates it via
`LocalRPCContext`, while `RemoteFxtrRPCContext` receives it over the stream. The `FxtrRPCHandle`
doesn't care.

### LocalFxtrRPCContext / RemoteFxtrRPCContext split

Instead of a single `FxtrRPCContext`, two context types parallel the Cap-IR level:

- **`LocalFxtrRPCContext`**: Wraps Python objects as `FxtrRPCHandle`s with scope management.
  No stream, no transport. Good for in-process use and testing.
- **`RemoteFxtrRPCContext`**: Lifts a Cap-IR `ObjectStream` to an `FxtrRPCValue` stream.
  Requires a transport underneath. Also internally owns a `LocalRPCContext` for creating
  arena capability handles.

Both produce the same `FxtrRPCHandle` type. The local context can be used independently for
testing, or composed with a remote context for full RPC.

### Arena capability follows HTTP arena pattern

The `RemoteArenaAdapter` follows the same pattern as `HttpReadableArena` in
`experimental/http/client.py`: precomp dicts for provenance relationships, tracking sets for
known IDs, and the same loading protocol. No local caching needed — the scope handles caching
when entities are loaded through the arena.

### Discard notification as a prerequisite enhancement

Arena capability lifetime management (knowing when to clean up per-call scopes on the callee
side) requires a discard-notification mechanism on `LocalRPCContext` / `LocalHandle`. When
all clones of a wrapped handle are discarded, a callback fires. This is a small Cap-IR level
enhancement that should be implemented first.

On `FxtrRPCContext` exit, all per-call scopes are forcibly closed regardless of discard state,
bounding cleanup to the context manager lifetime.

## Summary of Design Evolution

```
Initial ideas:
  ├─ Connection-level scope (too coarse, accumulation issues)
  ├─ Arenas as capabilities (right primitive, too low-level for users)
  └─ Per-entity bundles (correct lifetime model, too verbose)

Key insight: RPC calls are natural scope boundaries
  └─ Per-call shared arenas (deduplication, one capability per call)

User-facing API:
  ├─ FxtrRPCValue / FxtrRPCCallValue type aliases
  ├─ FxtrRPCHandle (single concrete type wrapping Cap-IR handle)
  └─ FxtrRPCPending (bound to RPCPending + dispatch scope)
       └─ Two-mode pull (lazy with two-axis constraint, or eager copy)

Architecture: layered stream model
  ├─ Raw JSON stream → Cap-IR ObjectStream → FxtrRPC ObjectStream
  ├─ LocalFxtrRPCContext (in-process, no transport)
  ├─ RemoteFxtrRPCContext (stream-based, transport-independent)
  ├─ Abort on unexpected teardown (fast signal for invariant violation)
  └─ Top-level stream carries handles; calls carry entities

Cap-IR internals:
  ├─ Single-pass conversion (handles + FxtrIDs + bytes in one walk)
  ├─ Per-call arena capabilities (internal, HttpReadableArena pattern)
  ├─ Discard notification for arena capability lifecycle
  └─ Forwarding optimization (skip remote fetch for locally-known entities)
```
