/**
 * Tests for arena/memoryArena.ts.
 * Ported from Python's test_memory_arena.py.
 */

import { describe, expect, it } from "vitest";
import { MemoryArena, MemoryStore } from "../../src/arena/memoryArena.js";
import { DepKind } from "../../src/arena/types.js";
import {
	type FxtrCbor,
	FxtrID,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	RawWeakFxtrID,
} from "../../src/core/entityCore.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function groundCtx(): FxtrID {
	return FxtrID.here();
}

function makeSimpleEntity(
	ctx: FxtrID,
	data: Record<string, FxtrCbor> = {},
): Record<string, FxtrCbor> {
	return {
		"$fxtr.ctx": new RawFxtrEntityMarkerID(ctx),
		...data,
	};
}

/** Create a store + arena with "." tracked. */
function makeArena(): { store: MemoryStore; arena: MemoryArena } {
	const store = new MemoryStore();
	const arena = store.newEmptyArena();
	arena.trackBuiltin(FxtrID.here());
	return { store, arena };
}

// =============================================================================
// MemoryStore Basics
// =============================================================================

describe("MemoryStore basics", () => {
	it("creates empty store", () => {
		const store = new MemoryStore();
		expect(store).toBeDefined();
	});

	it("creates a working arena", () => {
		const store = new MemoryStore();
		const arena = store.newEmptyArena();
		expect(arena).toBeInstanceOf(MemoryArena);
		arena.close();
	});

	it("creates multiple arenas", () => {
		const store = new MemoryStore();
		const arena1 = store.newEmptyArena();
		const arena2 = store.newEmptyArena();
		expect(arena1).not.toBe(arena2);
		expect(arena1._arenaKey).not.toBe(arena2._arenaKey);
		arena1.close();
		arena2.close();
	});
});

// =============================================================================
// MemoryArena Basic Operations
// =============================================================================

describe("MemoryArena basics", () => {
	it("new arena tracks builtin after trackBuiltin", () => {
		const { arena } = makeArena();
		expect(arena.isTracked(FxtrID.here())).toBe(true);
		expect(arena.isTracked(FxtrID.builtin("not_tracked"))).toBe(false);
		arena.close();
	});

	it("trackBuiltin tracks . and ! nodes", () => {
		const { arena } = makeArena();
		const builtin = FxtrID.builtin("test_type");
		arena.trackBuiltin(builtin);
		expect(arena.isTracked(builtin)).toBe(true);
		arena.close();
	});

	it("closed arena returns false for isTracked", () => {
		const { store } = makeArena();
		const arena = store.newEmptyArena();
		const ground = FxtrID.here();
		arena.trackBuiltin(ground);
		expect(arena.isTracked(ground)).toBe(true);
		arena.close();
		expect(arena.isTracked(ground)).toBe(false);
	});
});

// =============================================================================
// Storing Ground Entities (d-nodes)
// =============================================================================

describe("store ground entities", () => {
	it("stores simple entity", async () => {
		const { arena } = makeArena();
		const typeId = FxtrID.builtin("test");
		arena.trackBuiltin(typeId);

		const entity = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(typeId),
			name: "test",
			value: 42,
		});
		const nodeId = await arena.store(entity);
		expect(nodeId).toBeDefined();
		expect(arena.isTracked(nodeId)).toBe(true);
		arena.close();
	});

	it("returns d-node ID", async () => {
		const { arena } = makeArena();
		const entity = makeSimpleEntity(groundCtx(), { data: "test" });
		const nodeId = await arena.store(entity);
		expect(nodeId.getTag()).toBe("d");
		arena.close();
	});

	it("same data same ID (content-addressed)", async () => {
		const { arena } = makeArena();
		const entity = makeSimpleEntity(groundCtx(), { data: "test" });
		const id1 = await arena.store(entity);
		const id2 = await arena.store(entity);
		expect(id1.equals(id2)).toBe(true);
		arena.close();
	});

	it("different data different ID", async () => {
		const { arena } = makeArena();
		const id1 = await arena.store(makeSimpleEntity(groundCtx(), { data: "test1" }));
		const id2 = await arena.store(makeSimpleEntity(groundCtx(), { data: "test2" }));
		expect(id1.equals(id2)).toBe(false);
		arena.close();
	});

	it("stores nested entities with refs", async () => {
		const { arena } = makeArena();
		const childId = await arena.store(makeSimpleEntity(groundCtx(), { child_data: "child" }));

		const parent = makeSimpleEntity(groundCtx(), {
			child_ref: new RawStrongFxtrID(childId),
		});
		const parentId = await arena.store(parent);
		expect(arena.isTracked(parentId)).toBe(true);
		expect(arena.isTracked(childId)).toBe(true);
		arena.close();
	});

	it("rejects entity with untracked refs", async () => {
		const { arena } = makeArena();
		const untracked = FxtrID.builtin("untracked");
		const entity = makeSimpleEntity(groundCtx(), {
			ref: new RawWeakFxtrID(untracked),
		});
		await expect(arena.store(entity)).rejects.toThrow("untracked");
		arena.close();
	});
});

