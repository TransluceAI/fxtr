/**
 * Renderer for the `stopwatch` WS service type — a FullPanel that
 * subscribes to a server-side stopwatch (the backend notifies an
 * incrementing count every second) and shows the latest tick.
 *
 * Demonstrates a WS subscription from inside a dynamically-loaded
 * renderer: `ctx.allocateCallback()` mints a client-local inbox and
 * `ctx.allocateInvoke(inbox.handle, "subscribe_to", [serviceStub])`
 * desugars (on the HTTP connector) to a `subscribe` invoke that hands
 * the inbox to the service; the server's notifies arrive as ws frames
 * and append to `inbox.events`. Dropping the inbox from state
 * unsubscribes.
 */

import type {
	CallbackInboxStub,
	FxtrSessionAtom,
	OpResultStub,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { FullPanel } from "@fxtr-ui/workstation-dispatch";
import { useAtom, useAtomValue } from "jotai";

// Both slots must be stored: the inbox creates the callback, and the
// `subscribe_to` invoke slot must be referenced from state or the
// reducer's flush walk drops it (allocate-and-store contract) and never
// sends the subscribe. The subscribe result only anchors the slot.
type SwState = {
	readonly inbox: CallbackInboxStub<number>;
	readonly subscribe: OpResultStub<null>;
} | null;

interface ServiceRendererProps {
	readonly stub: ServiceRefStub;
	readonly ownStorage: FxtrSessionAtom<unknown>;
}

function StopwatchPanel({ stub, ownStorage }: ServiceRendererProps) {
	const [state, setState] = useAtom(ownStorage as FxtrSessionAtom<SwState>);

	const subscribe = (): void => {
		setState((_prev, ctx) => {
			const inbox = ctx.allocateCallback<number>();
			// `subscribe_to` desugars to a `subscribe` invoke handing the
			// inbox to the stopwatch service; ticks arrive on `inbox.events`.
			const subscribe = ctx.allocateInvoke<null>(inbox.handle, "subscribe_to", [stub]);
			return { inbox, subscribe };
		});
	};

	const unsubscribe = (): void => {
		// Dropping the inbox stub from state closes the subscription
		// (the connector fires the best-effort unsubscribe).
		setState(() => null);
	};

	return (
		<div style={panelStyle}>
			<h2 style={{ margin: 0, fontSize: 18 }}>stopwatch service</h2>
			<p style={mutedStyle}>
				A WS service: on subscribe, the backend pushes an incrementing count once a second over
				the multiplexed ws connection.
			</p>
			{state === null ? (
				<button type="button" onClick={subscribe} style={btnStyle}>
					Subscribe
				</button>
			) : (
				<Ticker inbox={state.inbox} onUnsubscribe={unsubscribe} />
			)}
		</div>
	);
}

function Ticker({
	inbox,
	onUnsubscribe,
}: {
	readonly inbox: CallbackInboxStub<number>;
	readonly onUnsubscribe: () => void;
}) {
	const events = useAtomValue(inbox.events) as readonly number[];
	const connectionState = useAtomValue(inbox.connectionState);
	const latest = events.length > 0 ? events[events.length - 1] : null;

	return (
		<div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
			<div style={bigNumberStyle}>{latest ?? "—"}</div>
			<div style={mutedStyle}>
				{events.length} tick{events.length === 1 ? "" : "s"} · connection: {connectionState}
			</div>
			<button type="button" onClick={onUnsubscribe} style={btnStyle}>
				Unsubscribe
			</button>
		</div>
	);
}

const panelStyle = {
	padding: 16,
	height: "100%",
	overflow: "auto",
	display: "flex",
	flexDirection: "column" as const,
	gap: 12,
	color: "#234",
};
const mutedStyle = { margin: 0, fontSize: 12, color: "#789", lineHeight: 1.5 };
const btnStyle = {
	alignSelf: "flex-start" as const,
	padding: "4px 12px",
	border: "1px solid #58a",
	borderRadius: 6,
	background: "#eaf2fb",
	color: "#136",
	cursor: "pointer",
	fontSize: 13,
};
const bigNumberStyle = {
	fontSize: 56,
	fontWeight: 700,
	fontFamily: "ui-monospace, monospace",
	color: "#136",
	lineHeight: 1,
};

export default {
	default: {
		[FullPanel.uuid]: StopwatchPanel,
	},
};
