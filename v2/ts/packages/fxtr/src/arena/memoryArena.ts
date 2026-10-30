/**
 * In-memory implementation of fxtr arenas.
 *
 * Ported from Python's memory_arena.py. Provides:
 * - MemoryStore: Backing store with node states, refcounting, GC
 * - MemoryArena: Arena view with tracked liveset
 *
 * Node state types:
 * - StoredNode: A d/n node with actual CBOR bytes
 * - DerivedNode: A contentless node (., !, c, p, u) computed on demand
 * - InheritedNode: Placeholder for data inherited from another arena
 *
 * The store uses reference counting for garbage collection. Cycles are
 * impossible because entities are content-addressed and immutable.
 */

import { encode } from "cbor2";
import { sortLengthFirstDeterministic } from "cbor2/sorts";
import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	type RawFxtrRef,
	RawStrongFxtrID,
	RawWeakFxtrID,
	fxtrCborDumpsWithMarkerCheck,
	fxtrCborLoads,
} from "../core/entityCore.js";
import {
	visitAndProjectIds,
	visitAndProjectIdsInCanonicalCborOrder,
	visitIds,
} from "../core/visitor.js";
import { Eager, type PromiseOrEager } from "../util/eager.js";
import {
	type DeepFetchable,
	DepKind,
	type FxtrEntityFlatData,
	type ReadWriteArena,
	type ReadableArena,
} from "./types.js";

// ---------------------------------------------------------------------------
// SHA-256 hashing (Web Crypto API)
// ---------------------------------------------------------------------------

async function sha256(data: Uint8Array): Promise<Uint8Array> {
	const hash = await crypto.subtle.digest("SHA-256", data as Uint8Array<ArrayBuffer>);
	return new Uint8Array(hash);
}

// ---------------------------------------------------------------------------
// InMemoryBytesBackingStore
// ---------------------------------------------------------------------------

/**
 * Simple in-memory store of entity data with reference counting.
 * All operations are sync.
 */
export class InMemoryBytesBackingStore {
	private _data = new Map<string, Uint8Array>();
	private _counts = new Map<string, number>();

	saveAndIncrement(key: string, value: Uint8Array): void {
		this._data.set(key, value);
		this._counts.set(key, (this._counts.get(key) ?? 0) + 1);
	}

	get(key: string): Uint8Array {
		const data = this._data.get(key);
		if (data === undefined) {
			throw new Error(`Key not found in backing store: ${key}`);
		}
		return data;
	}

	free(key: string): void {
		const count = this._counts.get(key);
		if (count === undefined || count <= 0) {
			throw new Error(`Key not found or already freed: ${key}`);
		}
		if (count === 1) {
			this._data.delete(key);
			this._counts.delete(key);
		} else {
			this._counts.set(key, count - 1);
		}
	}
}

// ---------------------------------------------------------------------------
// Node state types (discriminated union)
// ---------------------------------------------------------------------------

/** Arena key prefix — uses `_arena:` to avoid collision with FxtrID key strings. */
function arenaKeyStr(ix: number): string {
	return `_arena:${ix}`;
}

interface StoredNode {
	readonly kind: "stored";
	readonly myId: FxtrID;
	readonly requires: ReadonlySet<string>;
	readonly strongLinkedRefs: ReadonlySet<string>;
	readonly unwrapsTo: FxtrID | null;
	readonly requiredBy: Set<string>;
	readonly wrappedBy: Set<string>;
}

interface DerivedNode {
	readonly kind: "derived";
	readonly myId: FxtrID;
	readonly requires: ReadonlySet<string>;
	readonly ctxNode: FxtrID | null;
	readonly backstepNode: FxtrID | null;
	readonly unwrapsTo: FxtrID | null;
	readonly requiredBy: Set<string>;
	readonly wrappedBy: Set<string>;
}

interface InheritedNode {
	readonly kind: "inherited";
	readonly myId: FxtrID;
	readonly inheritanceParentArena: ReadableArena;
	readonly unwrapsTo: FxtrID | null;
	readonly requiredBy: Set<string>;
	readonly wrappedBy: Set<string>;
}

type NodeState = StoredNode | DerivedNode | InheritedNode;

interface MemoryArenaState {
	readonly myKey: string;
	readonly liveset: Set<string>;
}

// ---------------------------------------------------------------------------
// MemoryStore
// ---------------------------------------------------------------------------

/**
 * Default shared backing store. All MemoryStores share this unless a
 * different one is provided, mirroring Python's GLOBAL_BACKING_STORE.
 * This means copying entities between scopes shares the underlying
 * bytes via refcounting rather than duplicating data.
 */
export const GLOBAL_BACKING_STORE = new InMemoryBytesBackingStore();

export class MemoryStore implements DeepFetchable {
	private _entityStates = new Map<string, NodeState>();
	private _arenaCounter = 0;
	private _arenas = new Map<string, MemoryArenaState>();
	private _deadPreCleanup = new Set<string>();
	private _freshNodes = new Set<string>();
	private _backingStore: InMemoryBytesBackingStore;

	constructor(backingStore: InMemoryBytesBackingStore = GLOBAL_BACKING_STORE) {
		this._backingStore = backingStore;
	}

	// -- Lifecycle --

	close(closeArenas = false): void {
		if (this._arenas.size > 0) {
			if (closeArenas) {
				for (const arenaKey of [...this._arenas.keys()]) {
					this._closeArena(arenaKey);
				}
			} else {
				throw new Error("Cannot clear state with open arenas unless closeArenas is true");
			}
		}

		const allNodes = [...this._entityStates.values()];
		this._entityStates.clear();
		this._deadPreCleanup.clear();
		this._freshNodes.clear();

		// Free backing store data and close inherited parent arenas.
		const inheritanceParents = new Set<ReadableArena>();
		for (const node of allNodes) {
			if (node.kind === "stored") {
				this._backingStore.free(node.myId.key());
			} else if (node.kind === "inherited") {
				inheritanceParents.add(node.inheritanceParentArena);
			}
		}
		for (const ipa of inheritanceParents) {
			ipa.close();
		}
	}

	/** Whether the store still has open arenas. */
	hasOpenArenas(): boolean {
		return this._arenas.size > 0;
	}

	// -- Arena management --

	newEmptyArena(): MemoryArena {
		const key = arenaKeyStr(this._arenaCounter);
		this._arenaCounter++;
		this._arenas.set(key, { myKey: key, liveset: new Set() });
		return new MemoryArena(this, key);
	}

