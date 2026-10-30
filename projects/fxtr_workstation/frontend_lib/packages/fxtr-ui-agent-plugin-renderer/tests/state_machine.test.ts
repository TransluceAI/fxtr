/**
 * State-machine tests for the agent-plugin panel.
 *
 * The driver atom is mounted by subscribing to it via
 * ``store.sub(...)`` (mirrors how the real panel mounts it via
 * ``useAtomValue(driverAtom)`` in React). We then flip the
 * controllable mock stubs and assert the panel state transitions
 * + the right allocate calls were observed.
 */

import type { EntityRefStub } from "@fxtr-ui/core";
import { createStore } from "jotai";
import { describe, expect, it } from "vitest";

import type { AgentMcpRunningSessionProto } from "../src/proto.js";
import {
	INITIAL_PANEL_STATE,
	type PanelState,
	makeAgentPanelDriver,
} from "../src/state_machine.js";

import {
	type MockInvokeCall,
	TestCallbackInboxStub,
	TestEntityRefStub,
	TestOpResultStub,
	TestServiceRefStub,
	makeMockSessionAtom,
} from "./_mock_session.js";

function mkServiceStub() {
	return new TestServiceRefStub<AgentMcpRunningSessionProto>("agent_mcp:demo");
}

describe("agent panel state machine", () => {
	it("init: allocates inbox + invokes subscribe_to on its handle, transitions to subscribing", () => {
		const store = createStore();
		const inbox = new TestCallbackInboxStub<EntityRefStub>();
		const subscribeStub = new TestOpResultStub<null>();
		const invokeCalls: MockInvokeCall[] = [];
		const { atom: panelAtom } = makeMockSessionAtom<PanelState>({
			initial: INITIAL_PANEL_STATE,
			callbackQueue: [inbox],
			invokeQueue: [subscribeStub],
			invokeCalls,
		});
		const serviceStub = mkServiceStub();
		const driver = makeAgentPanelDriver(panelAtom, serviceStub);

		const unsub = store.sub(driver, () => {});

		const state = store.get(panelAtom);
		expect(state.phase.kind).toBe("subscribing");
		if (state.phase.kind !== "subscribing") throw new Error("unreachable");
		expect(state.phase.inbox).toBe(inbox);
		expect(state.phase.subscribe).toBe(subscribeStub);

		// Side-channel: one callback-target allocateInvoke — target
		// is the fresh inbox's own handle, the agent service rides
		// as the event-source arg.
		expect(invokeCalls).toHaveLength(1);
		const call = invokeCalls[0];
		if (call === undefined) throw new Error("unreachable");
		expect(call.stub).toBe(inbox.handle);
		expect(call.method).toBe("subscribe_to");
		expect(call.args).toHaveLength(1);
		expect(call.args[0]).toBe(serviceStub);

		unsub();
	});

	it("subscribing → running on ack; the current head arrives as the first inbox event", () => {
		const store = createStore();
		const inbox = new TestCallbackInboxStub<EntityRefStub>();
		const subscribeStub = new TestOpResultStub<null>();
		const headLoad = new TestOpResultStub();
		const loadCalls: EntityRefStub<unknown>[] = [];
		const { atom: panelAtom } = makeMockSessionAtom<PanelState>({
			initial: INITIAL_PANEL_STATE,
			callbackQueue: [inbox],
			invokeQueue: [subscribeStub],
			loadQueue: [headLoad],
			loadCalls,
		});
		const driver = makeAgentPanelDriver(panelAtom, mkServiceStub());
		const unsub = store.sub(driver, () => {});

		// init → subscribing happened. Fill the ack (subscribe_to
		// returns None); the effect transitions to running with NO
		// seeded entries.
		store.set(subscribeStub._resultPrivate, { status: "filled", value: null });
		const ranState = store.get(panelAtom);
		expect(ranState.phase.kind).toBe("running");
		expect(ranState.entries).toHaveLength(0);

		// The backend hub's deliver_current seed: the current head
		// arrives as the subscription's first event.
		const head = new TestEntityRefStub("chain-head-1");
		store.set(inbox._eventsPrivate, [head]);

		const state = store.get(panelAtom);
		expect(state.entries).toHaveLength(1);
		expect(state.entries[0]?.stub).toBe(head);
		expect(state.entries[0]?.load).toBe(headLoad);
		expect(loadCalls).toHaveLength(1);
		expect(loadCalls[0]).toBe(head);

		unsub();
	});

	it("subscribing → running on ack with no events: entries stays empty", () => {
		const store = createStore();
		const inbox = new TestCallbackInboxStub<EntityRefStub>();
		const subscribeStub = new TestOpResultStub<null>();
		const { atom: panelAtom } = makeMockSessionAtom<PanelState>({
			initial: INITIAL_PANEL_STATE,
			callbackQueue: [inbox],
			invokeQueue: [subscribeStub],
		});
		const driver = makeAgentPanelDriver(panelAtom, mkServiceStub());
		const unsub = store.sub(driver, () => {});

		store.set(subscribeStub._resultPrivate, { status: "filled", value: null });

		const state = store.get(panelAtom);
		expect(state.phase.kind).toBe("running");
		expect(state.entries).toHaveLength(0);

		unsub();
	});

	it("subscribing → errored when the subscribe_to invoke errors", () => {
		const store = createStore();
		const inbox = new TestCallbackInboxStub<EntityRefStub>();
		const subscribeStub = new TestOpResultStub<null>();
		const { atom: panelAtom } = makeMockSessionAtom<PanelState>({
			initial: INITIAL_PANEL_STATE,
			callbackQueue: [inbox],
			invokeQueue: [subscribeStub],
		});
		const driver = makeAgentPanelDriver(panelAtom, mkServiceStub());
		const unsub = store.sub(driver, () => {});

		store.set(subscribeStub._resultPrivate, {
			status: "errored",
			message: "agent_mcp service unreachable",
		});

		const state = store.get(panelAtom);
		expect(state.phase.kind).toBe("errored");
		if (state.phase.kind !== "errored") throw new Error("unreachable");
		expect(state.phase.message).toBe("agent_mcp service unreachable");

		unsub();
	});

	it("running: appends an entry per delivered inbox event + slices the inbox", () => {
		const store = createStore();
		const inbox = new TestCallbackInboxStub<EntityRefStub>();
		const subscribeStub = new TestOpResultStub<null>();
		const eventLoad1 = new TestOpResultStub();
		const eventLoad2 = new TestOpResultStub();
		const { atom: panelAtom } = makeMockSessionAtom<PanelState>({
			initial: INITIAL_PANEL_STATE,
			callbackQueue: [inbox],
			invokeQueue: [subscribeStub],
			loadQueue: [eventLoad1, eventLoad2],
		});
		const driver = makeAgentPanelDriver(panelAtom, mkServiceStub());
		const unsub = store.sub(driver, () => {});

		// Ack the subscribe; running with no entries.
		store.set(subscribeStub._resultPrivate, { status: "filled", value: null });

		// Deliver two events on the inbox (the first would be the
		// deliver_current head on a non-fresh session — the driver
		// treats every event uniformly). The driver should append
		// two entries (with the two queued load stubs) and slice the
		// inbox by 2.
		const head1 = new TestEntityRefStub("chain-head-1");
		const head2 = new TestEntityRefStub("chain-head-2");
		store.set(inbox._eventsPrivate, [head1, head2]);

		const state = store.get(panelAtom);
		expect(state.phase.kind).toBe("running");
		expect(state.entries).toHaveLength(2);
		expect(state.entries[0]?.stub).toBe(head1);
		expect(state.entries[0]?.load).toBe(eventLoad1);
		expect(state.entries[1]?.stub).toBe(head2);
		expect(state.entries[1]?.load).toBe(eventLoad2);

		// Inbox sliced past the two events.
		expect(inbox.slicedTotal).toBe(2);

		unsub();
	});

	it("running: idle when the inbox has no events", () => {
		const store = createStore();
		const inbox = new TestCallbackInboxStub<EntityRefStub>();
		const subscribeStub = new TestOpResultStub<null>();
		const { atom: panelAtom } = makeMockSessionAtom<PanelState>({
			initial: INITIAL_PANEL_STATE,
			callbackQueue: [inbox],
			invokeQueue: [subscribeStub],
		});
		const driver = makeAgentPanelDriver(panelAtom, mkServiceStub());
		const unsub = store.sub(driver, () => {});

		// Filled with null so we land in running with empty entries.
		store.set(subscribeStub._resultPrivate, { status: "filled", value: null });
		const stateAfterRunning = store.get(panelAtom);
		expect(stateAfterRunning.phase.kind).toBe("running");
		expect(stateAfterRunning.entries).toHaveLength(0);

		// No inbox events ⇒ driver shouldn't slice or write.
		expect(inbox.slicedTotal).toBe(0);

		unsub();
	});

	it("running: errored phase is terminal — driver doesn't transition further on later writes", () => {
		const store = createStore();
		const inbox = new TestCallbackInboxStub<EntityRefStub>();
		const subscribeStub = new TestOpResultStub<null>();
		const { atom: panelAtom } = makeMockSessionAtom<PanelState>({
			initial: INITIAL_PANEL_STATE,
			callbackQueue: [inbox],
			invokeQueue: [subscribeStub],
		});
		const driver = makeAgentPanelDriver(panelAtom, mkServiceStub());
		const unsub = store.sub(driver, () => {});
		store.set(subscribeStub._resultPrivate, {
			status: "errored",
			message: "boom",
		});

		// Sanity: errored.
		expect(store.get(panelAtom).phase.kind).toBe("errored");

		// Push a stray inbox event; the terminal driver should
		// ignore it (no allocate, no slice).
		const stray = new TestEntityRefStub("stray");
		store.set(inbox._eventsPrivate, [stray]);
		expect(store.get(panelAtom).phase.kind).toBe("errored");
		expect(inbox.slicedTotal).toBe(0);

		unsub();
	});
});
