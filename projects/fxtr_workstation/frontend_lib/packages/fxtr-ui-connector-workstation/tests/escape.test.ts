import { describe, expect, it } from "vitest";

import {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	type ResultStatus,
	ServiceRefStub,
} from "@fxtr-ui/core";
import type { StateValue } from "@fxtr-ui/core";
import { type Atom, atom } from "jotai";
import { createStore } from "jotai";
import {
	TAG_BROKEN_OP_RESULT,
	TAG_OP_RESULT,
	WireEscapeError,
	WorkstationOpResultStub,
	type WorkstationStubSession,
	escapeInvokeArgs,
	escapeWire,
	unescapeWire,
} from "../src";
import type {
	EscapeOptions,
	FillRecord,
	InboxLog,
	InboxSubscriptionState,
	StateUnescapeOptions,
	WireValue,
} from "../src";

// --- Test-side concrete stubs ---------------------------------------
//
// The core package only exposes abstract base classes; tests need
// instantiable subclasses with explicit short_ids. These match what a
// real connector's intern map would produce.

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

/**
 * Minimal `CallbackInboxStub` for codec round-trip tests. Carries
 * the `(slot, offset)` identity, stubs the rest of the abstract
 * surface (atoms, handle, cursor methods) — those are never read
 * by codec-only tests.
 */
class TestCallbackInboxStub extends CallbackInboxStub {
	constructor(
		readonly slot: number,
		readonly offset: number,
	) {
		super();
	}
	get events(): Atom<readonly never[]> {
		return atom([] as readonly never[]);
	}
	get state(): Atom<"opening" | "open" | "closed"> {
		return atom("opening" as const);
	}
	get connectionState(): Atom<"idle" | "connected" | "broken"> {
		return atom("idle" as const);
	}
	get subscriptionBreakReason(): Atom<string | null> {
		return atom(null as string | null);
	}
	get committed(): Atom<boolean> {
		return atom(false);
	}
	get handle(): CallbackHandleStub {
		return new TestCallbackHandleStub(this.slot);
	}
	sliced(_n: number): this {
		return this;
	}
}

/**
 * Minimal `CallbackHandleStub` for codec tests. Slot-only identity.
 */
class TestCallbackHandleStub extends CallbackHandleStub {
	constructor(readonly slot: number) {
		super();
	}
}

/**
 * Test stand-in for `WorkstationSession`'s `WorkstationStubSession`
 * surface — the atoms the stubs read, plus a jotai store for
 * imperative reads. Always-empty collections; sufficient for codec
 * round-trip tests.
 */
const testSession: WorkstationStubSession = {
	sessionId: "test-session",
	slotFillsAtom: atom<ReadonlyMap<number, FillRecord>>(new Map<number, FillRecord>()),
	committedSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
	brokenSlotsAtom: atom<ReadonlySet<number>>(new Set<number>()),
	inboxLogsAtom: atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>()),
	committedInboxSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
	inboxSubscriptionStateAtom: atom<ReadonlyMap<number, InboxSubscriptionState>>(
		new Map<number, InboxSubscriptionState>(),
	),
};

/**
 * Build the escape opts a real session would supply, including
 * `encodeOpResult` that dispatches on `slot === 0` vs `slot > 0`.
 */
function makeEncodeOpts(): EscapeOptions {
	return {
		encodeEntity: (s) => s.shortId,
		encodeService: (s) => s.shortId,
		encodeOpResult: (stub) => {
			if (!(stub instanceof WorkstationOpResultStub)) {
				throw new Error("test only encodes WorkstationOpResultStub");
			}
			if (stub.slot === 0) return [TAG_BROKEN_OP_RESULT, 0];
			return [TAG_OP_RESULT, stub.slot];
		},
	};
}

const encodeOpts: EscapeOptions = makeEncodeOpts();