	/** @internal */
	_trackInArena(nodeId: FxtrID, arenaKey: string): void {
		const arenaState = this._arenas.get(arenaKey);
		if (!arenaState) throw new Error(`Arena ${arenaKey} not found`);
		const nk = nodeId.key();
		if (!arenaState.liveset.has(nk)) {
			this._addRequirement(nodeId, arenaKey);
			arenaState.liveset.add(nk);
		}
	}

	/** @internal */
	_untrackFromArena(nodeId: FxtrID, arenaKey: string): void {
		const arenaState = this._arenas.get(arenaKey);
		if (!arenaState) throw new Error(`Arena ${arenaKey} not found`);
		const nk = nodeId.key();
		if (!arenaState.liveset.has(nk)) {
			throw new Error(`Node ${nodeId} is not in arena ${arenaKey}`);
		}
		arenaState.liveset.delete(nk);
		const node = this._entityStates.get(nk);
		if (!node) throw new Error(`Node ${nodeId} not in entity states`);
		node.requiredBy.delete(arenaKey);
		if (node.requiredBy.size === 0 && node.wrappedBy.size === 0) {
			this._deadPreCleanup.add(nk);
		}
	}

	/** @internal */
	_closeArena(arenaKey: string): void {
		const arena = this._arenas.get(arenaKey);
		if (!arena) throw new Error(`Arena ${arenaKey} not found`);
		this._arenas.delete(arenaKey);
		for (const nk of arena.liveset) {
			const node = this._entityStates.get(nk);
			if (!node) continue;
			node.requiredBy.delete(arenaKey);
			if (node.requiredBy.size === 0 && node.wrappedBy.size === 0) {
				this._deadPreCleanup.add(nk);
			}
		}
		arena.liveset.clear();
	}

	/** @internal */
	_arenaLiveset(arenaKey: string): Set<string> {
		const arenaState = this._arenas.get(arenaKey);
		if (!arenaState) throw new Error(`Arena ${arenaKey} not found`);
		return arenaState.liveset;
	}

	/** @internal — returns FxtrID objects for the liveset (bridges string keys back to IDs). */
	*_arenaLivesetIds(arenaKey: string): IterableIterator<FxtrID> {
		for (const nk of this._arenaLiveset(arenaKey)) {
			const node = this._entityStates.get(nk);
			if (node) yield node.myId;
		}
	}

	// -- Refcounting --

	private _addRequirement(nodeId: FxtrID, requiredBy: string): void {
		const nk = nodeId.key();
		const node = this._entityStates.get(nk);
		if (!node) throw new Error(`Node ${nodeId} not in entity states`);
		if (this._freshNodes.has(nk)) {
			this._freshNodes.delete(nk);
		} else if (node.requiredBy.size === 0) {
			this._deadPreCleanup.delete(nk);
		}
		node.requiredBy.add(requiredBy);
	}

	/** Collect up to one garbage node. Returns true if more garbage remains. */
	collectOneGarbage(): boolean {
		if (this._deadPreCleanup.size === 0) return false;

		const nk = this._deadPreCleanup.values().next().value as string;
		this._deadPreCleanup.delete(nk);
		const node = this._entityStates.get(nk);
		if (!node) return this._deadPreCleanup.size > 0;

		// If someone still depends on it, put it back.
		if (node.requiredBy.size > 0) {
			return this._deadPreCleanup.size > 0;
		}

		this._entityStates.delete(nk);

		if (node.kind === "inherited") {
			node.inheritanceParentArena.abandon(node.myId);
		} else {
			// Reduce dependency counts for requires.
			for (const reqKey of node.requires) {
				const reqNode = this._entityStates.get(reqKey);
				if (reqNode) {
					reqNode.requiredBy.delete(nk);
					if (reqNode.requiredBy.size === 0 && reqNode.wrappedBy.size === 0) {
						this._deadPreCleanup.add(reqKey);
					}
				}
			}
			// Free backing store data.
			if (node.kind === "stored") {
				this._backingStore.free(node.myId.key());
			}
		}

		// Reduce dependency count for unwrapped node.
		if (node.unwrapsTo) {
			const uwKey = node.unwrapsTo.key();
			const uwNode = this._entityStates.get(uwKey);
			if (uwNode) {
				uwNode.requiredBy.delete(nk);
				uwNode.wrappedBy.delete(nk);
				if (uwNode.requiredBy.size === 0 && uwNode.wrappedBy.size === 0) {
					this._deadPreCleanup.add(uwKey);
				}
			}
		}

		return this._deadPreCleanup.size > 0;
	}

	private _maybeMarkDead(nodeId: FxtrID): void {
		const nk = nodeId.key();
		const node = this._entityStates.get(nk);
		if (node && node.requiredBy.size === 0 && node.wrappedBy.size === 0) {
			this._deadPreCleanup.add(nk);
		}
	}

	// -- Node saving --

	private _alreadyHasDataFor(nodeId: FxtrID): boolean {
		const node = this._entityStates.get(nodeId.key());
		return node !== undefined && node.kind !== "inherited";
	}

	private _saveOrUpgradeNode(newNode: StoredNode | DerivedNode): void {
		const nk = newNode.myId.key();
		const existingNode = this._entityStates.get(nk);

		if (existingNode) {
			// Must be an inherited node we're replacing.
			if (existingNode.kind !== "inherited") {
				throw new Error(`Expected inherited node for ${newNode.myId}, got ${existingNode.kind}`);
			}

			// Track new requirements.
			for (const reqKey of newNode.requires) {
				const reqNode = this._entityStates.get(reqKey);
				if (!reqNode) throw new Error(`Required node ${reqKey} not found`);
				this._addRequirement(reqNode.myId, nk);
			}

			// Preserve mutable tracking state from replaced node.
			for (const rid of existingNode.requiredBy) {
				newNode.requiredBy.add(rid);
			}
			for (const wid of existingNode.wrappedBy) {
				newNode.wrappedBy.add(wid);
			}

			this._entityStates.set(nk, newNode);
			existingNode.inheritanceParentArena.abandon(existingNode.myId);
		} else {
			// New node.
			for (const reqKey of newNode.requires) {
				const reqNode = this._entityStates.get(reqKey);
				if (!reqNode) throw new Error(`Required node ${reqKey} not found`);
				this._addRequirement(reqNode.myId, nk);
			}

			if (newNode.unwrapsTo) {
				const uwKey = newNode.unwrapsTo.key();
				const uwNode = this._entityStates.get(uwKey);
				if (!uwNode) throw new Error(`Unwrapped node ${uwKey} not found`);
				uwNode.wrappedBy.add(nk);
			}

			this._entityStates.set(nk, newNode);
			this._freshNodes.add(nk);
		}
	}

