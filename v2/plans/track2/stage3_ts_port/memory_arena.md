# Memory Arena Spec

TS-specific considerations for the 1:1 port of abstract arena interfaces, MemoryStore, and MemoryArena.

## Overview

The arena layer sits between the core (entityCore, visitor) and the scope layer. It provides:

- **`arena/types.ts`**: Abstract interfaces (`ReadableArena`, `CopyTargetArena`, `ReadWriteArena`, `DeepFetchable`) and data types (`DepKind`, `FxtrEntityFlatData`)
- **`arena/memoryArena.ts`**: In-memory implementation (`MemoryStore`, `MemoryArena`)

## What we're porting

From `abstract_arena.py`:
- `ReadableArena` (is_tracked, load, load_flat, unwrap_provenance, c_node_to_context_data, p_node_to_pathroot, step_back_p_ctx, abandon, copy_to_new_arena, close)
- `CopyTargetArena` (copy_from_arena)
- `ReadWriteArena` (store, track_builtin, create_c_node, create_p_node, create_u_node)
- `DepKind` enum
- `FxtrEntityFlatData`
- `DeepFetchable` (replaces `ObservableStore`, just `tryLookup`)

From `memory_arena.py`:
- `MemoryStore` — backing store with node states, refcounting, GC
- `MemoryArena` — arena view with tracked liveset
- `InMemoryBytesBackingStore` — simple bytes store with refcounting
- Node state types: `StoredNode`, `DerivedNode`, `InheritedNode`

## What we're NOT porting

- `LMDBBytesBackingStore` — no filesystem stores in TS
- `StreamSink` / `MemoryStreamSink` — experimental, not needed
- `PersistentPinningBackend` — no persistent stores yet in TS
- `EntityBackref` / `OtherBackref` / `backrefs()` — not needed; DeepFetchable replaces ObservableStore
- `force_async_sleep` — testing utility, skip
- `DEBUG_DROPPED_ARENAS` / `weakref.finalize` leak detection — skip for now
- Detailed `logger.debug()` calls — skip for now (can add later if needed)

## Async store (key difference from Python)

In Python, `ReadWriteArena.store()` and `MemoryStore.bare_store()` are sync because SHA-256 is sync. In TS, SHA-256 uses Web Crypto API (`crypto.subtle.digest`), which is async. This means:

- `ReadWriteArena.store()` → `Promise<FxtrID>`
- `MemoryStore.bareStore()` → `Promise<FxtrID>`
- `MemoryStore.bareStoreFlat()` → `Promise<FxtrID>` (stores d/n nodes which need backing store save)
- `ReadWriteArena.createCNode()` → `Promise<FxtrID>` (calls bare_store internally)
- `ReadWriteArena.createPNode()` → `Promise<FxtrID>` (calls bare_store internally)
- `ReadWriteArena.createUNode()` → `Promise<FxtrID>` (calls bare_store internally)
- `ReadWriteArena.trackBuiltin()` → stays sync (no hashing needed)

The backing store's `saveAndIncrement()` also becomes async since it wraps the hash computation.

Note: Some methods like `bareStoreFlat()` (pre-computed ID) and `createUNode()` (ID derived from wrapped ID, no hashing) may not strictly need to be async. After implementation, we may want to revisit and make some of these sync if they don't need async and are sometimes called from sync contexts. For now, keeping them async is simpler and more flexible.

## arena/types.ts

### DepKind

```typescript
enum DepKind {
  STRONG_REF = "strong_ref",
  WEAK_REF = "weak_ref",
  CTX_NODE = "ctx_node",
  BACKSTEP = "backstep",
  UNWRAPS_TO = "unwraps_to",
}
```

### FxtrEntityFlatData

```typescript
interface FxtrEntityFlatData {
  readonly entityId: FxtrID;
  readonly entityBytes: Uint8Array | null; // null for derived nodes
  readonly deps: ReadonlyArray<readonly [FxtrID, DepKind]>;
}
```

### ReadableArena

```typescript
interface ReadableArena {
  isTracked(nodeId: FxtrID): boolean;
  load(nodeId: FxtrID): Promise<FxtrCbor>;
  loadFlat(nodeId: FxtrID): Promise<FxtrEntityFlatData>;
  unwrapProvenance(nodeId: FxtrID): FxtrID | null;
  cNodeToContextData(cNodeId: FxtrID): FxtrID;
  pNodeToPathroot(pNodeId: FxtrID): FxtrID;
  stepBackPCtx(pNodeId: FxtrID): FxtrID | null;
  abandon(nodeId: FxtrID): void;
  copyToNewArena(nodeIds: Iterable<FxtrID>): ReadableArena;
  close(): void;
}
```

### CopyTargetArena

```typescript
interface CopyTargetArena extends ReadableArena {
  copyFromArena(entityId: FxtrID, source: ReadableArena): Promise<void>;
}
```

### ReadWriteArena

