/**
 * Unit tests for the structural state walks in `state.ts`:
 * `collectStubsInState` (with the new op-result slot surface) and
 * `diffStateToPatch` (with slot-based equality for
 * `WorkstationOpResultStub`).
 *
 * Codec-level round-trips are covered in `escape.test.ts`; this
 * file focuses on the slot bookkeeping the chunk-3 reducer will
 * consume.
 */

import { EntityRefStub, ServiceRefStub, type StateValue } from "@fxtr-ui/core";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";

import {
	type EscapeOptions,
	type FillRecord,
	type InboxLog,
	type InboxSubscriptionState,
	TAG_BROKEN_OP_RESULT,
	TAG_OP_RESULT,
	WorkstationCallbackInboxStub,
	WorkstationOpResultStub,
	type WorkstationStubSession,
	collectStubsInState,
	diffStateToPatch,
} from "../src";

class TestEntityStub extends EntityRefStub {
	constructor(readonly shortId: string) {
		super();
	}
}

class TestServiceStub extends ServiceRefStub {
	constructor(readonly shortId: string) {
		super();
	}
}

const testSession: WorkstationStubSession = {
	sessionId: "test",
	slotFillsAtom: atom<ReadonlyMap<number, FillRecord>>(new Map<number, FillRecord>()),
	committedSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
	brokenSlotsAtom: atom<ReadonlySet<number>>(new Set<number>()),
	inboxLogsAtom: atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>()),
	committedInboxSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
	inboxSubscriptionStateAtom: atom<ReadonlyMap<number, InboxSubscriptionState>>(
		new Map<number, InboxSubscriptionState>(),
	),
};

function makeOpts(): EscapeOptions {
	return {
		encodeEntity: (s) => s.shortId,
		encodeService: (s) => s.shortId,
		encodeOpResult: (stub) => {
			if (!(stub instanceof WorkstationOpResultStub)) {
				throw new Error("unexpected stub type");
			}
			if (stub.slot === 0) return [TAG_BROKEN_OP_RESULT, 0];
			return [TAG_OP_RESULT, stub.slot];
		},
	};
}

describe("collectStubsInState: op-result slot collection", () => {
	it("surfaces slot integers from WorkstationOpResultStub leaves", () => {
		const v = {
			a: new WorkstationOpResultStub(3, testSession),
			b: [new WorkstationOpResultStub(7, testSession)],
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.opResultSlots].sort((x, y) => x - y)).toEqual([3, 7]);
	});

	it("excludes the dead-sentinel (slot=0) from opResultSlots", () => {
		const v = {
			dead: new WorkstationOpResultStub(0, testSession),
			live: new WorkstationOpResultStub(4, testSession),
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.opResultSlots]).toEqual([4]);
	});

	it("collects entity / service shortIds and op-result slots together in one walk", () => {
		const v = {
			ref: new TestEntityStub("e1"),
			svc: new TestServiceStub("s1"),
			result: new WorkstationOpResultStub(2, testSession),
			nested: {
				more: [new TestEntityStub("e2"), new WorkstationOpResultStub(5, testSession)],
			},
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.entityShortIds].sort()).toEqual(["e1", "e2"]);
		expect([...c.serviceShortIds]).toEqual(["s1"]);
		expect([...c.opResultSlots].sort((a, b) => a - b)).toEqual([2, 5]);
	});

	it("dedups slot integers when the same stub appears multiple times", () => {
		const stub = new WorkstationOpResultStub(9, testSession);
		const v = { a: stub, b: [stub, stub] } as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.opResultSlots]).toEqual([9]);
	});
});