// =============================================================================
// Loading Ground Entities (d-nodes)
// =============================================================================

describe("load ground entities", () => {
	it("loads stored entity", async () => {
		const { arena } = makeArena();
		const entity = makeSimpleEntity(groundCtx(), {
			name: "test",
			value: 42,
		});
		const nodeId = await arena.store(entity);
		const loaded = (await arena.load(nodeId)) as Record<string, FxtrCbor>;

		expect(loaded.name).toBe("test");
		expect(loaded.value).toBe(42);
		arena.close();
	});

	it("loading tracks children", async () => {
		const { store, arena } = makeArena();
		const childId = await arena.store(makeSimpleEntity(groundCtx(), { child_data: "child" }));
		const parentId = await arena.store(
			makeSimpleEntity(groundCtx(), {
				child_ref: new RawStrongFxtrID(childId),
			}),
		);

		// New arena with just the parent
		const newArena = store.newEmptyArena();
		newArena._trackFromStore(parentId);
		expect(newArena.isTracked(parentId)).toBe(true);
		expect(newArena.isTracked(childId)).toBe(false);

		await newArena.load(parentId);
		expect(newArena.isTracked(childId)).toBe(true);
		newArena.close();
		arena.close();
	});

	it("rejects load of untracked node", () => {
		const { arena } = makeArena();
		const untracked = FxtrID.builtin("untracked");
		expect(() => arena.load(untracked)).toThrow("not tracked");
		arena.close();
	});

	it("store -> load roundtrips data", async () => {
		const { arena } = makeArena();
		const entity = makeSimpleEntity(groundCtx(), {
			int_val: 42,
			str_val: "hello",
			list_val: [1, 2, 3],
			bool_val: true,
			bytes_val: new Uint8Array([1, 2, 3]),
		});
		const nodeId = await arena.store(entity);
		const loaded = (await arena.load(nodeId)) as Record<string, FxtrCbor>;

		expect(loaded.int_val).toBe(42);
		expect(loaded.str_val).toBe("hello");
		expect(loaded.list_val).toEqual([1, 2, 3]);
		expect(loaded.bool_val).toBe(true);
		expect(loaded.bytes_val).toEqual(new Uint8Array([1, 2, 3]));
		arena.close();
	});
});

// =============================================================================
// Storing Provenance Entities (n-nodes)
// =============================================================================

describe("store provenance entities (n-nodes)", () => {
	it("stores n-node with c-node context", async () => {
		const { arena } = makeArena();
		const ctxType = FxtrID.builtin("ctx");
		const testType = FxtrID.builtin("test");
		arena.trackBuiltin(ctxType);
		arena.trackBuiltin(testType);

		const ctxData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(ctxType),
			value: new RawWeakFxtrID(FxtrID.here()),
			ctx_key: "test_ctx",
			metadata: { version: 1 },
		});
		const cNodeId = await arena.createCNode(ctxData);

		const uTestType = await arena.createUNode(testType);

		const entity: Record<string, FxtrCbor> = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(cNodeId),
			"$fxtr.type": new RawWeakFxtrID(uTestType),
			data: "provenance data",
		};
		const nNodeId = await arena.store(entity);
		expect(nNodeId.getTag()).toBe("n");
		arena.close();
	});

	it("n-node unwraps to d-node", async () => {
		const { arena } = makeArena();
		const ctxType = FxtrID.builtin("ctx");
		const testType = FxtrID.builtin("test");
		arena.trackBuiltin(ctxType);
		arena.trackBuiltin(testType);

		const ctxData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(ctxType),
			value: new RawWeakFxtrID(FxtrID.here()),
			ctx_key: "test_ctx",
			metadata: { version: 1 },
		});
		const cNodeId = await arena.createCNode(ctxData);
		const uTestType = await arena.createUNode(testType);

		const entity: Record<string, FxtrCbor> = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(cNodeId),
			"$fxtr.type": new RawWeakFxtrID(uTestType),
			data: "provenance data",
		};
		const nNodeId = await arena.store(entity);

		const dNodeId = arena.unwrapProvenance(nNodeId);
		expect(dNodeId).not.toBeNull();
		expect(dNodeId?.getTag()).toBe("d");
		arena.close();
	});
});

// =============================================================================
// Context Nodes (c-nodes)
// =============================================================================