	// -- Lazy attachment --

	/** @internal */
	_bareLazilyAttachFromArena(nodeId: FxtrID, source: ReadableArena): void {
		if (!source.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId} is not tracked in the source arena`);
		}
		if (this._entityStates.has(nodeId.key())) {
			return; // Already have it.
		}
		const ownedSource = source.copyToNewArena([nodeId]);
		this._adoptInheritanceArenaClaim(nodeId, ownedSource);
	}

	private _adoptInheritanceArenaClaim(nodeId: FxtrID, parentArena: ReadableArena): NodeState {
		if (!parentArena.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId} is not tracked in the parent arena`);
		}

		const nk = nodeId.key();
		const existing = this._entityStates.get(nk);

		if (!existing) {
			// New InheritedNode.
			let unwrapsTo: FxtrID | null = null;
			const uw = parentArena.unwrapProvenance(nodeId);
			if (uw) {
				unwrapsTo = uw;
				const uwNode = this._adoptInheritanceArenaClaim(uw, parentArena);
				uwNode.wrappedBy.add(nk);
			}

			const newNode: InheritedNode = {
				kind: "inherited",
				myId: nodeId,
				inheritanceParentArena: parentArena,
				unwrapsTo,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._entityStates.set(nk, newNode);
			this._freshNodes.add(nk);
			return newNode;
		}

		if (existing.kind === "inherited") {
			if (existing.inheritanceParentArena === parentArena) {
				return existing;
			}
			// Different parent — keep existing, abandon new claim.
			parentArena.abandon(nodeId);
			return existing;
		}

		// Already have a StoredNode or DerivedNode — release the claim.
		parentArena.abandon(nodeId);
		return existing;
	}

	private _adoptInheritanceArenaClaimOrNull(
		nodeId: FxtrID | null,
		parentArena: ReadableArena,
	): NodeState | null {
		if (nodeId === null) return null;
		return this._adoptInheritanceArenaClaim(nodeId, parentArena);
	}

	// -- Store operations --

	async bareStore(entity: FxtrCbor): Promise<FxtrID> {
		if (
			typeof entity !== "object" ||
			entity === null ||
			Array.isArray(entity) ||
			entity instanceof Uint8Array ||
			entity instanceof RawWeakFxtrID ||
			entity instanceof RawStrongFxtrID ||
			entity instanceof RawFxtrEntityMarkerID
		) {
			throw new Error("Entity must be a plain object");
		}

		const ctxField = (entity as Record<string, FxtrCbor>)["$fxtr.ctx"];
		if (!(ctxField instanceof RawFxtrEntityMarkerID)) {
			throw new Error('Entity must have a "$fxtr.ctx" RawFxtrEntityMarkerID');
		}

		const [ctxTag, ctxPayload] = ctxField.innerId.splitTagAndPayload();

		switch (ctxTag) {
			case ".":
				return this._bareStoreGroundNode(entity as Record<string, FxtrCbor>);
			case "c":
				return this._bareStoreNNode(entity as Record<string, FxtrCbor>);
			case "p":
				return this._bareStorePNode(entity as Record<string, FxtrCbor>, ctxField, ctxPayload);
			case "u":
				return this._bareStoreUNode(entity as Record<string, FxtrCbor>);
			default:
				throw new Error(
					`Invalid entity: "$fxtr.ctx" has tag ${ctxTag}! Expected ".", "c", "p", or "u".`,
				);
		}
	}

	private async _bareStoreGroundNode(entity: Record<string, FxtrCbor>): Promise<FxtrID> {
		const entityBytes = fxtrCborDumpsWithMarkerCheck(entity);
		const hash = await sha256(entityBytes);
		const newId = FxtrID.fromTagAndPayload("d", hash);

		if (this._alreadyHasDataFor(newId)) return newId;

		const requires = new Set<string>();
		const strongLinks = new Set<string>();

		visitIds(entity, (ref: RawFxtrRef) => {
			const childKey = ref.innerId.key();
			if (!this._entityStates.has(childKey)) {
				throw new Error(`Child ${ref.innerId} not in entity states`);
			}
			requires.add(childKey);
			if (ref instanceof RawStrongFxtrID) {
				strongLinks.add(childKey);
			}
		});

		this._backingStore.saveAndIncrement(newId.key(), entityBytes);
		const newNode: StoredNode = {
			kind: "stored",
			myId: newId,
			requires,
			strongLinkedRefs: strongLinks,
			unwrapsTo: null,
			requiredBy: new Set(),
			wrappedBy: new Set(),
		};
		this._saveOrUpgradeNode(newNode);
		return newId;
	}

	private async _bareStoreNNode(entity: Record<string, FxtrCbor>): Promise<FxtrID> {
		const entityBytes = fxtrCborDumpsWithMarkerCheck(entity);
		const hash = await sha256(entityBytes);
		const newId = FxtrID.fromTagAndPayload("n", hash);

		if (this._alreadyHasDataFor(newId)) return newId;

		const requires = new Set<string>();
		const strongLinks = new Set<string>();

		const unwrapData = visitAndProjectIds(entity, (ref: RawFxtrRef) => {
			const childId = ref.innerId;
			const childKey = childId.key();
			const childState = this._entityStates.get(childKey);
			if (!childState) {
				throw new Error(`Child ${childId} not in entity states`);
			}
			if (childState.unwrapsTo === null) {
				throw new Error(
					`Illegal entity: n-type provenance node has non-provenance child: ${childId}`,
				);
			}
			requires.add(childKey);
			if (ref instanceof RawStrongFxtrID) {
				strongLinks.add(childKey);
			}
			return childState.unwrapsTo;
		});

		// Recursively store the unwrapped node.
		const unwrapId = await this.bareStore(unwrapData);

		this._backingStore.saveAndIncrement(newId.key(), entityBytes);
		const newNode: StoredNode = {
			kind: "stored",
			myId: newId,
			requires,
			strongLinkedRefs: strongLinks,
			unwrapsTo: unwrapId,
			requiredBy: new Set(),
			wrappedBy: new Set(),
		};
		this._saveOrUpgradeNode(newNode);
		return newId;
	}

	private async _bareStorePNode(
		entity: Record<string, FxtrCbor>,
		ctxField: RawFxtrEntityMarkerID,
		ctxPayload: Uint8Array,
	): Promise<FxtrID> {
		const cborEncodeOpts = { sortKeys: sortLengthFirstDeterministic };
		const payloadData = decode_cbor_payload(ctxPayload);

		if (!Array.isArray(payloadData) || payloadData.length < 2) {
			throw new Error("Any valid p node has a nonempty path to its ctx field");
		}

		const externalPayload = payloadData.slice(0, -1);
		const newId = FxtrID.fromTagAndPayload("p", encode(externalPayload, cborEncodeOpts));

		const ctxNodeState = this._entityStates.get(ctxField.innerId.key());
		if (!ctxNodeState) {
			throw new Error(`Context node ${ctxField.innerId} not in entity states`);
		}

		let pChildCounter = 0;
		const unwrapData = visitAndProjectIdsInCanonicalCborOrder(entity, (ref: RawFxtrRef) => {
			const expectedChildId = FxtrID.fromTagAndPayload(
				"p",
				encode([...externalPayload, pChildCounter], cborEncodeOpts),
			);
			pChildCounter++;

			const childId = ref.innerId;
			if (!childId.equals(expectedChildId)) {
				throw new Error(
					`Illegal p-tag entity: child ID ${childId} does not match expected ${expectedChildId}`,
				);
			}
			const childState = this._entityStates.get(childId.key());
			if (!childState) {
				throw new Error(`Child ${childId} not in entity states`);
			}
			if (childState.unwrapsTo === null) {
				throw new Error(`Child ${childId} has no unwrapsTo`);
			}
			return childState.unwrapsTo;
		});

		// Resolve the node itself.
		let retrievedNode = this._entityStates.get(newId.key());
		if (!retrievedNode) {
			if (ctxNodeState.kind === "inherited") {
				const backstepId = ctxNodeState.inheritanceParentArena.stepBackPCtx(ctxNodeState.myId);
				if (backstepId === null) {
					throw new Error("Cannot resolve p-node: inherited ctx has no backstep");
				}
				retrievedNode = this._adoptInheritanceArenaClaim(
					backstepId,
					ctxNodeState.inheritanceParentArena,
				);
				if (!retrievedNode.myId.equals(newId)) {
					throw new Error(`Backstep node ${retrievedNode.myId} does not match expected ${newId}`);
				}
			} else {
				throw new Error(
					"Entity being stored is a path-provenance node but is not alive in the store and its ctx is not inherited",
				);
			}
		}

		const actualUnwrapId = await this.bareStore(unwrapData);

		if (retrievedNode.unwrapsTo === null || !actualUnwrapId.equals(retrievedNode.unwrapsTo)) {
			this._maybeMarkDead(actualUnwrapId);
			throw new Error(
				`Illegal p-tag entity: wrapped data inconsistent (expected ${retrievedNode.unwrapsTo} but got ${actualUnwrapId})`,
			);
		}

		if (retrievedNode.kind === "inherited") {
			const contextNode = this._adoptInheritanceArenaClaim(
				retrievedNode.inheritanceParentArena.pNodeToPathroot(newId),
				retrievedNode.inheritanceParentArena,
			);
			const newNode: DerivedNode = {
				kind: "derived",
				myId: newId,
				ctxNode: contextNode.myId,
				backstepNode: null,
				requires: new Set([contextNode.myId.key()]),
				unwrapsTo: retrievedNode.unwrapsTo,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		} else {
			// Already a DerivedNode — nothing to do.
			return newId;
		}

		return newId;
	}

	private async _bareStoreUNode(entity: Record<string, FxtrCbor>): Promise<FxtrID> {
		const unwrapData = visitAndProjectIds(entity, (ref: RawFxtrRef) => {
			const childId = ref.innerId;
			const childKey = childId.key();
			const childState = this._entityStates.get(childKey);
			if (!childState) {
				throw new Error(`Child ${childId} not in entity states`);
			}
			const [childTag] = childId.splitTagAndPayload();
			if (childTag !== "u") {
				throw new Error(`Illegal entity: u-type node has non-u-type child: ${childId}`);
			}
			if (childState.unwrapsTo === null) {
				throw new Error(`Child ${childId} has no unwrapsTo`);
			}
			return childState.unwrapsTo;
		});

		const unwrapId = await this.bareStore(unwrapData);
		const newId = FxtrID.fromTagAndPayload("u", unwrapId.rawDataId);

		if (!this._alreadyHasDataFor(newId)) {
			const newNode: DerivedNode = {
				kind: "derived",
				myId: newId,
				ctxNode: null,
				backstepNode: null,
				requires: new Set([unwrapId.key()]),
				unwrapsTo: unwrapId,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		}

		return newId;
	}

	bareStoreFlat(flatData: FxtrEntityFlatData): FxtrID {
		const { entityId: nodeId, entityBytes, deps } = flatData;

		// Verify all deps exist.
		for (const [depId] of deps) {
			if (!this._entityStates.has(depId.key())) {
				throw new Error(`Dependency ${depId} not in entity states`);
			}
		}

		if (this._alreadyHasDataFor(nodeId)) return nodeId;

		const tag = nodeId.getTag();

		if (tag === "d" || tag === "n") {
			const requires = new Set<string>();
			const strongLinkedRefs = new Set<string>();
			let unwrapsTo: FxtrID | null = null;

			for (const [depId, depKind] of deps) {
				switch (depKind) {
					case DepKind.STRONG_REF:
						requires.add(depId.key());
						strongLinkedRefs.add(depId.key());
						break;
					case DepKind.WEAK_REF:
						requires.add(depId.key());
						break;
					case DepKind.UNWRAPS_TO:
						if (unwrapsTo !== null) throw new Error("Multiple UNWRAPS_TO deps");
						unwrapsTo = depId;
						break;
					default:
						throw new Error(`Unexpected dep kind for d/n node: ${depKind}`);
				}
			}

			if (!entityBytes) throw new Error("d/n nodes must have entityBytes");
			this._backingStore.saveAndIncrement(nodeId.key(), entityBytes);
			const newNode: StoredNode = {
				kind: "stored",
				myId: nodeId,
				requires,
				strongLinkedRefs,
				unwrapsTo,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		} else if (tag === "c" || tag === "p") {
			let ctxNodeId: FxtrID | null = null;
			let backstepNodeId: FxtrID | null = null;
			let unwrapsToId: FxtrID | null = null;

			for (const [depId, depKind] of deps) {
				switch (depKind) {
					case DepKind.CTX_NODE:
						if (ctxNodeId !== null) throw new Error("Multiple CTX_NODE deps");
						ctxNodeId = depId;
						break;
					case DepKind.BACKSTEP:
						if (backstepNodeId !== null) throw new Error("Multiple BACKSTEP deps");
						backstepNodeId = depId;
						break;
					case DepKind.UNWRAPS_TO:
						if (unwrapsToId !== null) throw new Error("Multiple UNWRAPS_TO deps");
						unwrapsToId = depId;
						break;
					default:
						throw new Error(`Unexpected dep kind for ${tag} node: ${depKind}`);
				}
			}

			if (!ctxNodeId) throw new Error(`${tag} nodes must have CTX_NODE dep`);
			if (!unwrapsToId) throw new Error(`${tag} nodes must have UNWRAPS_TO dep`);
			if (entityBytes) throw new Error(`${tag} nodes must not have entityBytes`);

			const requiresSet = new Set<string>([ctxNodeId.key()]);
			if (backstepNodeId) requiresSet.add(backstepNodeId.key());

			const newNode: DerivedNode = {
				kind: "derived",
				myId: nodeId,
				ctxNode: ctxNodeId,
				backstepNode: backstepNodeId,
				requires: requiresSet,
				unwrapsTo: unwrapsToId,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		} else if (tag === "u") {
			let unwrapsToId: FxtrID | null = null;

			for (const [depId, depKind] of deps) {
				if (depKind === DepKind.UNWRAPS_TO) {
					if (unwrapsToId !== null) throw new Error("Multiple UNWRAPS_TO deps");
					unwrapsToId = depId;
				} else {
					throw new Error(`Unexpected dep kind for u node: ${depKind}`);
				}
			}

			if (!unwrapsToId) throw new Error("u nodes must have UNWRAPS_TO dep");
			if (entityBytes) throw new Error("u nodes must not have entityBytes");

			const newNode: DerivedNode = {
				kind: "derived",
				myId: nodeId,
				ctxNode: null,
				backstepNode: null,
				requires: new Set(),
				unwrapsTo: unwrapsToId,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		} else if (tag === "." || tag === "!") {
			if (deps.length !== 0) throw new Error(`${tag} nodes must have no deps`);
			if (entityBytes) throw new Error(`${tag} nodes must not have entityBytes`);

			const newNode: DerivedNode = {
				kind: "derived",
				myId: nodeId,
				ctxNode: null,
				backstepNode: null,
				requires: new Set(),
				unwrapsTo: null,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		} else {
			throw new Error(`Unsupported node tag: ${tag}`);
		}

		return nodeId;
	}

	// -- Load operations --

	private _ensureOwnDerived(nodeId: FxtrID): DerivedNode {
		const nk = nodeId.key();
		const node = this._entityStates.get(nk);
		if (!node) throw new Error(`Node ${nodeId} not in entity states`);

		if (node.kind === "derived") return node;

		if (node.kind === "inherited") {
			const parentArena = node.inheritanceParentArena;
			const tag = nodeId.getTag();
			let newNode: DerivedNode;

			switch (tag) {
				case ".":
				case "!": {
					newNode = {
						kind: "derived",
						myId: nodeId,
						ctxNode: null,
						backstepNode: null,
						requires: new Set(),
						unwrapsTo: null,
						requiredBy: new Set(),
						wrappedBy: new Set(),
					};
					break;
				}
				case "c": {
					const ctxData = this._adoptInheritanceArenaClaim(
						parentArena.cNodeToContextData(nodeId),
						parentArena,
					);
					newNode = {
						kind: "derived",
						myId: nodeId,
						ctxNode: ctxData.myId,
						backstepNode: null,
						requires: new Set([ctxData.myId.key()]),
						unwrapsTo: node.unwrapsTo,
						requiredBy: new Set(),
						wrappedBy: new Set(),
					};
					break;
				}
				case "p": {
					const pathroot = this._adoptInheritanceArenaClaim(
						parentArena.pNodeToPathroot(nodeId),
						parentArena,
					);
					const backstep = this._adoptInheritanceArenaClaimOrNull(
						parentArena.stepBackPCtx(nodeId),
						parentArena,
					);
					const requires = new Set([pathroot.myId.key()]);
					if (backstep) requires.add(backstep.myId.key());
					newNode = {
						kind: "derived",
						myId: nodeId,
						ctxNode: pathroot.myId,
						backstepNode: backstep?.myId ?? null,
						requires,
						unwrapsTo: node.unwrapsTo,
						requiredBy: new Set(),
						wrappedBy: new Set(),
					};
					break;
				}
				case "u": {
					newNode = {
						kind: "derived",
						myId: nodeId,
						ctxNode: null,
						backstepNode: null,
						requires: new Set(),
						unwrapsTo: node.unwrapsTo,
						requiredBy: new Set(),
						wrappedBy: new Set(),
					};
					break;
				}
				default:
					throw new Error(`Unsupported derived node tag: ${tag}`);
			}

			this._saveOrUpgradeNode(newNode);
			return newNode;
		}

		throw new Error(`Bad derived node type: ${nodeId} -> ${node.kind}`);
	}

	private _ensureOwn(nodeId: FxtrID): PromiseOrEager<StoredNode | DerivedNode> {
		const nk = nodeId.key();
		const node = this._entityStates.get(nk);
		if (!node) throw new Error(`Node ${nodeId} not in entity states`);

		if (node.kind !== "inherited") {
			return Eager.resolve(node as StoredNode | DerivedNode);
		}

		const parentArena = node.inheritanceParentArena;
		const tag = nodeId.getTag();

		if (tag === "d" || tag === "n") {
			return parentArena.loadFlat(nodeId).then((flatData) => {
				for (const [depId] of flatData.deps) {
					this._adoptInheritanceArenaClaim(depId, parentArena);
				}
				this.bareStoreFlat(flatData);
				const ownNode = this._entityStates.get(nk);
				if (!ownNode || ownNode.kind !== "stored") {
					throw new Error(`Expected stored node after loading flat data for ${nodeId}`);
				}
				return ownNode;
			});
		}

		return Eager.resolve(this._ensureOwnDerived(nodeId));
	}

	bareLoad(nodeId: FxtrID): PromiseOrEager<FxtrCbor> {
		const nk = nodeId.key();
		if (!this._entityStates.has(nk)) {
			throw new Error(`Node ${nodeId} not in entity states`);
		}

		return this._ensureOwn(nodeId).then((node) => this._bareLoadAfterEnsureOwn(nodeId, node));
	}

	/** @internal Post-_ensureOwn logic for bareLoad, factored out for .then() chaining. */
	private _bareLoadAfterEnsureOwn(
		nodeId: FxtrID,
		node: StoredNode | DerivedNode,
	): PromiseOrEager<FxtrCbor> {
		if (node.kind === "stored") {
			const cborBytes = this._backingStore.get(nodeId.key());
			return Eager.resolve(fxtrCborLoads(cborBytes));
		}

		// DerivedNode
		const tag = nodeId.getTag();

		switch (tag) {
			case ".":
			case "!":
			case "c":
				throw new Error(`Cannot load a contentless ID: ${nodeId}`);

			case "p": {
				const [, nodePayload] = nodeId.splitTagAndPayload();
				if (node.unwrapsTo === null || node.ctxNode === null) {
					throw new Error(`p-node ${nodeId} missing unwrapsTo or ctxNode`);
				}
				const cborEncodeOpts = {
					sortKeys: sortLengthFirstDeterministic,
				};
				const payloadData = decode_cbor_payload(nodePayload);
				if (!Array.isArray(payloadData)) {
					throw new Error("Invalid p-node payload");
				}

				const ctxNode = node.ctxNode;
				return this.bareLoad(node.unwrapsTo).then((unwrapContents) => {
					let pChildCounter = 0;
					const projected = visitAndProjectIdsInCanonicalCborOrder(
						unwrapContents,
						(ref: RawFxtrRef) => {
							const unwrappedChildId = ref.innerId;
							const newPChildId = FxtrID.fromTagAndPayload(
								"p",
								encode([...payloadData, pChildCounter], cborEncodeOpts),
							);
							pChildCounter++;

							const existingChild = this._entityStates.get(newPChildId.key());
							if (existingChild) {
								if (
									existingChild.unwrapsTo === null ||
									!existingChild.unwrapsTo.equals(unwrappedChildId)
								) {
									throw new Error(`p-node child ${newPChildId} unwrapsTo mismatch`);
								}
							} else if (ref instanceof RawFxtrEntityMarkerID) {
								const newPChild: DerivedNode = {
									kind: "derived",
									myId: newPChildId,
									ctxNode,
									backstepNode: nodeId,
									requires: new Set([ctxNode.key(), nodeId.key()]),
									unwrapsTo: unwrappedChildId,
									requiredBy: new Set(),
									wrappedBy: new Set(),
								};
								this._saveOrUpgradeNode(newPChild);
							} else {
								const newPChild: DerivedNode = {
									kind: "derived",
									myId: newPChildId,
									ctxNode,
									backstepNode: null,
									requires: new Set([ctxNode.key()]),
									unwrapsTo: unwrappedChildId,
									requiredBy: new Set(),
									wrappedBy: new Set(),
								};
								this._saveOrUpgradeNode(newPChild);
							}
							return newPChildId;
						},
					);
					return projected;
				});
			}

			case "u": {
				if (node.unwrapsTo === null) {
					throw new Error(`u-node ${nodeId} missing unwrapsTo`);
				}
				return this.bareLoad(node.unwrapsTo).then((unwrapContents) => {
					return visitAndProjectIds(unwrapContents, (ref: RawFxtrRef) => {
						return this._createUNodeSync(ref.innerId);
					});
				});
			}

			default:
				throw new Error(`Invalid derived node type ${tag} for ${nodeId}`);
		}
	}

	bareLoadFlat(nodeId: FxtrID): PromiseOrEager<FxtrEntityFlatData> {
		const nk = nodeId.key();
		if (!this._entityStates.has(nk)) {
			throw new Error(`Node ${nodeId} not in entity states`);
		}

		return this._ensureOwn(nodeId).then((node) => {
			const deps: Array<[FxtrID, DepKind]> = [];

			if (node.kind === "stored") {
				const entityBytes = this._backingStore.get(nodeId.key());

				for (const reqKey of node.requires) {
					const reqNode = this._entityStates.get(reqKey);
					if (!reqNode) continue;
					if (node.strongLinkedRefs.has(reqKey)) {
						deps.push([reqNode.myId, DepKind.STRONG_REF]);
					} else {
						deps.push([reqNode.myId, DepKind.WEAK_REF]);
					}
				}
				if (node.unwrapsTo) {
					deps.push([node.unwrapsTo, DepKind.UNWRAPS_TO]);
				}

				return { entityId: nodeId, entityBytes: new Uint8Array(entityBytes), deps };
			}

			// DerivedNode
			if (node.ctxNode) deps.push([node.ctxNode, DepKind.CTX_NODE]);
			if (node.backstepNode) deps.push([node.backstepNode, DepKind.BACKSTEP]);
			if (node.unwrapsTo) deps.push([node.unwrapsTo, DepKind.UNWRAPS_TO]);

			return { entityId: nodeId, entityBytes: null, deps };
		});
	}

	// -- Provenance operations (sync) --

	unwrapProvenance(nodeId: FxtrID): FxtrID | null {
		const node = this._entityStates.get(nodeId.key());
		if (!node) throw new Error(`Node ${nodeId} not in entity states`);
		return node.unwrapsTo;
	}

	trackBuiltin(nodeId: FxtrID): FxtrID {
		const tag = nodeId.getTag();
		if (tag !== "." && tag !== "!") {
			throw new Error(`trackBuiltin only accepts . or ! nodes, got ${tag}`);
		}
		if (!this._alreadyHasDataFor(nodeId)) {
			const newNode: DerivedNode = {
				kind: "derived",
				myId: nodeId,
				ctxNode: null,
				backstepNode: null,
				requires: new Set(),
				unwrapsTo: null,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		}
		return nodeId;
	}

	async createCNode(contextData: FxtrCbor): Promise<FxtrID> {
		if (
			typeof contextData !== "object" ||
			contextData === null ||
			Array.isArray(contextData) ||
			contextData instanceof Uint8Array ||
			contextData instanceof RawWeakFxtrID ||
			contextData instanceof RawStrongFxtrID ||
			contextData instanceof RawFxtrEntityMarkerID
		) {
			throw new Error("Context data must be a plain object");
		}
		const record = contextData as Record<string, FxtrCbor>;
		const value = record.value;
		if (!(value instanceof RawStrongFxtrID) && !(value instanceof RawWeakFxtrID)) {
			throw new Error('Context data must have a "value" field with a node ID');
		}

		const dataId = await this.bareStore(contextData);
		const [dataTag, dataPayload] = dataId.splitTagAndPayload();
		if (dataTag !== "d") {
			throw new Error("Context data must be for a d-node");
		}

		const newId = FxtrID.fromTagAndPayload("c", dataPayload);

		if (!this._alreadyHasDataFor(newId)) {
			const newNode: DerivedNode = {
				kind: "derived",
				myId: newId,
				ctxNode: dataId,
				backstepNode: null,
				requires: new Set([dataId.key()]),
				unwrapsTo: value.innerId,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		}

		return newId;
	}

	async createPNode(pathrootData: FxtrCbor): Promise<FxtrID> {
		if (
			typeof pathrootData !== "object" ||
			pathrootData === null ||
			Array.isArray(pathrootData) ||
			pathrootData instanceof Uint8Array ||
			pathrootData instanceof RawWeakFxtrID ||
			pathrootData instanceof RawStrongFxtrID ||
			pathrootData instanceof RawFxtrEntityMarkerID
		) {
			throw new Error("Pathroot data must be a plain object");
		}
		const record = pathrootData as Record<string, FxtrCbor>;
		const value = record.value;
		if (!(value instanceof RawStrongFxtrID) && !(value instanceof RawWeakFxtrID)) {
			throw new Error('Pathroot data must have a "value" field with a node ID');
		}

		const dataId = await this.bareStore(pathrootData);
		const [dataTag] = dataId.splitTagAndPayload();
		if (dataTag !== "d") {
			throw new Error("Pathroot data must be for a d-node");
		}

		const cborEncodeOpts = { sortKeys: sortLengthFirstDeterministic };
		const newPayload = encode([dataId.rawDataId], cborEncodeOpts);
		const newId = FxtrID.fromTagAndPayload("p", newPayload);

		if (!this._alreadyHasDataFor(newId)) {
			const newNode: DerivedNode = {
				kind: "derived",
				myId: newId,
				ctxNode: dataId,
				backstepNode: null,
				requires: new Set([dataId.key()]),
				unwrapsTo: value.innerId,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		}

		return newId;
	}

	/** Create a u-node. Sync since no hashing needed. */
	private _createUNodeSync(wrappedId: FxtrID): FxtrID {
		if (!this._entityStates.has(wrappedId.key())) {
			throw new Error(`Wrapped node ${wrappedId} not in entity states`);
		}
		const newId = FxtrID.fromTagAndPayload("u", wrappedId.rawDataId);
		if (!this._alreadyHasDataFor(newId)) {
			const newNode: DerivedNode = {
				kind: "derived",
				myId: newId,
				ctxNode: null,
				backstepNode: null,
				requires: new Set(),
				unwrapsTo: wrappedId,
				requiredBy: new Set(),
				wrappedBy: new Set(),
			};
			this._saveOrUpgradeNode(newNode);
		}
		return newId;
	}

	/** Public async wrapper for createUNode. */
	async createUNode(wrappedId: FxtrID): Promise<FxtrID> {
		return this._createUNodeSync(wrappedId);
	}

	cNodeToContextData(cNodeId: FxtrID): FxtrID {
		if (cNodeId.getTag() !== "c") {
			throw new Error(`Expected c-node, got ${cNodeId.getTag()}`);
		}
		const node = this._ensureOwnDerived(cNodeId);
		if (!node.ctxNode) {
			throw new Error(`c-node ${cNodeId} has no ctxNode`);
		}
		return node.ctxNode;
	}

	pNodeToPathroot(pNodeId: FxtrID): FxtrID {
		if (pNodeId.getTag() !== "p") {
			throw new Error(`Expected p-node, got ${pNodeId.getTag()}`);
		}
		const node = this._ensureOwnDerived(pNodeId);
		if (!node.ctxNode) {
			throw new Error(`p-node ${pNodeId} has no ctxNode`);
		}
		return node.ctxNode;
	}

	stepBackPCtx(pNodeId: FxtrID): FxtrID | null {
		if (pNodeId.getTag() !== "p") {
			throw new Error(`Expected p-node, got ${pNodeId.getTag()}`);
		}
		const node = this._ensureOwnDerived(pNodeId);
		return node.backstepNode;
	}

	// -- DeepFetchable --

	tryLookup(nodeId: FxtrID): PromiseOrEager<ReadableArena | null> {
		if (!this._entityStates.has(nodeId.key())) return Eager.resolve(null);
		const arena = this.newEmptyArena();
		this._trackInArena(nodeId, arena._arenaKey);
		return Eager.resolve(arena);
	}

	// -- Copy from external arena --

	/** @internal */
	async _buildTmpArenaCopyingFromExternalArena(
		entityId: FxtrID,
		source: ReadableArena,
	): Promise<MemoryArena> {
		const tmpSource = source.copyToNewArena([entityId]);
		const tmpDest = this.newEmptyArena();

		try {
			type WorkItem =
				| { kind: "discover"; entityId: FxtrID }
				| { kind: "save"; flatData: FxtrEntityFlatData };

			const worklist: WorkItem[] = [{ kind: "discover", entityId }];

			while (worklist.length > 0) {
				// biome-ignore lint/style/noNonNullAssertion: checked length > 0
				const item = worklist.pop()!;

				if (item.kind === "discover") {
					if (this._entityStates.has(item.entityId.key())) {
						this._trackInArena(item.entityId, tmpDest._arenaKey);
					} else {
						const flatData = await tmpSource.loadFlat(item.entityId);
						worklist.push({ kind: "save", flatData });
						for (const [depId] of flatData.deps) {
							worklist.push({
								kind: "discover",
								entityId: depId,
							});
						}
					}
				} else {
					this.bareStoreFlat(item.flatData);
					this._trackInArena(item.flatData.entityId, tmpDest._arenaKey);
				}
			}

			return tmpDest.copyToNewArena([entityId]);
		} finally {
			tmpSource.close();
			tmpDest.close();
		}
	}
}

// ---------------------------------------------------------------------------
// Decode helper (for p-node payloads)
// ---------------------------------------------------------------------------

import { decode } from "cbor2";

function decode_cbor_payload(payload: Uint8Array): unknown {
	return decode(payload);
}

// ---------------------------------------------------------------------------
// MemoryArena
// ---------------------------------------------------------------------------

export class MemoryArena implements ReadWriteArena {
	/** @internal */
	readonly _store: MemoryStore;
	/** @internal */
	readonly _arenaKey: string;
	private _closed = false;

	/** @internal */
	constructor(store: MemoryStore, arenaKey: string) {
		this._store = store;
		this._arenaKey = arenaKey;
	}

	// -- ReadableArena --

	isTracked(nodeId: FxtrID): boolean {
		if (this._closed) return false;
		return this._store._arenaLiveset(this._arenaKey).has(nodeId.key());
	}

	load(nodeId: FxtrID): PromiseOrEager<FxtrCbor> {
		this._ensureOpen();
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId} is not tracked in this arena`);
		}
		return this._store.bareLoad(nodeId).then((loaded) => {
			visitIds(loaded, (ref: RawFxtrRef) => {
				this._store._trackInArena(ref.innerId, this._arenaKey);
			});
			return loaded;
		});
	}

	loadFlat(nodeId: FxtrID): PromiseOrEager<FxtrEntityFlatData> {
		this._ensureOpen();
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId} is not tracked in this arena`);
		}
		return this._store.bareLoadFlat(nodeId).then((flatData) => {
			for (const [depId] of flatData.deps) {
				this._store._trackInArena(depId, this._arenaKey);
			}
			return flatData;
		});
	}

	unwrapProvenance(nodeId: FxtrID): FxtrID | null {
		const result = this._store.unwrapProvenance(nodeId);
		if (result) this._store._trackInArena(result, this._arenaKey);
		return result;
	}

	cNodeToContextData(cNodeId: FxtrID): FxtrID {
		const result = this._store.cNodeToContextData(cNodeId);
		this._store._trackInArena(result, this._arenaKey);
		return result;
	}

	pNodeToPathroot(pNodeId: FxtrID): FxtrID {
		const result = this._store.pNodeToPathroot(pNodeId);
		this._store._trackInArena(result, this._arenaKey);
		return result;
	}

	stepBackPCtx(pNodeId: FxtrID): FxtrID | null {
		const result = this._store.stepBackPCtx(pNodeId);
		if (result) this._store._trackInArena(result, this._arenaKey);
		return result;
	}

	abandon(nodeId: FxtrID): void {
		this._ensureOpen();
		if (!this.isTracked(nodeId)) {
			throw new Error(`Node ${nodeId} is not tracked in this arena`);
		}
		this._store._untrackFromArena(nodeId, this._arenaKey);
	}

	copyToNewArena(nodeIds: Iterable<FxtrID>): MemoryArena {
		this._ensureOpen();
		const newArena = this._store.newEmptyArena();
		for (const nodeId of nodeIds) {
			if (!this.isTracked(nodeId)) {
				throw new Error(`Node ${nodeId} is not tracked in this arena`);
			}
			this._store._trackInArena(nodeId, newArena._arenaKey);
		}
		return newArena;
	}

	close(): void {
		if (!this._closed) {
			this._store._closeArena(this._arenaKey);
			this._closed = true;
		}
	}

	// -- CopyTargetArena --

	copyFromArena(entityId: FxtrID, source: ReadableArena): PromiseOrEager<void> {
		this._ensureOpen();
		if (!source.isTracked(entityId)) {
			throw new Error(`Node ${entityId} is not tracked in the source arena`);
		}

		// Same store optimization — fully sync.
		if (source instanceof MemoryArena && source._store === this._store) {
			this._trackFromStore(entityId);
			return Eager.resolve(undefined);
		}

		return this._store._buildTmpArenaCopyingFromExternalArena(entityId, source).then((tmpArena) => {
			this._trackFromStore(entityId);
			tmpArena.close();
		});
	}

	// -- ReadWriteArena --

	async store(entity: FxtrCbor): Promise<FxtrID> {
		this._ensureOpen();
		this._ensureAllAccessibleForStoring(entity);
		const newId = await this._store.bareStore(entity);
		return this._trackFromStore(newId);
	}

	trackBuiltin(nodeId: FxtrID): FxtrID {
		this._ensureOpen();
		return this._trackFromStore(this._store.trackBuiltin(nodeId));
	}

	async createCNode(contextData: FxtrCbor): Promise<FxtrID> {
		this._ensureOpen();
		this._ensureAllAccessibleForStoring(contextData);
		return this._trackFromStore(await this._store.createCNode(contextData));
	}

	async createPNode(pathrootData: FxtrCbor): Promise<FxtrID> {
		this._ensureOpen();
		this._ensureAllAccessibleForStoring(pathrootData);
		return this._trackFromStore(await this._store.createPNode(pathrootData));
	}

	async createUNode(wrappedId: FxtrID): Promise<FxtrID> {
		this._ensureOpen();
		return this._trackFromStore(await this._store.createUNode(wrappedId));
	}

	// -- MemoryArena-specific --

	unsafeLazilyAttachFromArena(entityId: FxtrID, source: ReadableArena): void {
		this._ensureOpen();
		this._store._bareLazilyAttachFromArena(entityId, source);
		this._store._trackInArena(entityId, this._arenaKey);
	}

	copyFromSiblingMemoryArena(entityId: FxtrID, source: MemoryArena): void {
		this._ensureOpen();
		if (source._store !== this._store) {
			throw new Error(
				"copyFromSiblingMemoryArena only supports sibling arenas with the same store",
			);
		}
		if (!source.isTracked(entityId)) {
			throw new Error(`Node ${entityId} is not tracked in the source arena`);
		}
		this._trackFromStore(entityId);
	}

	clone(): MemoryArena {
		this._ensureOpen();
		const newArena = this._store.newEmptyArena();
		for (const nodeId of this._store._arenaLivesetIds(this._arenaKey)) {
			this._store._trackInArena(nodeId, newArena._arenaKey);
		}
		return newArena;
	}

	// -- Internal helpers --

	/** @internal */
	_trackFromStore(nodeId: FxtrID): FxtrID {
		this._store._trackInArena(nodeId, this._arenaKey);
		return nodeId;
	}

	private _ensureOpen(): void {
		if (this._closed) {
			throw new Error("Arena is closed");
		}
	}

	private _ensureAllAccessibleForStoring(entity: FxtrCbor): void {
		visitIds(entity, (ref: RawFxtrRef) => {
			if (!this.isTracked(ref.innerId)) {
				throw new Error(
					`Entity references untracked node ${ref.innerId}. All children must be tracked before storing.`,
				);
			}
		});
	}
}
