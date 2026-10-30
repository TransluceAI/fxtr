import { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { atom, createStore } from "jotai";
import { beforeEach, describe, expect, it } from "vitest";

import {
	type FillRecord,
	HttpCallbackInboxStub,
	HttpEntityRefStub,
	HttpOpResultStub,
	HttpServiceRefStub,
	type HttpStubSession,
	type InboxLog,
	type InboxSubscriptionState,
} from "../src/stubs";

/** A mutable fake `HttpStubSession` whose atoms tests can write to. */
function makeFakeSession() {
	const slotFills = atom<ReadonlyMap<number, FillRecord>>(new Map<number, FillRecord>());
	const committedSlots = atom<ReadonlySet<number>>(new Set<number>());
	const brokenSlots = atom<ReadonlySet<number>>(new Set<number>());
	const inboxLogs = atom<ReadonlyMap<number, InboxLog>>(new Map<number, InboxLog>());
	const committedInboxSlots = atom<ReadonlySet<number>>(new Set<number>());
	const inboxSubs = atom<ReadonlyMap<number, InboxSubscriptionState>>(
		new Map<number, InboxSubscriptionState>(),
	);
	const session: HttpStubSession = {
		sessionId: "s-test",
		slotFillsAtom: slotFills,
		committedSlotSetAtom: committedSlots,
		brokenSlotsAtom: brokenSlots,
		inboxLogsAtom: inboxLogs,
		committedInboxSlotSetAtom: committedInboxSlots,
		inboxSubscriptionStateAtom: inboxSubs,
	};
	return {
		session,
		slotFills,
		committedSlots,
		brokenSlots,
		inboxLogs,
		committedInboxSlots,
		inboxSubs,
	};
}

let store: ReturnType<typeof createStore>;
beforeEach(() => {
	store = createStore();
});

describe("HttpEntityRefStub / HttpServiceRefStub", () => {
	it("entity ref exposes its shortId", () => {
		const e = new HttpEntityRefStub("dabc");
		expect(e).toBeInstanceOf(EntityRefStub);
		expect(e.shortId).toBe("dabc");
	});

	it("service ref state derives from the connector broken-services set", () => {
		const broken = atom<ReadonlySet<string>>(new Set<string>());
		const s = new HttpServiceRefStub("s1", broken);
		expect(s).toBeInstanceOf(ServiceRefStub);
		expect(store.get(s.state)).toBe("alive");
		store.set(broken, new Set(["s1"]));
		expect(store.get(s.state)).toBe("broken");
	});
});

describe("HttpOpResultStub", () => {
	it("reflects pending → filled and committed", () => {
		const f = makeFakeSession();
		const stub = new HttpOpResultStub(5, f.session);
		expect(store.get(stub.result)).toEqual({ status: "pending" });
		expect(store.get(stub.done)).toBe(false);
		store.set(f.slotFills, new Map([[5, { kind: "filled", seq: 1, value: "hi" }]]));
		expect(store.get(stub.result)).toEqual({ status: "filled", value: "hi" });
		expect(store.get(stub.done)).toBe(true);
		expect(store.get(stub.committed)).toBe(false);
		store.set(f.committedSlots, new Set([5]));
		expect(store.get(stub.committed)).toBe(true);
	});

	it("broken slot errors regardless of fills", () => {
		const f = makeFakeSession();
		const stub = new HttpOpResultStub(7, f.session);
		store.set(f.brokenSlots, new Set([7]));
		expect(store.get(stub.result).status).toBe("errored");
	});

	it("slot 0 is a permanent dead sentinel", () => {
		const f = makeFakeSession();
		const stub = new HttpOpResultStub(0, f.session);
		expect(store.get(stub.result).status).toBe("errored");
		expect(store.get(stub.done)).toBe(true);
	});
});

describe("HttpCallbackInboxStub", () => {
	it("slices events from its offset and reflects open + connectionState", () => {
		const f = makeFakeSession();
		const inbox = new HttpCallbackInboxStub(3, 0, f.session);
		store.set(
			f.inboxLogs,
			new Map([
				[
					3,
					{
						firstIndex: 0,
						events: [
							{ seq: 1, payload: "a" },
							{ seq: 2, payload: "b" },
						],
					},
				],
			]),
		);
		expect(store.get(inbox.events)).toEqual(["a", "b"]);
		expect(store.get(inbox.state)).toBe("opening");
		store.set(f.committedInboxSlots, new Set([3]));
		expect(store.get(inbox.state)).toBe("open");

		// sliced cursor skips consumed events.
		const sliced = inbox.sliced(1) as HttpCallbackInboxStub;
		expect(store.get(sliced.events)).toEqual(["b"]);

		// synthesized connection state.
		expect(store.get(inbox.connectionState)).toBe("idle");
		store.set(f.inboxSubs, new Map([[3, { state: "broken", reason: "ws dropped" }]]));
		expect(store.get(inbox.connectionState)).toBe("broken");
		expect(store.get(inbox.subscriptionBreakReason)).toBe("ws dropped");
	});

	it("handle is the slot-matched send side", () => {
		const f = makeFakeSession();
		const inbox = new HttpCallbackInboxStub(9, 0, f.session);
		const handle = inbox.handle as unknown as { slot: number };
		expect(handle.slot).toBe(9);
		expect(inbox.handle).toBe(inbox.handle); // memoized
	});
});