describe("context nodes (c-nodes)", () => {
	it("creates c-node from context data", async () => {
		const { arena } = makeArena();
		const ctxType = FxtrID.builtin("ctx");
		arena.trackBuiltin(ctxType);

		const ctxData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(ctxType),
			value: new RawWeakFxtrID(FxtrID.here()),
			ctx_key: "test_ctx",
			metadata: {},
		});
		const cNodeId = await arena.createCNode(ctxData);
		expect(cNodeId.getTag()).toBe("c");
		expect(arena.isTracked(cNodeId)).toBe(true);
		arena.close();
	});

	it("c-node requires value field", async () => {
		const { arena } = makeArena();
		const ctxData = makeSimpleEntity(groundCtx(), {
			ctx_key: "test_ctx",
			metadata: {},
		});
		await expect(arena.createCNode(ctxData)).rejects.toThrow("value");
		arena.close();
	});

	it("c-node unwraps to value", async () => {
		const { arena } = makeArena();
		const ctxType = FxtrID.builtin("ctx");
		arena.trackBuiltin(ctxType);

		const ctxData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(ctxType),
			value: new RawWeakFxtrID(FxtrID.here()),
			ctx_key: "test_ctx",
			metadata: {},
		});
		const cNodeId = await arena.createCNode(ctxData);
		const unwrapped = arena.unwrapProvenance(cNodeId);
		expect(unwrapped?.equals(FxtrID.here())).toBe(true);
		arena.close();
	});

	it("c_node_to_context_data returns d-node", async () => {
		const { arena } = makeArena();
		const ctxType = FxtrID.builtin("ctx");
		arena.trackBuiltin(ctxType);

		const ctxData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(ctxType),
			value: new RawWeakFxtrID(FxtrID.here()),
			ctx_key: "test_ctx",
			metadata: {},
		});
		const cNodeId = await arena.createCNode(ctxData);
		const dNodeId = arena.cNodeToContextData(cNodeId);
		expect(dNodeId.getTag()).toBe("d");
		expect(arena.isTracked(dNodeId)).toBe(true);
		arena.close();
	});
});

// =============================================================================
// Path Nodes (p-nodes)
// =============================================================================

describe("path nodes (p-nodes)", () => {
	it("creates p-node from pathroot data", async () => {
		const { arena } = makeArena();
		const pathrootType = FxtrID.builtin("pathroot");
		arena.trackBuiltin(pathrootType);

		const rootId = await arena.store(makeSimpleEntity(groundCtx(), { data: "root" }));

		const pathrootData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(pathrootType),
			value: new RawWeakFxtrID(rootId),
			ctx_key: "test_path",
			metadata: {},
		});
		const pNodeId = await arena.createPNode(pathrootData);
		expect(pNodeId.getTag()).toBe("p");
		expect(arena.isTracked(pNodeId)).toBe(true);
		arena.close();
	});

	it("p-node unwraps to root", async () => {
		const { arena } = makeArena();
		const pathrootType = FxtrID.builtin("pathroot");
		arena.trackBuiltin(pathrootType);

		const rootId = await arena.store(makeSimpleEntity(groundCtx(), { data: "root" }));

		const pathrootData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(pathrootType),
			value: new RawWeakFxtrID(rootId),
			ctx_key: "test_path",
			metadata: {},
		});
		const pNodeId = await arena.createPNode(pathrootData);
		const unwrapped = arena.unwrapProvenance(pNodeId);
		expect(unwrapped?.equals(rootId)).toBe(true);
		arena.close();
	});

	it("p_node_to_pathroot returns d-node", async () => {
		const { arena } = makeArena();
		const pathrootType = FxtrID.builtin("pathroot");
		arena.trackBuiltin(pathrootType);

		const rootId = await arena.store(makeSimpleEntity(groundCtx(), { data: "root" }));

		const pathrootData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(pathrootType),
			value: new RawWeakFxtrID(rootId),
			ctx_key: "test_path",
			metadata: {},
		});
		const pNodeId = await arena.createPNode(pathrootData);
		const pathrootId = arena.pNodeToPathroot(pNodeId);
		expect(pathrootId.getTag()).toBe("d");
		expect(arena.isTracked(pathrootId)).toBe(true);
		arena.close();
	});

	it("loading p-node creates p-node children", async () => {
		const { arena } = makeArena();
		const pathrootType = FxtrID.builtin("pathroot");
		arena.trackBuiltin(pathrootType);

		const child1Id = await arena.store(makeSimpleEntity(groundCtx(), { child: "1" }));
		const child2Id = await arena.store(makeSimpleEntity(groundCtx(), { child: "2" }));

		const rootEntity = makeSimpleEntity(groundCtx(), {
			children: [new RawStrongFxtrID(child1Id), new RawStrongFxtrID(child2Id)],
		});
		const rootId = await arena.store(rootEntity);

		const pathrootData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(pathrootType),
			value: new RawWeakFxtrID(rootId),
			ctx_key: "test_path",
			metadata: {},
		});
		const pNodeId = await arena.createPNode(pathrootData);

		const loaded = (await arena.load(pNodeId)) as Record<string, FxtrCbor>;
		expect(loaded.children).toBeDefined();
		const children = loaded.children as FxtrCbor[];
		for (const childRef of children) {
			expect(childRef).toBeInstanceOf(RawStrongFxtrID);
			expect((childRef as RawStrongFxtrID).innerId.getTag()).toBe("p");
		}
		arena.close();
	});
});