```typescript
interface ReadWriteArena extends CopyTargetArena {
  store(entity: FxtrCbor): Promise<FxtrID>;  // async due to SHA-256
  trackBuiltin(nodeId: FxtrID): FxtrID;
  createCNode(contextData: FxtrCbor): Promise<FxtrID>;  // async due to SHA-256
  createPNode(pathrootData: FxtrCbor): Promise<FxtrID>;  // async due to SHA-256
  createUNode(wrappedId: FxtrID): Promise<FxtrID>;  // async due to recursive store
  close(): void;
}
```

### DeepFetchable

```typescript
interface DeepFetchable {
  tryLookup(nodeId: FxtrID): Promise<ReadableArena | null>;
}
```

Replaces Python's `ObservableStore`. Only `tryLookup` — no `backrefs`.

## arena/memoryArena.ts

### Node state types

Use a discriminated union with a `kind` field:

```typescript
interface StoredNode {
  kind: "stored";
  myId: FxtrID;
  requires: ReadonlySet<string>;          // FxtrID keys
  strongLinkedRefs: ReadonlySet<string>;   // FxtrID keys
  unwrapsTo: FxtrID | null;
  requiredBy: Set<string>;                 // FxtrID keys or ArenaKey strings
  wrappedBy: Set<string>;                  // FxtrID keys
}

interface DerivedNode {
  kind: "derived";
  myId: FxtrID;
  requires: ReadonlySet<string>;
  ctxNode: FxtrID | null;
  backstepNode: FxtrID | null;
  unwrapsTo: FxtrID | null;
  requiredBy: Set<string>;
  wrappedBy: Set<string>;
}

interface InheritedNode {
  kind: "inherited";
  myId: FxtrID;
  inheritanceParentArena: ReadableArena;
  unwrapsTo: FxtrID | null;
  requiredBy: Set<string>;
  wrappedBy: Set<string>;
}

type NodeState = StoredNode | DerivedNode | InheritedNode;
```

Note: `requires`, `requiredBy`, `wrappedBy` all use `string` keys (via `id.key()`), matching our convention for FxtrID as Map key.

For `requiredBy`, Python uses `FxtrID | ArenaKey`. In TS, we use strings for both — arena keys can be prefixed (e.g., `"arena:0"`) to distinguish from FxtrID key strings.

### InMemoryBytesBackingStore

```typescript
class InMemoryBytesBackingStore {
  private data: Map<string, Uint8Array>;   // keyed by FxtrID key
  private counts: Map<string, number>;

  saveAndIncrement(key: string, value: Uint8Array): void;
  get(key: string): Uint8Array;
  free(key: string): void;
}
```

### MemoryStore

1:1 port of Python's MemoryStore minus debugging infrastructure. Key methods:

```typescript
class MemoryStore implements DeepFetchable {
  // Internal state
  private entityStates: Map<string, NodeState>;     // keyed by FxtrID key
  private arenaCounter: number;
  private arenas: Map<string, MemoryArenaState>;    // keyed by ArenaKey string
  private deadPreCleanup: Set<string>;              // FxtrID keys
  private freshNodes: Set<string>;                  // FxtrID keys
  private backingStore: InMemoryBytesBackingStore;

  // Lifecycle
  close(closeArenas?: boolean): void;

  // Arena management
  newEmptyArena(): MemoryArena;
  trackInArena(nodeId: FxtrID, arenaKey: string): void;
  untrackFromArena(nodeId: FxtrID, arenaKey: string): void;
  closeArena(arenaKey: string): void;

  // Storage (async due to SHA-256)
  bareStore(entity: FxtrCbor): Promise<FxtrID>;
  bareStoreFlat(flatData: FxtrEntityFlatData): Promise<FxtrID>;
  bareLoad(nodeId: FxtrID): Promise<FxtrCbor>;
  bareLoadFlat(nodeId: FxtrID): Promise<FxtrEntityFlatData>;

  // Node operations (sync — no hashing needed)
  unwrapProvenance(nodeId: FxtrID): FxtrID | null;
  trackBuiltin(nodeId: FxtrID): FxtrID;
  createUNode(wrappedId: FxtrID): Promise<FxtrID>;   // async: recursive store
  createCNode(contextData: FxtrCbor): Promise<FxtrID>;  // async: calls bareStore
  createPNode(pathrootData: FxtrCbor): Promise<FxtrID>; // async: calls bareStore

  // Provenance operations (sync)
  cNodeToContextData(cNodeId: FxtrID): FxtrID;
  pNodeToPathroot(pNodeId: FxtrID): FxtrID;
  stepBackPCtx(pNodeId: FxtrID): FxtrID | null;

  // GC
  private addRequirement(nodeId: FxtrID, requiredBy: string): void;
  private collectOneGarbage(): boolean;
  private maybeMarkDead(nodeId: FxtrID): void;

  // Lazy attachment
  bareLazilyAttachFromArena(nodeId: FxtrID, source: ReadableArena): void;

  // DeepFetchable
  tryLookup(nodeId: FxtrID): Promise<ReadableArena | null>;

  // Internal
  private ensureOwn(nodeId: FxtrID): Promise<StoredNode | DerivedNode>;
  private ensureOwnDerived(nodeId: FxtrID): DerivedNode;
  private saveOrUpgradeNode(newNode: StoredNode | DerivedNode): void;
  private adoptInheritanceArenaClaim(nodeId: FxtrID, parentArena: ReadableArena): NodeState;
  private alreadyHasDataFor(nodeId: FxtrID): boolean;
}
```

