/**
 * Abstract interfaces for fxtr arenas and storage backends.
 *
 * Ported from Python's abstract_arena.py. An "arena" represents a lease on a
 * set of entities — a promise that certain data will remain accessible.
 *
 * Key abstractions:
 * - ReadableArena: Can load entities and track what's accessible
 * - CopyTargetArena: Can also copy entities from other arenas
 * - ReadWriteArena: Can also store new entities
 * - DeepFetchable: Can look up entities by ID (for deep fetch)
 */

import type { FxtrCbor, FxtrID } from "../core/entityCore.js";
import type { PromiseOrEager } from "../util/eager.js";

// ---------------------------------------------------------------------------
// DepKind
// ---------------------------------------------------------------------------

/** Kind of dependency relationship between entities. */
export enum DepKind {
	STRONG_REF = "strong_ref",
	WEAK_REF = "weak_ref",
	CTX_NODE = "ctx_node",
	BACKSTEP = "backstep",
	UNWRAPS_TO = "unwraps_to",
}

// ---------------------------------------------------------------------------
// FxtrEntityFlatData
// ---------------------------------------------------------------------------

/**
 * Flat representation of an entity's data and dependencies.
 *
 * For d/n nodes: entityBytes is the raw CBOR bytes.
 * For derived nodes (c/p/u/./!): entityBytes is null.
 */
export interface FxtrEntityFlatData {
	readonly entityId: FxtrID;
	readonly entityBytes: Uint8Array | null;
	readonly deps: ReadonlyArray<readonly [FxtrID, DepKind]>;
}

// ---------------------------------------------------------------------------
// ReadableArena
// ---------------------------------------------------------------------------

/**
 * An arena of entities that can be read from.
 *
 * Arenas manage the mapping between FxtrIDs and their data. Whenever you
 * receive a FxtrID from an arena, the arena promises it will be able to load
 * the entity data for that ID as long as the arena is active, unless you
 * explicitly abandon the ID.
 */
export interface ReadableArena {
	/** Check if a node is tracked in this arena. */
	isTracked(nodeId: FxtrID): boolean;

	/**
	 * Load an entity from the fxtr graph.
	 *
	 * The entity must be tracked in the arena. After loading, every ID
	 * referenced by the entity will also be tracked.
	 */
	load(nodeId: FxtrID): PromiseOrEager<FxtrCbor>;

	/**
	 * Load flat entity data without deserializing CBOR.
	 *
	 * Returns raw bytes (for d/n nodes) and the full dependency list.
	 * All dep IDs will be tracked in the arena after this call.
	 */
	loadFlat(nodeId: FxtrID): PromiseOrEager<FxtrEntityFlatData>;

	/**
	 * Unwrap a provenance node.
	 *
	 * Returns the data node ID it unwraps to, or null if the node is not
	 * a provenance node.
	 */
	unwrapProvenance(nodeId: FxtrID): FxtrID | null;

	/** Get the context data node for a "c" node. */
	cNodeToContextData(cNodeId: FxtrID): FxtrID;

	/** Get the pathroot data node for a "p" node. */
	pNodeToPathroot(pNodeId: FxtrID): FxtrID;

	/**
	 * Step back along a path provenance context node.
	 *
	 * Steps from a "$fxtr.ctx" p-node entry back to the contentful p-node
	 * that contains it. Returns null if not applicable.
	 */
	stepBackPCtx(pNodeId: FxtrID): FxtrID | null;

	/**
	 * Abandon an entity from the arena.
	 *
	 * The entity will no longer be tracked. The backing storage may GC it
	 * if no other arena references it.
	 */
	abandon(nodeId: FxtrID): void;

	/**
	 * Copy the given nodes to a new arena.
	 *
	 * The new arena guarantees the nodes remain accessible until closed,
	 * even if the original arena is closed.
	 */
	copyToNewArena(nodeIds: Iterable<FxtrID>): ReadableArena;

	/** Close the arena, releasing all tracked entities. */
	close(): void;
}

// ---------------------------------------------------------------------------
// CopyTargetArena
// ---------------------------------------------------------------------------

/** An arena that can copy entities from another arena. */
export interface CopyTargetArena extends ReadableArena {
	/**
	 * Copy an entity and all transitive dependencies from another arena.
	 *
	 * After this call, entityId will be tracked in this arena.
	 */
	copyFromArena(entityId: FxtrID, source: ReadableArena): PromiseOrEager<void>;
}

// ---------------------------------------------------------------------------
// ReadWriteArena
// ---------------------------------------------------------------------------

/**
 * An arena that can also store new entities.
 *
 * Store operations return PromiseOrEager — currently always Promise
 * (SHA-256 via Web Crypto API is async), but typed for future flexibility.
 */
export interface ReadWriteArena extends CopyTargetArena {
	/**
	 * Store an entity in the fxtr graph.
	 *
	 * All IDs referenced by the entity must already be tracked in the arena.
	 * Returns the canonical content-addressed ID.
	 */
	store(entity: FxtrCbor): PromiseOrEager<FxtrID>;

	/** Track a "." or "!" builtin node. */
	trackBuiltin(nodeId: FxtrID): FxtrID;

	/**
	 * Create a context provenance node ("c" node) from context data.
	 *
	 * The data must have a "value" field containing the node ID this
	 * context node unwraps to.
	 */
	createCNode(contextData: FxtrCbor): PromiseOrEager<FxtrID>;

	/**
	 * Create a path provenance node ("p" node) from pathroot data.
	 *
	 * The data must have a "value" field.
	 */
	createPNode(pathrootData: FxtrCbor): PromiseOrEager<FxtrID>;

	/**
	 * Create an untracked provenance node ("u" node) from a data node.
	 */
	createUNode(wrappedId: FxtrID): PromiseOrEager<FxtrID>;

	close(): void;
}

// ---------------------------------------------------------------------------
// DeepFetchable
// ---------------------------------------------------------------------------

/**
 * A store whose entities can be looked up by ID.
 *
 * Replaces Python's ObservableStore. Only tryLookup — no backrefs.
 * Used by FxtrScope for deep fetch.
 */
export interface DeepFetchable {
	/**
	 * Try to look up an entity in the store.
	 *
	 * Returns an arena that can load the entity, or null if not found.
	 */
	tryLookup(nodeId: FxtrID): PromiseOrEager<ReadableArena | null>;
}