function decodeOpts(opts?: Partial<StateUnescapeOptions>): StateUnescapeOptions {
	const entities = new Map<string, TestEntityStub>();
	const services = new Map<string, TestServiceStub>();
	const slots = new Map<number, WorkstationOpResultStub>();
	const broken = new Set<number>();
	return {
		lookupEntity: (id) => {
			let s = entities.get(id);
			if (!s) {
				s = new TestEntityStub(id);
				entities.set(id, s);
			}
			return s;
		},
		lookupService: (id) => {
			let s = services.get(id);
			if (!s) {
				s = new TestServiceStub(id);
				services.set(id, s);
			}
			return s;
		},
		lookupOpResult: (slot: number, brokenFlag: boolean) => {
			let s = slots.get(slot);
			if (!s) {
				s = new WorkstationOpResultStub(slot, testSession);
				slots.set(slot, s);
			}
			if (brokenFlag && slot > 0) broken.add(slot);
			return s;
		},
		// No interning — inbox stubs are value-typed by (slot, offset);
		// the chunk-1 contract is fresh-mint per decode (per the
		// stage-6 plan's "Don't intern inbox/handle stubs" decision).
		// The third arg (`broken`) is passed by the decoder for the
		// `broken_inbox` tag; the test helper just mints a stub at
		// the given (slot, offset) regardless, with offset=0 for
		// broken cases (the wire form carries no offset).
		lookupInbox: (slot: number, offset: number, _broken: boolean) =>
			new TestCallbackInboxStub(slot, offset),
		...opts,
	};
}

function roundTrip(value: StateValue): StateValue {
	return unescapeWire(escapeWire(value, encodeOpts), decodeOpts());
}

// --- Tests ----------------------------------------------------------

describe("escapeWire / unescapeWire: primitives", () => {
	it("passes null through", () => {
		expect(escapeWire(null, encodeOpts)).toBe(null);
		expect(roundTrip(null)).toBe(null);
	});

	it("passes booleans through", () => {
		for (const v of [true, false]) {
			expect(escapeWire(v, encodeOpts)).toBe(v);
			expect(roundTrip(v)).toBe(v);
		}
	});

	it("passes numbers through", () => {
		for (const v of [0, 1, -1, 1.5, 1e9, Number.MIN_SAFE_INTEGER]) {
			expect(escapeWire(v, encodeOpts)).toBe(v);
			expect(roundTrip(v)).toBe(v);
		}
	});

	it("passes strings through, including reserved-tag-looking ones", () => {
		for (const v of ["", "hello", "entity", "service", "bytes", "callback"]) {
			expect(escapeWire(v, encodeOpts)).toBe(v);
			expect(roundTrip(v)).toBe(v);
		}
	});
});

describe("escapeWire / unescapeWire: containers", () => {
	it("encodes objects key-by-key", () => {
		const v = { a: 1, b: "two", c: null };
		expect(escapeWire(v, encodeOpts)).toEqual({ a: 1, b: "two", c: null });
		expect(roundTrip(v)).toEqual(v);
	});

	it("encodes empty list as a single-element list-of-list", () => {
		const v: StateValue = [];
		expect(escapeWire(v, encodeOpts)).toEqual([[]]);
		expect(roundTrip(v)).toEqual([]);
	});

	it("encodes literal lists as wrapped form", () => {
		const v = [1, 2, 3];
		expect(escapeWire(v, encodeOpts)).toEqual([[1, 2, 3]]);
		expect(roundTrip(v)).toEqual(v);
	});

	it("encodes lists of strings (including strings that look like tags)", () => {
		const v = ["entity", "abc"];
		// Without escape, ["entity", "abc"] would be interpreted as a
		// tagged entity ref. The single-element wrap distinguishes it.
		expect(escapeWire(v, encodeOpts)).toEqual([["entity", "abc"]]);
		expect(roundTrip(v)).toEqual(v);
	});

	it("recurses into nested objects and lists", () => {
		const v = {
			outer: [{ inner: [1, 2] }, null],
		};
		const wire = escapeWire(v, encodeOpts);
		expect(wire).toEqual({
			outer: [[{ inner: [[1, 2]] }, null]],
		});
		expect(roundTrip(v)).toEqual(v);
	});

	it("skips explicit-undefined dict values (treats as absent)", () => {
		// `StateValue` doesn't include `undefined`, but consumer
		// code typed against narrower shapes with optional fields can
		// produce dicts with explicit undefined values via spread or
		// partial updates. The wire codec treats these as absent —
		// same as `JSON.stringify` and as never-set fields.
		const v = { a: 1, b: undefined, c: null } as unknown as StateValue;
		expect(escapeWire(v, encodeOpts)).toEqual({ a: 1, c: null });
	});

	it("skips undefined values recursively in nested dicts", () => {
		const v = {
			outer: { kept: "ok", dropped: undefined },
		} as unknown as StateValue;
		expect(escapeWire(v, encodeOpts)).toEqual({ outer: { kept: "ok" } });
	});
});

