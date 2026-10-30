/**
 * Tests for `stringifyForDebug` / `debugReplacer`.
 *
 * The util dispatches via duck typing on `debugSummary(): string`,
 * which the abstract stub classes in `@fxtr-ui/core` define with
 * sensible defaults. Subclasses with richer identity (e.g.
 * `WorkstationOpResultStub`'s `slot`) override. We construct tiny
 * concrete subclasses inline to test both inheritance of the
 * default and override paths — without bringing in
 * `@fxtr-ui/connector-workstation` (kept the util connector-
 * agnostic).
 */

import { describe, expect, it } from "vitest";

import {
	CallbackInboxStub,
	EntityRefStub,
	OpResultStub,
	type ResultStatus,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { type Atom, atom } from "jotai";

import { debugReplacer, stringifyForDebug } from "../src/stringify_for_debug";

class TestEntityRef extends EntityRefStub {
	#shortId: string;
	constructor(shortId: string) {
		super();
		this.#shortId = shortId;
	}
	get shortId(): string {
		return this.#shortId;
	}
}

class TestServiceRef extends ServiceRefStub {
	#shortId: string;
	constructor(shortId: string) {
		super();
		this.#shortId = shortId;
	}
	get shortId(): string {
		return this.#shortId;
	}
}

class TestOpResult extends OpResultStub {
	readonly slot: number;
	readonly #result: Atom<ResultStatus>;
	readonly #committed: Atom<boolean>;
	readonly #done: Atom<boolean>;
	constructor(slot: number) {
		super();
		this.slot = slot;
		this.#result = atom<ResultStatus>({ status: "pending" });
		this.#committed = atom(false);
		this.#done = atom(false);
	}
	get result(): Atom<ResultStatus> {
		return this.#result;
	}
	get committed(): Atom<boolean> {
		return this.#committed;
	}
	get done(): Atom<boolean> {
		return this.#done;
	}
	override debugSummary(): string {
		return `<OpResultStub slot=${this.slot}>`;
	}
}

// Minimal CallbackInboxStub subclass for instanceof / debugSummary
// tests — never accessed for its data, so the abstract members are
// stubbed with `null as never` / a self-return. See the test cases
// below; they only call `debugSummary` and `instanceof` on the value.
class TestInbox extends CallbackInboxStub {
	get offset() {
		return 0;
	}
	get events() {
		return null as never;
	}
	get state() {
		return null as never;
	}
	get connectionState() {
		return null as never;
	}
	get subscriptionBreakReason() {
		return null as never;
	}
	get committed() {
		return null as never;
	}
	get handle() {
		return null as never;
	}
	sliced() {
		return this;
	}
}

describe("stringifyForDebug: primitives and plain objects", () => {
	it("pass through unchanged", () => {
		expect(stringifyForDebug({ a: 1, b: "x", c: [1, 2] })).toBe(
			JSON.stringify({ a: 1, b: "x", c: [1, 2] }),
		);
	});

	it("supports indent", () => {
		expect(stringifyForDebug({ a: 1 }, 2)).toBe(JSON.stringify({ a: 1 }, null, 2));
	});
});

describe("stringifyForDebug: stub summaries (default + override)", () => {
	it("EntityRefStub default → '<EntityRef shortId>'", () => {
		expect(stringifyForDebug({ e: new TestEntityRef("ent-a") })).toBe(
			JSON.stringify({ e: "<EntityRef ent-a>" }),
		);
	});

	it("ServiceRefStub default → '<ServiceRef shortId>'", () => {
		expect(stringifyForDebug({ s: new TestServiceRef("svc-a") })).toBe(
			JSON.stringify({ s: "<ServiceRef svc-a>" }),
		);
	});

	it("OpResultStub abstract default → '<OpResultStub>' (no slot known)", () => {
		// A concrete subclass that doesn't override `debugSummary`
		// inherits the abstract base's default.
		class SlotlessOpResult extends OpResultStub {
			readonly #r: Atom<ResultStatus> = atom<ResultStatus>({ status: "pending" });
			readonly #c: Atom<boolean> = atom(false);
			readonly #d: Atom<boolean> = atom(false);
			get result(): Atom<ResultStatus> {
				return this.#r;
			}
			get committed(): Atom<boolean> {
				return this.#c;
			}
			get done(): Atom<boolean> {
				return this.#d;
			}
		}
		expect(stringifyForDebug({ r: new SlotlessOpResult() })).toBe(
			JSON.stringify({ r: "<OpResultStub>" }),
		);
	});

	it("OpResultStub override → connector-specific summary (e.g. slot)", () => {
		expect(stringifyForDebug({ r: new TestOpResult(5) })).toBe(
			JSON.stringify({ r: "<OpResultStub slot=5>" }),
		);
	});

	it("CallbackInboxStub default → '<CallbackInboxStub>'", () => {
		expect(stringifyForDebug({ cb: new TestInbox() })).toBe(
			JSON.stringify({ cb: "<CallbackInboxStub>" }),
		);
	});

	it("nested stubs are summarized", () => {
		const value = {
			items: [new TestEntityRef("a"), { inner: new TestOpResult(3) }],
		};
		expect(stringifyForDebug(value)).toBe(
			JSON.stringify({
				items: ["<EntityRef a>", { inner: "<OpResultStub slot=3>" }],
			}),
		);
	});
});

describe("stringifyForDebug: any object can opt in via debugSummary", () => {
	it("a plain object with debugSummary is summarized", () => {
		const obj = {
			data: 42,
			debugSummary(): string {
				return "<custom data=42>";
			},
		};
		expect(stringifyForDebug({ x: obj })).toBe(JSON.stringify({ x: "<custom data=42>" }));
	});

	it("a non-function debugSummary field is not treated as opt-in", () => {
		// Defensive: only methods (`typeof === "function"`) trigger
		// the summary path. A plain string field named `debugSummary`
		// is left alone.
		const obj = { debugSummary: "not a function", n: 1 };
		expect(stringifyForDebug({ x: obj })).toBe(
			JSON.stringify({ x: { debugSummary: "not a function", n: 1 } }),
		);
	});
});

describe("stringifyForDebug: circular structure no longer trips", () => {
	it("a stub with a self-referential field serializes without throwing", () => {
		// Hand-build the same shape `WorkstationOpResultStub` exhibits:
		// stub holds a `session` field that holds an `updateCtx` whose
		// `session` points back at the holder. Vanilla `JSON.stringify`
		// would throw `Converting circular structure to JSON`.
		const stub = new TestOpResult(7) as TestOpResult & {
			session?: { updateCtx?: { session: unknown } };
		};
		const fakeSession = { updateCtx: { session: undefined as unknown } };
		fakeSession.updateCtx.session = fakeSession;
		stub.session = fakeSession;
		// Stub is summarized → traversal never reaches the cycle.
		expect(stringifyForDebug({ r: stub })).toBe(JSON.stringify({ r: "<OpResultStub slot=7>" }));
	});
});

describe("debugReplacer exported for composition", () => {
	it("usable as a JSON.stringify replacer directly", () => {
		const out = JSON.stringify({ e: new TestEntityRef("x") }, debugReplacer);
		expect(out).toBe(JSON.stringify({ e: "<EntityRef x>" }));
	});
});