// =============================================================================
// Untracked Nodes (u-nodes)
// =============================================================================

describe("untracked nodes (u-nodes)", () => {
	it("creates u-node wrapping data node", async () => {
		const { arena } = makeArena();
		const dNodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "test" }));
		const uNodeId = await arena.createUNode(dNodeId);
		expect(uNodeId.getTag()).toBe("u");
		expect(arena.isTracked(uNodeId)).toBe(true);
		arena.close();
	});

	it("u-node unwraps to wrapped node", async () => {
		const { arena } = makeArena();
		const dNodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "test" }));
		const uNodeId = await arena.createUNode(dNodeId);
		const unwrapped = arena.unwrapProvenance(uNodeId);
		expect(unwrapped?.equals(dNodeId)).toBe(true);
		arena.close();
	});

	it("loading u-node wraps children as u-nodes", async () => {
		const { arena } = makeArena();
		const childId = await arena.store(makeSimpleEntity(groundCtx(), { child: "data" }));
		const rootId = await arena.store(
			makeSimpleEntity(groundCtx(), {
				child_ref: new RawStrongFxtrID(childId),
			}),
		);
		const uNodeId = await arena.createUNode(rootId);

		const loaded = (await arena.load(uNodeId)) as Record<string, FxtrCbor>;
		const childRef = loaded.child_ref;
		expect(childRef).toBeInstanceOf(RawStrongFxtrID);
		expect((childRef as RawStrongFxtrID).innerId.getTag()).toBe("u");
		arena.close();
	});
});

// =============================================================================
// Unwrapping Provenance
// =============================================================================

describe("unwrap provenance", () => {
	it("d-node returns null on unwrap", async () => {
		const { arena } = makeArena();
		const dNodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "test" }));
		expect(arena.unwrapProvenance(dNodeId)).toBeNull();
		arena.close();
	});

	it("here node returns null on unwrap", () => {
		const { arena } = makeArena();
		expect(arena.unwrapProvenance(FxtrID.here())).toBeNull();
		arena.close();
	});

	it("builtin returns null on unwrap", () => {
		const { arena } = makeArena();
		const builtin = FxtrID.builtin("test");
		arena.trackBuiltin(builtin);
		expect(arena.unwrapProvenance(builtin)).toBeNull();
		arena.close();
	});
});

// =============================================================================
// Arena Abandonment
// =============================================================================

describe("arena abandonment", () => {
	it("abandon removes from liveset", async () => {
		const { arena } = makeArena();
		const nodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "test" }));
		expect(arena.isTracked(nodeId)).toBe(true);
		arena.abandon(nodeId);
		expect(arena.isTracked(nodeId)).toBe(false);
		arena.close();
	});

	it("abandon of untracked throws", () => {
		const { arena } = makeArena();
		const untracked = FxtrID.builtin("untracked");
		expect(() => arena.abandon(untracked)).toThrow("not tracked");
		arena.close();
	});
});

// =============================================================================
// Arena Copying
// =============================================================================