describe("collectStubsInState: inbox-slot collection", () => {
	it("surfaces (slot, offset) from WorkstationCallbackInboxStub leaves", () => {
		const v = {
			a: new WorkstationCallbackInboxStub(3, 0, testSession),
			b: new WorkstationCallbackInboxStub(7, 5, testSession),
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.inboxSlotOffsets.entries()].sort((x, y) => x[0] - y[0])).toEqual([
			[3, 0],
			[7, 5],
		]);
	});

	it("excludes the inbox dead-sentinel (slot=0)", () => {
		const v = {
			dead: new WorkstationCallbackInboxStub(0, 0, testSession),
			live: new WorkstationCallbackInboxStub(4, 2, testSession),
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.inboxSlotOffsets.entries()]).toEqual([[4, 2]]);
	});

	it("takes min-offset across multiple inbox stubs for the same slot", () => {
		const v = {
			a: new WorkstationCallbackInboxStub(5, 10, testSession),
			b: new WorkstationCallbackInboxStub(5, 3, testSession),
			c: new WorkstationCallbackInboxStub(5, 7, testSession),
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect(c.inboxSlotOffsets.get(5)).toBe(3);
	});

	it("collects op-result + inbox slots together in one walk", () => {
		const v = {
			op: new WorkstationOpResultStub(1, testSession),
			inbox: new WorkstationCallbackInboxStub(2, 4, testSession),
			nested: { another: new WorkstationCallbackInboxStub(3, 1, testSession) },
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.opResultSlots]).toEqual([1]);
		expect([...c.inboxSlotOffsets.entries()].sort((x, y) => x[0] - y[0])).toEqual([
			[2, 4],
			[3, 1],
		]);
	});
});

describe("diffStateToPatch: inbox stub equality", () => {
	const opts: EscapeOptions = {
		encodeEntity: (s) => s.shortId,
		encodeService: (s) => s.shortId,
		encodeOpResult: () => {
			throw new Error("op-result not used in inbox tests");
		},
		encodeInbox: (stub) => {
			if (!(stub instanceof WorkstationCallbackInboxStub)) {
				throw new Error("unexpected stub type");
			}
			return ["inbox", stub.slot, stub.offset];
		},
	};

	it("two inbox stubs with the same (slot, offset) diff as equal (no patch)", () => {
		const a = new WorkstationCallbackInboxStub(5, 3, testSession);
		const b = new WorkstationCallbackInboxStub(5, 3, testSession);
		const prev = { x: a } as unknown as StateValue;
		const next = { x: b } as unknown as StateValue;
		expect(diffStateToPatch(prev, next, opts)).toEqual([]);
	});

	it("same slot, different offsets diff as a replace (cursor advance)", () => {
		const a = new WorkstationCallbackInboxStub(5, 3, testSession);
		const b = new WorkstationCallbackInboxStub(5, 7, testSession);
		const prev = { x: a } as unknown as StateValue;
		const next = { x: b } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, opts);
		expect(patch).toEqual([{ op: "replace", path: "/x", value: ["inbox", 5, 7] }]);
	});

	it("different slots diff as a replace", () => {
		const a = new WorkstationCallbackInboxStub(5, 0, testSession);
		const b = new WorkstationCallbackInboxStub(6, 0, testSession);
		const prev = { x: a } as unknown as StateValue;
		const next = { x: b } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, opts);
		expect(patch).toEqual([{ op: "replace", path: "/x", value: ["inbox", 6, 0] }]);
	});

	it("adding an inbox stub emits an add with the inbox encoding", () => {
		const a = new WorkstationCallbackInboxStub(9, 2, testSession);
		const prev = {} as unknown as StateValue;
		const next = { x: a } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, opts);
		expect(patch).toEqual([{ op: "add", path: "/x", value: ["inbox", 9, 2] }]);
	});

	it("removing an inbox stub emits a remove", () => {
		const a = new WorkstationCallbackInboxStub(9, 2, testSession);
		const prev = { x: a } as unknown as StateValue;
		const next = {} as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, opts);
		expect(patch).toEqual([{ op: "remove", path: "/x" }]);
	});
});