### MemoryArena

```typescript
class MemoryArena implements ReadWriteArena {
  private store: MemoryStore;
  private arenaKey: string;
  private closed: boolean;

  // ReadableArena
  isTracked(nodeId: FxtrID): boolean;
  load(nodeId: FxtrID): Promise<FxtrCbor>;
  loadFlat(nodeId: FxtrID): Promise<FxtrEntityFlatData>;
  unwrapProvenance(nodeId: FxtrID): FxtrID | null;
  cNodeToContextData(cNodeId: FxtrID): FxtrID;
  pNodeToPathroot(pNodeId: FxtrID): FxtrID;
  stepBackPCtx(pNodeId: FxtrID): FxtrID | null;
  abandon(nodeId: FxtrID): void;
  copyToNewArena(nodeIds: Iterable<FxtrID>): MemoryArena;
  close(): void;

  // CopyTargetArena
  copyFromArena(entityId: FxtrID, source: ReadableArena): Promise<void>;

  // ReadWriteArena (async due to SHA-256)
  store(entity: FxtrCbor): Promise<FxtrID>;
  trackBuiltin(nodeId: FxtrID): FxtrID;
  createCNode(contextData: FxtrCbor): Promise<FxtrID>;
  createPNode(pathrootData: FxtrCbor): Promise<FxtrID>;
  createUNode(wrappedId: FxtrID): Promise<FxtrID>;

  // MemoryArena-specific
  unsafeLazilyAttachFromArena(entityId: FxtrID, source: ReadableArena): void;
  copyFromSiblingMemoryArena(entityId: FxtrID, source: MemoryArena): void;
  clone(): MemoryArena;
}
```

## SHA-256 hashing

Use Web Crypto API for cross-platform compatibility (browsers + Node.js 18+):

```typescript
async function sha256(data: Uint8Array): Promise<Uint8Array> {
  const hash = await crypto.subtle.digest("SHA-256", data);
  return new Uint8Array(hash);
}
```

This lives in `entityCore.ts` or a small utility, since it's used by `bareStore` to compute content hashes for d/n nodes.

## Refcounting and GC

Same model as Python:

- Each node has `requiredBy` (set of FxtrID keys and arena key strings) and `wrappedBy` (set of FxtrID keys)
- A node is "dead" when both `requiredBy` and `wrappedBy` are empty
- Dead nodes go into `deadPreCleanup` set
- `collectOneGarbage()` removes one dead node and may cascade (its dependencies may become dead)
- No cycles are possible (content-addressed, immutable entities)
- Arena close releases all arena's requirements, potentially cascading GC
- `MemoryStore.close()` bulk-clears everything

Arena key strings use a prefix to distinguish from FxtrID keys: `"_arena:N"` where N is the counter.

## Inherited nodes and lazy attachment

Same model as Python:

- `bareLazilyAttachFromArena()` creates a `copy_to_new_arena` claim and stores an `InheritedNode`
- When a node is needed, `ensureOwn()` loads it from the parent arena and upgrades it to a `StoredNode` or `DerivedNode`
- `adoptInheritanceArenaClaim()` handles deduplication: if we already have the node (stored/derived), abandon the claim; if we have an InheritedNode from a different parent, keep the existing one
- InheritedNodes track `unwrapsTo` immediately (via recursive claim adoption) so provenance operations work without loading

## Key differences from Python

| Aspect | Python | TypeScript |
|--------|--------|-----------|
| store/createCNode/etc. | sync | async (SHA-256) |
| ObservableStore | try_lookup + backrefs | DeepFetchable (tryLookup only) |
| Backing store | InMemory or LMDB | InMemory only |
| Map keys | FxtrID directly (custom __hash__) | string via `id.key()` |
| Node states | dataclass instances | discriminated union with `kind` field |
| Arena keys | ArenaKey dataclass | `"_arena:N"` string |
| Leak detection | weakref.finalize | not ported |
| Debug logging | extensive logger.debug | not ported (can add later) |
| StreamSink | MemoryStreamSink | not ported |

## File mapping

| Python | TypeScript | Contents |
|--------|-----------|----------|
| `abstract_arena.py` (interfaces) | `src/arena/types.ts` | ReadableArena, CopyTargetArena, ReadWriteArena, DeepFetchable, DepKind, FxtrEntityFlatData |
| `memory_arena.py` | `src/arena/memoryArena.ts` | MemoryStore, MemoryArena, InMemoryBytesBackingStore, node state types |