describe("arena copying", () => {
	it("copyToNewArena creates independent arena", async () => {
		const { arena } = makeArena();
		const nodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "test" }));

		const newArena = arena.copyToNewArena([nodeId]);
		expect(newArena.isTracked(nodeId)).toBe(true);
		expect(newArena).not.toBe(arena);
		newArena.close();
		arena.close();
	});

	it("copied arena independent of original", async () => {
		const { arena } = makeArena();
		const nodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "test" }));

		const newArena = arena.copyToNewArena([nodeId]);
		arena.abandon(nodeId);
		expect(arena.isTracked(nodeId)).toBe(false);
		expect(newArena.isTracked(nodeId)).toBe(true);
		newArena.close();
		arena.close();
	});

	it("copyToNewArena rejects untracked nodes", () => {
		const { arena } = makeArena();
		const untracked = FxtrID.builtin("untracked");
		expect(() => arena.copyToNewArena([untracked])).toThrow("not tracked");
		arena.close();
	});

	it("copyFromArena (same store)", async () => {
		const { store, arena: arena1 } = makeArena();
		const nodeId = await arena1.store(makeSimpleEntity(groundCtx(), { data: "shared" }));

		const arena2 = store.newEmptyArena();
		arena2.trackBuiltin(FxtrID.here());
		await arena2.copyFromArena(nodeId, arena1);
		expect(arena2.isTracked(nodeId)).toBe(true);

		const loaded = (await arena2.load(nodeId)) as Record<string, FxtrCbor>;
		expect(loaded.data).toBe("shared");

		arena1.close();
		arena2.close();
	});

	it("copyFromArena (different store)", async () => {
		const store1 = new MemoryStore();
		const arena1 = store1.newEmptyArena();
		arena1.trackBuiltin(FxtrID.here());
		const nodeId = await arena1.store(makeSimpleEntity(groundCtx(), { data: "cross_store" }));

		const store2 = new MemoryStore();
		const arena2 = store2.newEmptyArena();
		arena2.trackBuiltin(FxtrID.here());

		await arena2.copyFromArena(nodeId, arena1);
		expect(arena2.isTracked(nodeId)).toBe(true);

		const loaded = (await arena2.load(nodeId)) as Record<string, FxtrCbor>;
		expect(loaded.data).toBe("cross_store");

		arena1.close();
		arena2.close();
	});

	it("copyFromArena copies nested entities between stores", async () => {
		const store1 = new MemoryStore();
		const arena1 = store1.newEmptyArena();
		arena1.trackBuiltin(FxtrID.here());
		const childId = await arena1.store(makeSimpleEntity(groundCtx(), { child: "data" }));
		const parentId = await arena1.store(
			makeSimpleEntity(groundCtx(), {
				ref: new RawStrongFxtrID(childId),
			}),
		);

		const store2 = new MemoryStore();
		const arena2 = store2.newEmptyArena();
		arena2.trackBuiltin(FxtrID.here());

		await arena2.copyFromArena(parentId, arena1);
		expect(arena2.isTracked(parentId)).toBe(true);

		const loaded = (await arena2.load(parentId)) as Record<string, FxtrCbor>;
		const ref = loaded.ref as RawStrongFxtrID;
		expect(arena2.isTracked(ref.innerId)).toBe(true);

		arena1.close();
		arena2.close();
	});
});

// =============================================================================
// Error Cases
// =============================================================================

describe("error cases", () => {
	it("store non-object raises", async () => {
		const { arena } = makeArena();
		await expect(arena.store([1, 2, 3] as unknown as FxtrCbor)).rejects.toThrow();
		arena.close();
	});

	it("store missing $fxtr.ctx raises", async () => {
		const { arena } = makeArena();
		await expect(arena.store({ data: "test" })).rejects.toThrow();
		arena.close();
	});

	it("load contentless node raises", async () => {
		const { arena } = makeArena();
		await expect(arena.load(FxtrID.here())).rejects.toThrow("contentless");

		const builtin = FxtrID.builtin("test");
		arena.trackBuiltin(builtin);
		await expect(arena.load(builtin)).rejects.toThrow("contentless");
		arena.close();
	});
});

// =============================================================================
// Edge Cases
// =============================================================================