describe("diffStateToPatch: slot equality", () => {
	it("two OpResultStubs with the same slot diff as equal (no patch)", () => {
		const a = new WorkstationOpResultStub(5, testSession);
		const b = new WorkstationOpResultStub(5, testSession);
		// Same slot, different instances — the diff should treat
		// them as equal.
		const prev = { x: a } as unknown as StateValue;
		const next = { x: b } as unknown as StateValue;
		expect(diffStateToPatch(prev, next, makeOpts())).toEqual([]);
	});

	it("two OpResultStubs with different slots diff as different", () => {
		const a = new WorkstationOpResultStub(5, testSession);
		const b = new WorkstationOpResultStub(6, testSession);
		const prev = { x: a } as unknown as StateValue;
		const next = { x: b } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "replace", path: "/x", value: ["op_result", 6] }]);
	});

	it("dead-sentinel replacing a regular slot emits a replace", () => {
		const regular = new WorkstationOpResultStub(5, testSession);
		const dead = new WorkstationOpResultStub(0, testSession);
		const prev = { x: regular } as unknown as StateValue;
		const next = { x: dead } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "replace", path: "/x", value: ["broken_op_result", 0] }]);
	});

	it("adding an OpResultStub emits an add with op_result encoding", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		const prev = {} as unknown as StateValue;
		const next = { x: stub } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "add", path: "/x", value: ["op_result", 7] }]);
	});
});

describe("diffStateToPatch: array-element patches target wire-form paths", () => {
	// The wire form wraps literal arrays as `[[items…]]`. JSON Patch
	// paths target that wire form, so descending into array elements
	// must include a `/0` step past the wrap. Without it, appending
	// produces `[[a], b]` instead of `[[a, b]]` and breaks the
	// list-escape invariant — observable when a fresh client attaches
	// and tries to unescape the corrupted state.

	it("appending elements emits adds at /path/0/-, not /path/-", () => {
		const prev = { x: [1, 2] } as unknown as StateValue;
		const next = { x: [1, 2, 3] } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "add", path: "/x/0/-", value: 3 }]);
	});

	it("removing tail elements emits removes at /path/0/N", () => {
		const prev = { x: [1, 2, 3] } as unknown as StateValue;
		const next = { x: [1] } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([
			{ op: "remove", path: "/x/0/2" },
			{ op: "remove", path: "/x/0/1" },
		]);
	});

	it("replacing an in-array element targets /path/0/i", () => {
		const prev = { x: [1, 2, 3] } as unknown as StateValue;
		const next = { x: [1, 99, 3] } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "replace", path: "/x/0/1", value: 99 }]);
	});

	it("nested-in-array dict edits descend through both wraps and dict keys", () => {
		const prev = {
			items: [{ k: "a" }, { k: "b" }],
		} as unknown as StateValue;
		const next = {
			items: [{ k: "a" }, { k: "B" }],
		} as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "replace", path: "/items/0/1/k", value: "B" }]);
	});
});

describe("collectStubsInState + diffStateToPatch: undefined-valued dict fields", () => {
	// Consumer code typed with optional fields can produce dicts with
	// explicit `undefined` values via spread or partial updates. The
	// walks treat these as absent (same as `escapeWire`).

	it("walkCollect skips explicit-undefined fields", () => {
		const v = {
			ref: new TestEntityStub("e1"),
			absent: undefined,
		} as unknown as StateValue;
		const c = collectStubsInState(v);
		expect([...c.entityShortIds]).toEqual(["e1"]);
	});

	it("diffWalk treats undefined as absent: prev=foo, next=undefined → remove", () => {
		const prev = { x: "foo" } as unknown as StateValue;
		const next = { x: undefined } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "remove", path: "/x" }]);
	});

	it("diffWalk treats undefined as absent: prev=undefined, next=foo → add", () => {
		const prev = { x: undefined } as unknown as StateValue;
		const next = { x: "foo" } as unknown as StateValue;
		const patch = diffStateToPatch(prev, next, makeOpts());
		expect(patch).toEqual([{ op: "add", path: "/x", value: "foo" }]);
	});

	it("diffWalk treats both-undefined as no-op (no patch entry)", () => {
		const prev = { x: undefined, y: 1 } as unknown as StateValue;
		const next = { x: undefined, y: 1 } as unknown as StateValue;
		expect(diffStateToPatch(prev, next, makeOpts())).toEqual([]);
	});
});
