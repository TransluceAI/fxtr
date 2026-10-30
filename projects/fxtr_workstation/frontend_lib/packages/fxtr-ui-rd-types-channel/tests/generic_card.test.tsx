/**
 * Render smoke test for `GenericChannelCard` (the
 * `ChannelContentCard.NoMatch` fallback): drive scripted event sequences
 * through mock inboxes and assert the phases render (message list →
 * final-value reflink → abort banner), plus the debug subchannel list
 * (a `SubchannelOpened` → an opened child reader rendered as a reflink).
 *
 * Mocked at the `FxtrSessionAtom` level: the mock ctx dispenses
 * controllable inbox + invoke stubs from queues, and the test pushes
 * events into the inboxes' private atoms. The card subscribes TWO feeds on
 * mount — `subscribe_full` (subscription) then `subscribe_debug_subchannels`
 * (debug) — so the queues supply two inboxes + two invokes; opening a
 * subchannel pops a third invoke. `RefLink`'s default impl is stubbed via
 * `RefLinkImplContext` to print the stub's shortId.
 *
 * @vitest-environment jsdom
 */

import { RefLinkImplContext } from "@fxtr-ui/workstation-dispatch";
import { act, cleanup, render, screen } from "@testing-library/react";
import { Provider, createStore } from "jotai";
import { afterEach, describe, expect, it } from "vitest";

import {
	type ChannelUpdateWire,
	GenericChannelCard,
	type SubchannelOpenedWire,
} from "../src/index.js";
import {
	type MockInvokeCall,
	TestCallbackInboxStub,
	TestEntityRefStub,
	TestOpResultStub,
	TestServiceRefStub,
	makeMockSessionAtom,
} from "./_mocks.js";

afterEach(cleanup);

const StubRefLinkImpl = (props: { stub: { shortId: string }; as?: "inline" | "block" }) => (
	<span>link:{props.stub.shortId}</span>
);

function setUp() {
	const store = createStore();
	const tag = new TestEntityRefStub("e-tag");
	const channel = new TestServiceRefStub("s-reader");
	const fullInbox = new TestCallbackInboxStub<ChannelUpdateWire>();
	const debugInbox = new TestCallbackInboxStub<SubchannelOpenedWire>();
	// invoke queue: subscribe_full, subscribe_debug_subchannels, then a spare
	// for an open_subchannel_reader (only popped if a subchannel is opened).
	const openInvoke = new TestOpResultStub<unknown>();
	const invokeCalls: MockInvokeCall[] = [];
	const { atom: ownStorage } = makeMockSessionAtom<unknown>({
		initial: null,
		callbackQueue: [
			fullInbox as TestCallbackInboxStub<unknown>,
			debugInbox as TestCallbackInboxStub<unknown>,
		],
		invokeQueue: [new TestOpResultStub(), new TestOpResultStub(), openInvoke],
		invokeCalls,
	});

	const view = render(
		<Provider store={store}>
			<RefLinkImplContext.Provider value={StubRefLinkImpl}>
				<GenericChannelCard stub={tag} channel={channel} ownStorage={ownStorage} />
			</RefLinkImplContext.Provider>
		</Provider>,
	);

	const pushFull = (...events: ChannelUpdateWire[]) => {
		act(() => {
			store.set(fullInbox._eventsPrivate, [...store.get(fullInbox._eventsPrivate), ...events]);
		});
	};
	const pushDebug = (...events: SubchannelOpenedWire[]) => {
		act(() => {
			store.set(debugInbox._eventsPrivate, [...store.get(debugInbox._eventsPrivate), ...events]);
		});
	};

	return {
		view,
		store,
		tag,
		channel,
		fullInbox,
		debugInbox,
		openInvoke,
		invokeCalls,
		pushFull,
		pushDebug,
	};
}