describe("escapeWire / unescapeWire: bytes", () => {
	it("round-trips an empty Uint8Array", () => {
		const v = new Uint8Array(0);
		expect(escapeWire(v, encodeOpts)).toEqual(["bytes", ""]);
		const out = roundTrip(v);
		expect(out).toBeInstanceOf(Uint8Array);
		expect(out).toEqual(v);
	});

	it("round-trips a non-empty Uint8Array", () => {
		const v = new Uint8Array([0, 1, 2, 255, 128]);
		const wire = escapeWire(v, encodeOpts);
		expect(Array.isArray(wire)).toBe(true);
		expect((wire as readonly WireValue[])[0]).toBe("bytes");
		const out = roundTrip(v);
		expect(out).toBeInstanceOf(Uint8Array);
		expect(out).toEqual(v);
	});

	it("rejects malformed base64 on decode", () => {
		expect(() => unescapeWire(["bytes", "not!base64"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("rejects mid-string padding / bad lengths / over-padding", () => {
		// Cases pinning that the loosened validation regex (character
		// class + length gate; see escape.ts's isValidBase64) stays
		// exactly as strict as the old block-structured form.
		for (const bad of ["=AAA", "A=AA", "AAA", "AB=", "AB===", "A==="]) {
			expect(() => unescapeWire(["bytes", bad] as WireValue, decodeOpts())).toThrow(
				WireEscapeError,
			);
		}
	});

	it("round-trips multi-megabyte payloads (no regex stack overflow)", () => {
		// Regression context: the old block-structured validation
		// regex ((?:[A-Za-z0-9+/]{4})*…) overflowed CHROME's regex
		// stack on multi-MB strings — first hit by a compiled renderer
		// module riding a ["bytes", …] tag. Node's V8 configuration
		// happens to survive it, so this test can't reproduce the
		// crash itself; the strictness cases above pin the replacement
		// validation, and this one keeps the multi-MB path exercised.
		const v = new Uint8Array(4 * 1024 * 1024 + 3);
		for (let i = 0; i < v.length; i++) v[i] = (i * 31 + 7) & 0xff;
		const out = roundTrip(v);
		expect(out).toBeInstanceOf(Uint8Array);
		// Buffer compare — vitest's element-wise toEqual takes seconds
		// at this size.
		expect(Buffer.from(out as Uint8Array).equals(Buffer.from(v))).toBe(true);
	});
});

describe("escapeWire / unescapeWire: entity / service refs", () => {
	it("encodes EntityRefStub as ['entity', short_id]", () => {
		const stub = new TestEntityStub("da1b2c");
		expect(escapeWire(stub, encodeOpts)).toEqual(["entity", "da1b2c"]);
	});

	it("decodes ['entity', short_id] via lookupEntity", () => {
		const out = unescapeWire(["entity", "da1b2c"], decodeOpts());
		expect(out).toBeInstanceOf(EntityRefStub);
		expect((out as EntityRefStub).shortId).toBe("da1b2c");
	});

	it("decodes a bare ['entity', X] with no provenance arg", () => {
		const seen: Array<[string, string | undefined]> = [];
		unescapeWire(["entity", "da1b2c"], {
			...decodeOpts(),
			lookupEntity: (id, prov) => {
				seen.push([id, prov]);
				return new TestEntityStub(id);
			},
		});
		expect(seen).toEqual([["da1b2c", undefined]]);
	});

	it("decodes the layered ['entity', data, prov] with both args", () => {
		const seen: Array<[string, string | undefined]> = [];
		unescapeWire(["entity", "ddata", "nprov"], {
			...decodeOpts(),
			lookupEntity: (id, prov) => {
				seen.push([id, prov]);
				return new TestEntityStub(id);
			},
		});
		expect(seen).toEqual([["ddata", "nprov"]]);
	});

	it("rejects a malformed entity tag (bad arity / non-string element)", () => {
		for (const bad of [
			["entity"],
			["entity", 7],
			["entity", "d", 7],
			["entity", "d", "n", "extra"],
		]) {
			expect(() => unescapeWire(bad as WireValue, decodeOpts())).toThrow(WireEscapeError);
		}
	});

	it("encodes ServiceRefStub as ['service', short_id]", () => {
		const stub = new TestServiceStub("s7");
		expect(escapeWire(stub, encodeOpts)).toEqual(["service", "s7"]);
	});

	it("decodes ['service', short_id] via lookupService", () => {
		const out = unescapeWire(["service", "s7"], decodeOpts());
		expect(out).toBeInstanceOf(ServiceRefStub);
		expect((out as ServiceRefStub).shortId).toBe("s7");
	});

	it("round-trips stubs nested in containers", () => {
		const e = new TestEntityStub("da1b2c");
		const s = new TestServiceStub("s7");
		const v = { hits: [e, e], lookup: s };
		const wire = escapeWire(v, encodeOpts);
		expect(wire).toEqual({
			hits: [
				[
					["entity", "da1b2c"],
					["entity", "da1b2c"],
				],
			],
			lookup: ["service", "s7"],
		});
		// Round-trip via fresh decode opts; instance identity isn't
		// preserved (the lookup callbacks are responsible for
		// interning), but short_ids are.
		const out = roundTrip(v) as Record<string, StateValue>;
		expect(((out.hits as StateValue[])[0] as EntityRefStub).shortId).toBe("da1b2c");
		expect((out.lookup as ServiceRefStub).shortId).toBe("s7");
	});
});

describe("escapeWire / unescapeWire: resolved-mode service descriptors", () => {
	// Resolved mode (the HTTP connector's server wire): a service ref
	// encodes to `["service", <descriptor object>]` rather than a
	// short_id string, and decodes via `lookupServiceDescriptor`. Entity
	// refs stay string payloads in both modes, so they're unaffected.
	const descriptor = { kind: "ws", url: "ws://h/ws", name: "sched" };
	const resolvedEntityLookup = (id: string) => new TestEntityStub(id);

	it("encodeService may return a descriptor object", () => {
		const opts: EscapeOptions = {
			encodeEntity: (s) => s.shortId,
			encodeService: () => descriptor,
		};
		expect(escapeWire(new TestServiceStub("ignored"), opts)).toEqual(["service", descriptor]);
	});

	it("decodes ['service', <descriptor>] via lookupServiceDescriptor", () => {
		let seen: WireValue | undefined;
		const out = unescapeWire(["service", descriptor], {
			lookupEntity: resolvedEntityLookup,
			lookupServiceDescriptor: (d) => {
				seen = d;
				return new TestServiceStub("s-from-descriptor");
			},
		});
		expect(out).toBeInstanceOf(ServiceRefStub);
		expect((out as ServiceRefStub).shortId).toBe("s-from-descriptor");
		expect(seen).toEqual(descriptor);
	});

	it("throws on a descriptor payload when lookupServiceDescriptor is absent", () => {
		expect(() =>
			unescapeWire(["service", descriptor], {
				lookupEntity: resolvedEntityLookup,
				lookupService: (id) => new TestServiceStub(id),
			}),
		).toThrow(WireEscapeError);
	});

	it("throws on a string payload when lookupService is absent", () => {
		expect(() =>
			unescapeWire(["service", "s7"], {
				lookupEntity: resolvedEntityLookup,
				lookupServiceDescriptor: () => new TestServiceStub("x"),
			}),
		).toThrow(WireEscapeError);
	});

	it("throws on a malformed (non-string, non-object) service payload", () => {
		expect(() =>
			unescapeWire(["service", 123] as WireValue, {
				lookupEntity: resolvedEntityLookup,
				lookupServiceDescriptor: () => new TestServiceStub("x"),
			}),
		).toThrow(WireEscapeError);
	});
});

describe("unescapeWire: callback tag (send-only; decode always rejects)", () => {
	it("rejects well-formed ['callback', N] — never received from the server", () => {
		expect(() => unescapeWire(["callback", 3], decodeOpts())).toThrow(WireEscapeError);
	});

	it("rejects malformed callback tag", () => {
		expect(() => unescapeWire(["callback", "3"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
		expect(() => unescapeWire(["callback", 1.5] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});
});

describe("escapeWire / unescapeWire: inbox tag", () => {
	const inboxEncodeOpts: EscapeOptions = {
		...encodeOpts,
		encodeInbox: (stub) => {
			if (!(stub instanceof TestCallbackInboxStub)) {
				throw new Error("test only encodes TestCallbackInboxStub");
			}
			return ["inbox", stub.slot, stub.offset];
		},
	};

	it("encodes a CallbackInboxStub as ['inbox', slot, offset]", () => {
		const stub = new TestCallbackInboxStub(5, 3);
		expect(escapeWire(stub, inboxEncodeOpts)).toEqual(["inbox", 5, 3]);
	});

	it("decodes ['inbox', slot, offset] via lookupInbox", () => {
		const out = unescapeWire(["inbox", 5, 3], decodeOpts());
		expect(out).toBeInstanceOf(TestCallbackInboxStub);
		const inbox = out as unknown as TestCallbackInboxStub;
		expect(inbox.slot).toBe(5);
		expect(inbox.offset).toBe(3);
	});

	it("rejects malformed inbox tag (missing offset)", () => {
		expect(() => unescapeWire(["inbox", 5] as WireValue, decodeOpts())).toThrow(WireEscapeError);
	});

	it("rejects malformed inbox tag (non-integer slot)", () => {
		expect(() => unescapeWire(["inbox", "5", 0] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
		expect(() => unescapeWire(["inbox", 1.5, 0] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("rejects malformed inbox tag (non-integer offset)", () => {
		expect(() => unescapeWire(["inbox", 5, "0"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
		expect(() => unescapeWire(["inbox", 5, 1.5] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("rejects ['inbox', ...] when lookupInbox is not supplied", () => {
		// Result-mode opts (no lookupOpResult / lookupInbox) — the
		// codec should pick the ResultUnescapeOptions overload and
		// throw at runtime on encountering an inbox tag.
		expect(() =>
			unescapeWire(["inbox", 5, 3], {
				lookupEntity: () => new TestEntityStub("x"),
				lookupService: () => new TestServiceStub("x"),
			}),
		).toThrow(WireEscapeError);
	});

	it("encodes the dead-inbox-sentinel (slot=0) as ['broken_inbox', 0]", () => {
		// The session's `encodeInbox` impl emits broken_inbox for
		// slot=0. The test stub directly emits inbox; the encode-side
		// behavior is exercised separately via the workstation
		// session. Here we just verify the codec's decode path.
		// Decode `["broken_inbox", 0]` calls lookupInbox(0, 0, true).
		const out = unescapeWire(["broken_inbox", 0], decodeOpts());
		expect(out).toBeInstanceOf(TestCallbackInboxStub);
		const inbox = out as unknown as TestCallbackInboxStub;
		expect(inbox.slot).toBe(0);
		expect(inbox.offset).toBe(0);
	});

	it("rejects malformed broken_inbox tag (missing slot)", () => {
		expect(() => unescapeWire(["broken_inbox"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("rejects malformed broken_inbox tag (non-integer slot)", () => {
		expect(() => unescapeWire(["broken_inbox", "0"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
		expect(() => unescapeWire(["broken_inbox", 1.5] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("rejects broken_inbox with negative slot", () => {
		expect(() => unescapeWire(["broken_inbox", -1] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("rejects ['broken_inbox', ...] when lookupInbox is not supplied", () => {
		expect(() =>
			unescapeWire(["broken_inbox", 0], {
				lookupEntity: () => new TestEntityStub("x"),
				lookupService: () => new TestServiceStub("x"),
			}),
		).toThrow(WireEscapeError);
	});
});

describe("escapeWire / unescapeWire: callback-handle in invoke args", () => {
	const handleEncodeOpts: EscapeOptions = {
		...encodeOpts,
		encodeCallbackHandle: (stub) => {
			if (!(stub instanceof TestCallbackHandleStub)) {
				throw new Error("test only encodes TestCallbackHandleStub");
			}
			return ["callback", stub.slot];
		},
	};

	it("encodes a CallbackHandleStub as ['callback', slot]", () => {
		const handle = new TestCallbackHandleStub(7);
		expect(escapeWire(handle, handleEncodeOpts)).toEqual(["callback", 7]);
	});

	it("escapeWire throws when encodeCallbackHandle is not supplied", () => {
		const handle = new TestCallbackHandleStub(7);
		expect(() => escapeWire(handle, encodeOpts)).toThrow(WireEscapeError);
	});

	it("escapeInvokeArgs accepts CallbackHandleStub at any depth", () => {
		const handle = new TestCallbackHandleStub(2);
		const out = escapeInvokeArgs(
			[handle, { wrapped: handle }, [handle]],
			{ kw: handle },
			handleEncodeOpts,
		);
		expect(out.args).toEqual([["callback", 2], { wrapped: ["callback", 2] }, [[["callback", 2]]]]);
		expect(out.kwargs).toEqual({ kw: ["callback", 2] });
	});
});

describe("escapeWire / unescapeWire: op_result tag", () => {
	it("encodes WorkstationOpResultStub (slot > 0) as ['op_result', slot]", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		expect(escapeWire(stub, encodeOpts)).toEqual(["op_result", 7]);
	});

	it("encodes the dead-sentinel (slot=0) as ['broken_op_result', 0]", () => {
		const sentinel = new WorkstationOpResultStub(0, testSession);
		expect(escapeWire(sentinel, encodeOpts)).toEqual(["broken_op_result", 0]);
	});

	it("decodes ['op_result', slot] via lookupOpResult", () => {
		const out = unescapeWire(["op_result", 7], decodeOpts());
		expect(out).toBeInstanceOf(WorkstationOpResultStub);
		expect((out as WorkstationOpResultStub).slot).toBe(7);
	});

	it("decode dedups: two ['op_result', N] tags resolve to the same stub", () => {
		const opts = decodeOpts();
		const a = unescapeWire(["op_result", 9], opts);
		const b = unescapeWire(["op_result", 9], opts);
		expect(a).toBe(b);
	});

	it("rejects ['op_result', N] when lookupOpResult is not supplied", () => {
		expect(() =>
			unescapeWire(["op_result", 3], {
				lookupEntity: () => new TestEntityStub("x"),
				lookupService: () => new TestServiceStub("x"),
			}),
		).toThrow(WireEscapeError);
	});

	it("rejects ['op_result', 0] (slot 0 is reserved for the dead-sentinel)", () => {
		expect(() => unescapeWire(["op_result", 0], decodeOpts())).toThrow(WireEscapeError);
	});

	it("rejects ['op_result', N] with non-integer slot", () => {
		expect(() => unescapeWire(["op_result", 1.5] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
		expect(() => unescapeWire(["op_result", "3"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("decodes ['broken_op_result', 0] (dead-sentinel) via lookupOpResult", () => {
		const out = unescapeWire(["broken_op_result", 0], decodeOpts());
		expect(out).toBeInstanceOf(WorkstationOpResultStub);
		expect((out as WorkstationOpResultStub).slot).toBe(0);
	});

	it("decodes ['broken_op_result', N] (slot > 0) into the same intern slot", () => {
		const opts = decodeOpts();
		const broken = unescapeWire(["broken_op_result", 4], opts);
		const normal = unescapeWire(["op_result", 4], opts);
		// Both resolve to the same interned stub — brokenness is a
		// side effect on the session, not a different instance.
		expect(broken).toBe(normal);
	});

	it("rejects ['broken_op_result', N] with negative slot", () => {
		expect(() => unescapeWire(["broken_op_result", -1], decodeOpts())).toThrow(WireEscapeError);
	});

	it("rejects ['broken_op_result', N] with non-integer slot", () => {
		expect(() => unescapeWire(["broken_op_result", 1.5] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("round-trips slot stubs nested in containers", () => {
		const a = new WorkstationOpResultStub(11, testSession);
		const b = new WorkstationOpResultStub(0, testSession);
		const v = { active: [a, a], dead: b };
		const wire = escapeWire(v, encodeOpts);
		expect(wire).toEqual({
			active: [
				[
					["op_result", 11],
					["op_result", 11],
				],
			],
			dead: ["broken_op_result", 0],
		});
		const out = roundTrip(v) as Record<string, StateValue>;
		const active = out.active as StateValue[];
		const dead = out.dead as WorkstationOpResultStub;
		expect((active[0] as WorkstationOpResultStub).slot).toBe(11);
		expect((active[1] as WorkstationOpResultStub).slot).toBe(11);
		expect(dead.slot).toBe(0);
	});
});

describe("WorkstationOpResultStub atom behavior", () => {
	it("dead-sentinel (slot=0) result is permanently errored", () => {
		const store = createStore();
		const sentinel = new WorkstationOpResultStub(0, testSession);
		const status = store.get(sentinel.result) as ResultStatus<unknown>;
		expect(status.status).toBe("errored");
		expect(store.get(sentinel.done)).toBe(true);
		expect(store.get(sentinel.committed)).toBe(false);
	});

	it("regular stub starts pending and tracks fill / broken changes", async () => {
		const store = createStore();
		const fills = atom<ReadonlyMap<number, FillRecord>>(new Map<number, FillRecord>());
		const committed = atom<ReadonlySet<number>>(new Set<number>());
		const broken = atom<ReadonlySet<number>>(new Set<number>());
		const session: WorkstationStubSession = {
			sessionId: "atom-test",
			slotFillsAtom: atom((get) => get(fills)),
			committedSlotSetAtom: atom((get) => get(committed)),
			brokenSlotsAtom: atom((get) => get(broken)),
			inboxLogsAtom: atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>()),
			committedInboxSlotSetAtom: atom<ReadonlySet<number>>(new Set<number>()),
			inboxSubscriptionStateAtom: atom<ReadonlyMap<number, InboxSubscriptionState>>(
				new Map<number, InboxSubscriptionState>(),
			),
		};
		const stub = new WorkstationOpResultStub<number>(5, session);

		expect((store.get(stub.result) as ResultStatus<number>).status).toBe("pending");
		expect(store.get(stub.done)).toBe(false);
		expect(store.get(stub.committed)).toBe(false);

		store.set(committed, new Set<number>([5]));
		expect(store.get(stub.committed)).toBe(true);

		store.set(fills, new Map<number, FillRecord>([[5, { kind: "filled", seq: 42, value: 17 }]]));
		const filled = store.get(stub.result) as ResultStatus<number>;
		expect(filled.status).toBe("filled");
		if (filled.status === "filled") expect(filled.value).toBe(17);
		expect(store.get(stub.done)).toBe(true);

		store.set(broken, new Set<number>([5]));
		const errored = store.get(stub.result) as ResultStatus<number>;
		expect(errored.status).toBe("errored");
	});
});

describe("escapeInvokeArgs", () => {
	// `TestCallbackInboxStub` and `TestCallbackHandleStub` are defined
	// at module scope (top of file).

	it("encodes empty args + no kwargs", () => {
		const out = escapeInvokeArgs([], undefined, encodeOpts);
		expect(out.args).toEqual([]);
		expect(out.kwargs).toEqual({});
	});

	it("encodes primitive args", () => {
		const out = escapeInvokeArgs([1, "x", null, true], undefined, encodeOpts);
		expect(out.args).toEqual([1, "x", null, true]);
		expect(out.kwargs).toEqual({});
	});

	it("encodes container args via escapeWire", () => {
		const out = escapeInvokeArgs([[1, 2], { k: [true] }], undefined, encodeOpts);
		// Literal lists wrap as [[...]], objects walk by key.
		expect(out.args).toEqual([[[1, 2]], { k: [[true]] }]);
	});

	it("encodes entity / service refs as tagged forms", () => {
		const e = new TestEntityStub("e1");
		const s = new TestServiceStub("s1");
		const out = escapeInvokeArgs([e, s], undefined, encodeOpts);
		expect(out.args).toEqual([
			["entity", "e1"],
			["service", "s1"],
		]);
	});

	it("encodes bytes args as ['bytes', b64]", () => {
		const out = escapeInvokeArgs([new Uint8Array([1, 2, 3])], undefined, encodeOpts);
		expect(out.args).toHaveLength(1);
		const tagged = out.args[0] as readonly WireValue[];
		expect(tagged[0]).toBe("bytes");
		expect(typeof tagged[1]).toBe("string");
	});

	it("encodes refs nested in containers", () => {
		const e = new TestEntityStub("e1");
		const s = new TestServiceStub("s1");
		const out = escapeInvokeArgs([{ entity: e, items: [e, s] }], undefined, encodeOpts);
		expect(out.args).toEqual([
			{
				entity: ["entity", "e1"],
				items: [
					[
						["entity", "e1"],
						["service", "s1"],
					],
				],
			},
		]);
	});

	it("encodes kwargs values", () => {
		const e = new TestEntityStub("e1");
		const out = escapeInvokeArgs([], { count: 3, ref: e, list: [1, 2] }, encodeOpts);
		expect(out.args).toEqual([]);
		expect(out.kwargs).toEqual({
			count: 3,
			ref: ["entity", "e1"],
			list: [[1, 2]],
		});
	});

	it("encodes args + kwargs together", () => {
		const e = new TestEntityStub("e1");
		const out = escapeInvokeArgs([1, "two"], { name: "alice", ref: e }, encodeOpts);
		expect(out.args).toEqual([1, "two"]);
		expect(out.kwargs).toEqual({ name: "alice", ref: ["entity", "e1"] });
	});

	it("omitted kwargs produces an empty dict", () => {
		const out = escapeInvokeArgs([1], undefined, encodeOpts);
		expect(out.kwargs).toEqual({});
	});

	it("empty kwargs object produces an empty dict", () => {
		const out = escapeInvokeArgs([1], {}, encodeOpts);
		expect(out.kwargs).toEqual({});
	});

	it("rejects OpResultStub at top level of args", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([stub], undefined, encodeOpts)).toThrow(WireEscapeError);
	});

	it("rejects OpResultStub nested in a list arg", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([[1, stub, 2]], undefined, encodeOpts)).toThrow(WireEscapeError);
	});

	it("rejects OpResultStub nested in a dict arg", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([{ inner: stub }], undefined, encodeOpts)).toThrow(
			WireEscapeError,
		);
	});

	it("rejects OpResultStub in kwargs (top-level and nested)", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([], { r: stub }, encodeOpts)).toThrow(WireEscapeError);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([], { r: { nested: stub } }, encodeOpts)).toThrow(
			WireEscapeError,
		);
	});

	it("rejects CallbackInboxStub at top level of args", () => {
		const cb = new TestCallbackInboxStub(7, 0);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([cb], undefined, encodeOpts)).toThrow(WireEscapeError);
	});

	it("rejects CallbackInboxStub nested in a list / dict / kwarg", () => {
		const cb = new TestCallbackInboxStub(7, 0);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([[cb]], undefined, encodeOpts)).toThrow(WireEscapeError);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([{ inner: cb }], undefined, encodeOpts)).toThrow(WireEscapeError);
		// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
		expect(() => escapeInvokeArgs([], { cb }, encodeOpts)).toThrow(WireEscapeError);
	});

	it("rejection points at the path in the error message (positional)", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		try {
			// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
			escapeInvokeArgs([1, { deep: [stub] }], undefined, encodeOpts);
			throw new Error("expected throw");
		} catch (err) {
			expect(err).toBeInstanceOf(WireEscapeError);
			expect((err as Error).message).toContain("args[1].deep[0]");
		}
	});

	it("rejection points at the path in the error message (kwargs)", () => {
		const stub = new WorkstationOpResultStub(7, testSession);
		try {
			// @ts-expect-error -- defense-in-depth: stub is forbidden in InvokeArgValue
			escapeInvokeArgs([], { outer: { inner: stub } }, encodeOpts);
			throw new Error("expected throw");
		} catch (err) {
			expect(err).toBeInstanceOf(WireEscapeError);
			expect((err as Error).message).toContain("kwargs.outer.inner");
		}
	});
});

describe("escapeWire / unescapeWire: error paths", () => {
	it("encode rejects an OpResultStub when encodeOpResult is not supplied", () => {
		const stub = new WorkstationOpResultStub(3, testSession);
		const optsWithoutOpResult: EscapeOptions = {
			encodeEntity: (s) => s.shortId,
			encodeService: (s) => s.shortId,
		};
		expect(() => escapeWire(stub, optsWithoutOpResult)).toThrow(WireEscapeError);
	});

	it("decode rejects an unknown tag", () => {
		expect(() => unescapeWire(["unknown_tag", "x"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("decode rejects malformed entity tag (wrong arity)", () => {
		// 1-element is too short; 4-element is too long. (A 3-element tag is
		// the valid layered form `["entity", data, prov]`.)
		expect(() => unescapeWire(["entity"] as WireValue, decodeOpts())).toThrow(WireEscapeError);
		expect(() => unescapeWire(["entity", "x", "p", "extra"] as WireValue, decodeOpts())).toThrow(
			WireEscapeError,
		);
	});

	it("decode rejects a malformed list (not single-element-of-list)", () => {
		expect(() => unescapeWire([1, 2, 3] as WireValue, decodeOpts())).toThrow(WireEscapeError);
		expect(() => unescapeWire([] as WireValue, decodeOpts())).toThrow(WireEscapeError);
	});
});