describe("edge cases", () => {
	it("entity with multiple refs to same child", async () => {
		const { arena } = makeArena();
		const childId = await arena.store(makeSimpleEntity(groundCtx(), { child_data: "shared" }));

		const parent = makeSimpleEntity(groundCtx(), {
			weak_ref1: new RawWeakFxtrID(childId),
			weak_ref2: new RawWeakFxtrID(childId),
			strong_ref: new RawStrongFxtrID(childId),
		});
		const parentId = await arena.store(parent);

		const loaded = (await arena.load(parentId)) as Record<string, FxtrCbor>;
		expect((loaded.weak_ref1 as RawWeakFxtrID).innerId.equals(childId)).toBe(true);
		expect((loaded.weak_ref2 as RawWeakFxtrID).innerId.equals(childId)).toBe(true);
		expect((loaded.strong_ref as RawStrongFxtrID).innerId.equals(childId)).toBe(true);
		arena.close();
	});

	it("empty entity (only $fxtr.ctx)", async () => {
		const { arena } = makeArena();
		const nodeId = await arena.store(makeSimpleEntity(groundCtx()));
		expect(nodeId.getTag()).toBe("d");
		arena.close();
	});

	it("large entity (100 fields)", async () => {
		const { arena } = makeArena();
		const fields: Record<string, FxtrCbor> = {};
		for (let i = 0; i < 100; i++) {
			fields[`field_${i}`] = `value_${i}`;
		}
		const nodeId = await arena.store(makeSimpleEntity(groundCtx(), fields));
		expect(arena.isTracked(nodeId)).toBe(true);
		arena.close();
	});

	it("deeply nested entity", async () => {
		const { arena } = makeArena();
		let nested: Record<string, FxtrCbor> = { level: 10 };
		for (let i = 9; i >= 0; i--) {
			nested = { level: i, inner: nested };
		}
		const entity = makeSimpleEntity(groundCtx(), { nested });
		const nodeId = await arena.store(entity);
		const loaded = (await arena.load(nodeId)) as Record<string, FxtrCbor>;
		expect((loaded.nested as Record<string, FxtrCbor>).level).toBe(0);
		arena.close();
	});

	it("all primitive types", async () => {
		const { arena } = makeArena();
		const entity = makeSimpleEntity(groundCtx(), {
			none: null,
			bool_true: true,
			bool_false: false,
			int_pos: 42,
			int_neg: -42,
			float: 3.14,
			str: "hello",
			bytes: new Uint8Array([0, 1, 2, 255]),
			list: [1, 2, 3],
			dict: { a: 1 },
		});
		const nodeId = await arena.store(entity);
		expect(arena.isTracked(nodeId)).toBe(true);
		arena.close();
	});

	it("entity with bytes field roundtrips", async () => {
		const { arena } = makeArena();
		const entity = makeSimpleEntity(groundCtx(), {
			binary: new Uint8Array([0, 1, 2, 255]),
		});
		const nodeId = await arena.store(entity);
		const loaded = (await arena.load(nodeId)) as Record<string, FxtrCbor>;
		expect(loaded.binary).toEqual(new Uint8Array([0, 1, 2, 255]));
		arena.close();
	});

	it("shared dependency referenced twice across stores", async () => {
		/**
		 * Regression test: loading an entity that references the same dependency
		 * twice from a lazily attached arena should not fail.
		 */
		const store1 = new MemoryStore();
		const x1 = store1.newEmptyArena();
		x1.trackBuiltin(FxtrID.here());

		const store2 = new MemoryStore();
		const x2 = store2.newEmptyArena();
		x2.trackBuiltin(FxtrID.here());

		try {
			const idA = await x1.store(makeSimpleEntity(groundCtx(), { name: "A" }));
			const idB = await x1.store(
				makeSimpleEntity(groundCtx(), {
					name: "B",
					ref: new RawStrongFxtrID(idA),
				}),
			);
			const idC = await x1.store(
				makeSimpleEntity(groundCtx(), {
					name: "C",
					ref1: new RawStrongFxtrID(idA),
					ref2: new RawStrongFxtrID(idA),
				}),
			);

			const subB = x1.copyToNewArena([idB]);
			const subC = x1.copyToNewArena([idC]);

			x2.unsafeLazilyAttachFromArena(idB, subB);
			const loadedB = (await x2.load(idB)) as Record<string, FxtrCbor>;
			expect(loadedB.name).toBe("B");

			x2.unsafeLazilyAttachFromArena(idC, subC);
			const loadedC = (await x2.load(idC)) as Record<string, FxtrCbor>;
			expect(loadedC.name).toBe("C");

			const loadedA = (await x2.load(idA)) as Record<string, FxtrCbor>;
			expect(loadedA.name).toBe("A");
		} finally {
			store1.close(true);
			store2.close(true);
		}
	});
});

// =============================================================================
// Integration: Store/Load/Unwrap Flow
// =============================================================================