describe("GenericChannelCard", () => {
	it("subscribes both feeds on mount (subscribe_full + subscribe_debug_subchannels)", () => {
		const { channel, fullInbox, debugInbox, invokeCalls } = setUp();
		expect(invokeCalls).toHaveLength(2);
		expect(invokeCalls[0]).toMatchObject({
			method: "subscribe_to",
			target: fullInbox.handle,
			args: [channel, "subscribe_full"],
		});
		expect(invokeCalls[1]).toMatchObject({
			method: "subscribe_to",
			target: debugInbox.handle,
			args: [channel, "subscribe_debug_subchannels"],
		});
	});

	it("renders the phases: connecting → open values → complete result", () => {
		const { pushFull } = setUp();

		expect(screen.getByText(/waiting for the channel snapshot/)).toBeTruthy();
		expect(screen.getByText("link:e-tag")).toBeTruthy();

		// Open: snapshot values + a batched update render as message rows;
		// ref-valued messages render as reflinks.
		const messageRef = new TestEntityRefStub("e-msg");
		pushFull(
			{ kind: "snapshot_open", status: "running", values: ["hello-value"] },
			{ kind: "update", events: [messageRef], status: "running", replace_history: false },
		);
		expect(screen.getByText(/hello-value/)).toBeTruthy();
		expect(screen.getByText("link:e-msg")).toBeTruthy();
		expect(screen.getByText(/2 messages/)).toBeTruthy();

		// Complete: the message rows drop, the result reflink shows.
		const resultRef = new TestEntityRefStub("e-result");
		pushFull({ kind: "complete", result: resultRef, status: "done" });
		expect(screen.getByText("link:e-result")).toBeTruthy();
		expect(screen.queryByText(/hello-value/)).toBeNull();
		expect(screen.queryByText("link:e-msg")).toBeNull();
		expect(screen.getByText("complete")).toBeTruthy();
	});

	it("replace_history resets the visible message log", () => {
		const { pushFull } = setUp();
		pushFull(
			{ kind: "snapshot_open", status: "running", values: ["old-a", "old-b"] },
			{ kind: "update", events: ["fresh"], status: "running", replace_history: true },
		);
		expect(screen.getByText(/fresh/)).toBeTruthy();
		expect(screen.queryByText(/old-a/)).toBeNull();
		expect(screen.getByText(/1 message/)).toBeTruthy();
	});

	it("renders a plain (non-ref) final value through the json tree", () => {
		const { pushFull } = setUp();
		pushFull(
			{ kind: "snapshot_open", status: "running", values: [] },
			{ kind: "complete", result: { n_ok: 3 }, status: "done" },
		);
		expect(screen.getByText(/n_ok/)).toBeTruthy();
	});

	it("renders the abort banner with the reason", () => {
		const { pushFull } = setUp();
		pushFull(
			{ kind: "snapshot_open", status: "running", values: ["x"] },
			{ kind: "abort", status: "running", reason: "scope closed before channel finished" },
		);
		expect(screen.getByText(/channel aborted: scope closed before channel finished/)).toBeTruthy();
		expect(screen.queryByText(/x/)).toBeNull();
	});

	it("lists a debug subchannel as an opened child reader reflink (while open)", () => {
		const { store, openInvoke, invokeCalls, channel, pushFull, pushDebug } = setUp();
		// Must be open for the subchannel section to render.
		pushFull({ kind: "snapshot_open", status: "running", values: [] });
		// A subchannel announcement on the debug feed → a DebugSubchannelLink
		// mounts and issues open_subchannel_reader(key) (popping `openInvoke`).
		pushDebug({ kind: "subchannel_opened", key: "cell-1", name: "cell" });
		expect(screen.getByText("subchannels")).toBeTruthy();
		expect(invokeCalls).toHaveLength(3);
		expect(invokeCalls[2]).toMatchObject({
			method: "open_subchannel_reader",
			target: channel,
			args: ["cell-1"],
		});
		// Fill the open invoke with the child reader → its reflink renders
		// (RefLink routes the child reader stub through the stub impl).
		const childReader = new TestServiceRefStub("s-child");
		act(() => {
			store.set(openInvoke._resultPrivate, { status: "filled", value: childReader });
		});
		expect(screen.getByText("link:s-child")).toBeTruthy();
	});
});