describe("integration flow", () => {
	it("full provenance flow: c-node -> n-node -> unwrap -> load both", async () => {
		const { arena } = makeArena();
		const ctxType = FxtrID.builtin("ctx");
		const testType = FxtrID.builtin("test");
		arena.trackBuiltin(ctxType);
		arena.trackBuiltin(testType);

		const ctxData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(ctxType),
			value: new RawWeakFxtrID(FxtrID.here()),
			ctx_key: "full_flow",
			metadata: { test: true },
		});
		const cNodeId = await arena.createCNode(ctxData);
		const uTestType = await arena.createUNode(testType);

		const entity: Record<string, FxtrCbor> = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(cNodeId),
			"$fxtr.type": new RawWeakFxtrID(uTestType),
			name: "provenance_entity",
			value: 123,
		};
		const nNodeId = await arena.store(entity);
		expect(nNodeId.getTag()).toBe("n");

		const dNodeId = arena.unwrapProvenance(nNodeId);
		expect(dNodeId).not.toBeNull();
		expect(dNodeId?.getTag()).toBe("d");

		const nLoaded = (await arena.load(nNodeId)) as Record<string, FxtrCbor>;
		// biome-ignore lint/style/noNonNullAssertion: asserted not null above
		const dLoaded = (await arena.load(dNodeId!)) as Record<string, FxtrCbor>;

		expect(nLoaded.name).toBe("provenance_entity");
		expect(dLoaded.name).toBe("provenance_entity");
		arena.close();
	});

	it("p-node path traversal: create tree, traverse via loads", async () => {
		const { arena } = makeArena();
		const pathrootType = FxtrID.builtin("pathroot");
		arena.trackBuiltin(pathrootType);

		const leaf1Id = await arena.store(makeSimpleEntity(groundCtx(), { type: "leaf", value: 1 }));
		const leaf2Id = await arena.store(makeSimpleEntity(groundCtx(), { type: "leaf", value: 2 }));
		const branchId = await arena.store(
			makeSimpleEntity(groundCtx(), {
				type: "branch",
				left: new RawStrongFxtrID(leaf1Id),
				right: new RawStrongFxtrID(leaf2Id),
			}),
		);
		const rootId = await arena.store(
			makeSimpleEntity(groundCtx(), {
				type: "root",
				child: new RawStrongFxtrID(branchId),
			}),
		);

		const pathrootData = makeSimpleEntity(groundCtx(), {
			"$fxtr.type": new RawWeakFxtrID(pathrootType),
			value: new RawWeakFxtrID(rootId),
			ctx_key: "tree_path",
			metadata: {},
		});
		const pRootId = await arena.createPNode(pathrootData);

		const pRootLoaded = (await arena.load(pRootId)) as Record<string, FxtrCbor>;
		expect(pRootLoaded.type).toBe("root");

		const pChildRef = pRootLoaded.child as RawStrongFxtrID;
		expect(pChildRef.innerId.getTag()).toBe("p");

		const pChildLoaded = (await arena.load(pChildRef.innerId)) as Record<string, FxtrCbor>;
		expect(pChildLoaded.type).toBe("branch");
		arena.close();
	});
});

// =============================================================================
// load_flat
// =============================================================================

describe("loadFlat", () => {
	it("d-node returns bytes and deps", async () => {
		const { arena } = makeArena();
		const nodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "flat_test" }));
		const flat = await arena.loadFlat(nodeId);

		expect(flat.entityId.equals(nodeId)).toBe(true);
		expect(flat.entityBytes).not.toBeNull();

		const depIds = flat.deps.map(([id]) => id.key());
		expect(depIds).toContain(FxtrID.here().key());

		const markerDeps = flat.deps.filter(
			([id, kind]) => id.equals(FxtrID.here()) && kind === DepKind.WEAK_REF,
		);
		expect(markerDeps.length).toBe(1);
		arena.close();
	});

	it("n-node returns bytes with UNWRAPS_TO dep", async () => {
		const { arena } = makeArena();
		const ctxData = makeSimpleEntity(groundCtx(), {
			value: new RawWeakFxtrID(FxtrID.here()),
		});
		const cId = await arena.createCNode(ctxData);

		const provEntity: Record<string, FxtrCbor> = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(cId),
			data: "test",
		};
		const nId = await arena.store(provEntity);
		expect(nId.getTag()).toBe("n");

		const flat = await arena.loadFlat(nId);
		expect(flat.entityBytes).not.toBeNull();

		const depKinds = new Set(flat.deps.map(([, kind]) => kind));
		expect(depKinds.has(DepKind.UNWRAPS_TO)).toBe(true);
		arena.close();
	});

	it("c-node returns no bytes with CTX_NODE and UNWRAPS_TO deps", async () => {
		const { arena } = makeArena();
		const ctxData = makeSimpleEntity(groundCtx(), {
			value: new RawWeakFxtrID(FxtrID.here()),
		});
		const cId = await arena.createCNode(ctxData);

		const flat = await arena.loadFlat(cId);
		expect(flat.entityBytes).toBeNull();

		const depKinds = new Set(flat.deps.map(([, kind]) => kind));
		expect(depKinds.has(DepKind.CTX_NODE)).toBe(true);
		expect(depKinds.has(DepKind.UNWRAPS_TO)).toBe(true);
		arena.close();
	});

	it("builtin node returns no bytes and no deps", async () => {
		const { arena } = makeArena();
		const flat = await arena.loadFlat(FxtrID.here());

		expect(flat.entityBytes).toBeNull();
		expect(flat.deps.length).toBe(0);
		arena.close();
	});

	it("loadFlat tracks deps in arena", async () => {
		const { store, arena } = makeArena();
		const childId = await arena.store(makeSimpleEntity(groundCtx(), { name: "child" }));
		const parentId = await arena.store(
			makeSimpleEntity(groundCtx(), {
				child: new RawStrongFxtrID(childId),
			}),
		);

		const newArena = store.newEmptyArena();
		newArena._trackFromStore(parentId);
		expect(newArena.isTracked(childId)).toBe(false);

		await newArena.loadFlat(parentId);
		expect(newArena.isTracked(childId)).toBe(true);

		newArena.close();
		arena.close();
	});

	it("bare_store_flat roundtrip", async () => {
		const { store, arena } = makeArena();
		const entity = makeSimpleEntity(groundCtx(), { data: [1, 2, 3] });
		const entityId = await arena.store(entity);

		const flatData = await store.bareLoadFlat(entityId);

		const newStore = new MemoryStore();
		const newArena = newStore.newEmptyArena();
		newArena.trackBuiltin(FxtrID.here());

		const storedId = newStore.bareStoreFlat(flatData);
		expect(storedId.equals(entityId)).toBe(true);

		newArena._trackFromStore(storedId);
		const loaded = (await newArena.load(storedId)) as Record<string, FxtrCbor>;
		expect(loaded.data).toEqual([1, 2, 3]);

		arena.close();
		newArena.close();
	});
});

// =============================================================================
// DeepFetchable (tryLookup)
// =============================================================================

describe("DeepFetchable", () => {
	it("tryLookup finds tracked entity", async () => {
		const { store, arena } = makeArena();
		const nodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "findme" }));

		const result = await store.tryLookup(nodeId);
		expect(result).not.toBeNull();
		expect(result?.isTracked(nodeId)).toBe(true);
		result?.close();
		arena.close();
	});

	it("tryLookup returns null for unknown entity", async () => {
		const store = new MemoryStore();
		const unknownId = FxtrID.fromIdString("dabcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrs");
		const result = await store.tryLookup(unknownId);
		expect(result).toBeNull();
	});
});

// =============================================================================
// Clone
// =============================================================================

describe("clone", () => {
	it("clones arena with same tracked set", async () => {
		const { arena } = makeArena();
		const nodeId = await arena.store(makeSimpleEntity(groundCtx(), { data: "clone_me" }));

		const cloned = arena.clone();
		expect(cloned.isTracked(nodeId)).toBe(true);
		expect(cloned.isTracked(FxtrID.here())).toBe(true);

		arena.close();
		expect(cloned.isTracked(nodeId)).toBe(true);

		const loaded = (await cloned.load(nodeId)) as Record<string, FxtrCbor>;
		expect(loaded.data).toBe("clone_me");
		cloned.close();
	});
});

// =============================================================================
// Lazy attachment
// =============================================================================

describe("unsafeLazilyAttachFromArena", () => {
	it("attaches entity from another store", async () => {
		const store1 = new MemoryStore();
		const arena1 = store1.newEmptyArena();
		arena1.trackBuiltin(FxtrID.here());
		const nodeId = await arena1.store(makeSimpleEntity(groundCtx(), { data: "lazy" }));

		const store2 = new MemoryStore();
		const arena2 = store2.newEmptyArena();
		arena2.trackBuiltin(FxtrID.here());

		arena2.unsafeLazilyAttachFromArena(nodeId, arena1);
		expect(arena2.isTracked(nodeId)).toBe(true);

		const loaded = (await arena2.load(nodeId)) as Record<string, FxtrCbor>;
		expect(loaded.data).toBe("lazy");

		arena1.close();
		arena2.close();
	});
});

// =============================================================================
// copyFromSiblingMemoryArena
// =============================================================================

describe("copyFromSiblingMemoryArena", () => {
	it("copies between sibling arenas", async () => {
		const { store, arena: arena1 } = makeArena();
		const nodeId = await arena1.store(makeSimpleEntity(groundCtx(), { data: "sibling" }));

		const arena2 = store.newEmptyArena();
		arena2.copyFromSiblingMemoryArena(nodeId, arena1);
		expect(arena2.isTracked(nodeId)).toBe(true);

		const loaded = (await arena2.load(nodeId)) as Record<string, FxtrCbor>;
		expect(loaded.data).toBe("sibling");

		arena1.close();
		arena2.close();
	});

	it("rejects different store", () => {
		const store1 = new MemoryStore();
		const arena1 = store1.newEmptyArena();
		const store2 = new MemoryStore();
		const arena2 = store2.newEmptyArena();

		const fakeId = FxtrID.here();
		arena1.trackBuiltin(fakeId);

		expect(() => arena2.copyFromSiblingMemoryArena(fakeId, arena1)).toThrow("same store");

		arena1.close();
		arena2.close();
	});
});

// =============================================================================
// Store close
// =============================================================================

describe("store close", () => {
	it("close with closeArenas cleans up", async () => {
		const store = new MemoryStore();
		const arena = store.newEmptyArena();
		arena.trackBuiltin(FxtrID.here());
		await arena.store(makeSimpleEntity(groundCtx(), { data: "test" }));
		store.close(true);
	});
});
