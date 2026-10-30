/**
 * ``AgentMcpFullPanel`` — the ``FullPanel`` renderer for the
 * ``agent_mcp_running_session`` service type.
 *
 * The panel:
 *
 * 1. Lifts its ``ownStorage`` (a ``FxtrSessionAtom<unknown>``
 *    threaded in by the workstation shell) to a typed
 *    ``FxtrSessionAtom<PanelState>`` via ``atomWithDefault``.
 * 2. Builds the state-machine driver atom (see
 *    :mod:`./state_machine`) and subscribes to it to mount the
 *    effect.
 * 3. Renders the panel chrome:
 *    - ``errored`` phase → an error card.
 *    - ``init`` / ``subscribing`` phase → a small "subscribing…"
 *      card.
 *    - ``running`` phase → the scrolling stream + the
 *      ``<SendBox>`` at the bottom.
 *
 * The stream renders each entry as a row with a timestamp
 * prefix + the per-event-type renderer dispatched through the
 * ``StreamEntry`` key. Gaps between adjacent entries (when the
 * watcher skipped intermediate heads) surface as a "Load missing
 * entries" affordance above the entry below the gap.
 */

import type { FxtrSessionAtom, ServiceRefStub } from "@fxtr-ui/core";
import { StreamEntry } from "@fxtr-ui/rd-types-stream";
import { type DispatchedComponent, RenderDispatch } from "@fxtr-ui/react-dispatch";
import { atomWithDefault } from "@fxtr-ui/state-util";
import type { FullPanelProps } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import type { CSSProperties } from "react";
import { useMemo } from "react";

import type { ChainNodeValue } from "./data_shapes.js";
import { formatTimestamp, mutedStyle } from "./entries/shared.js";
import { LoadMoreLink } from "./load_more.js";
import type { AgentMcpRunningSessionProto } from "./proto.js";
import { SendBox } from "./send_box/index.js";
import {
	type ChainEntry,
	INITIAL_PANEL_STATE,
	type PanelState,
	makeAgentPanelDriver,
} from "./state_machine.js";

const panelStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	height: "100%",
	background: "var(--surface-base)",
};

const streamScrollStyle: CSSProperties = {
	flex: 1,
	overflowY: "auto",
	padding: "var(--sp-3) var(--sp-4)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3)",
};

const stateCardStyle: CSSProperties = {
	padding: "var(--sp-4)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
	background: "var(--surface-raised)",
	color: "var(--text-muted)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
};

const errorCardStyle: CSSProperties = {
	...stateCardStyle,
	color: "var(--text-danger, #b3261e)",
	borderColor: "var(--text-danger, #b3261e)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};

const entryRowStyle: CSSProperties = {
	display: "flex",
	gap: "var(--sp-3)",
	alignItems: "flex-start",
};

const timestampStyle: CSSProperties = {
	flex: "0 0 auto",
	width: "5.5em",
	paddingTop: "var(--sp-2)",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
};

const entryBodyStyle: CSSProperties = {
	flex: 1,
	minWidth: 0,
};

/**
 * One row in the stream. Reads the entry's chain-node load
 * reactively; while pending shows a tiny placeholder, when filled
 * dispatches the ``event`` ref through the ``StreamEntry`` key.
 */
function StreamEntryRow(props: {
	entry: ChainEntry;
}) {
	const result = useAtomValue(props.entry.load.result);
	if (result.status === "pending") {
		return (
			<div style={entryRowStyle}>
				<div style={timestampStyle}>—</div>
				<div style={{ ...entryBodyStyle, ...mutedStyle }}>loading…</div>
			</div>
		);
	}
	if (result.status === "errored") {
		return (
			<div style={entryRowStyle}>
				<div style={timestampStyle}>—</div>
				<div style={{ ...entryBodyStyle, color: "var(--text-danger, #b3261e)" }}>
					chain entry failed to load: {result.message}
				</div>
			</div>
		);
	}
	const value = result.value as ChainNodeValue;
	return (
		<div style={entryRowStyle}>
			<div style={timestampStyle}>{formatTimestamp(value.timestamp)}</div>
			<div style={entryBodyStyle}>
				<RenderDispatch as={StreamEntry} stub={value.event} />
			</div>
		</div>
	);
}

/**
 * Render the entries list, interleaving "Load missing entries"
 * affordances where adjacent entries have non-matching
 * ``prev`` / ``stub`` shortIds.
 *
 * Gap detection runs per render: for each entry at index ``i ≥ 1``,
 * if the entry's load has filled and its ``prev`` is non-null and
 * ``prev.shortId !== entries[i-1].stub.shortId``, the gap-fill
 * affordance appears above this entry.
 */
function EntriesStream(props: {
	panelStateAtom: FxtrSessionAtom<PanelState>;
	entries: ReadonlyArray<ChainEntry>;
}) {
	return (
		<>
			{props.entries.map((entry, i) => (
				<EntryRowWithGap
					key={entry.stub.shortId}
					panelStateAtom={props.panelStateAtom}
					entries={props.entries}
					index={i}
					entry={entry}
				/>
			))}
		</>
	);
}

/**
 * Pair of (optional gap link + entry row) for one entry.
 *
 * Reading the entry's load result lives here (rather than in
 * ``StreamEntryRow`` alone) so the gap-detection branch can
 * inspect the loaded ``prev`` field; the row's render then uses
 * the same result.
 *
 * Gap detection covers two cases:
 *
 * - **Index ≥ 1**: this entry's ``prev`` should be the previous
 *   entry's stub. If they don't match (the watcher skipped one or
 *   more intermediate heads), surface the affordance.
 * - **Index 0**: there's no previous entry in the list, but the
 *   first entry's ``prev`` may still be non-null after a
 *   reconnect (we loaded the chain head from persistence; older
 *   entries are unloaded). Surface the affordance so the operator
 *   can walk back into history.
 */
function EntryRowWithGap(props: {
	panelStateAtom: FxtrSessionAtom<PanelState>;
	entries: ReadonlyArray<ChainEntry>;
	index: number;
	entry: ChainEntry;
}) {
	const result = useAtomValue(props.entry.load.result);
	const value = result.status === "filled" ? (result.value as ChainNodeValue) : null;

	let showGap = false;
	if (value !== null && value.prev !== null) {
		if (props.index === 0) {
			// No previous entry in the list, but `prev` points at
			// older history (typically: just reconnected and the
			// chain head was loaded from persistence).
			showGap = true;
		} else {
			const previousStub = props.entries[props.index - 1]?.stub;
			if (previousStub !== undefined && previousStub.shortId !== value.prev.shortId) {
				showGap = true;
			}
		}
	}

	const status = (
		result.status === "filled" ? "filled" : result.status === "errored" ? "errored" : "pending"
	) as "pending" | "filled" | "errored";

	return (
		<>
			{showGap ? (
				<LoadMoreLink
					panelStateAtom={props.panelStateAtom}
					belowLoad={props.entry.load}
					belowLoadStatus={status}
					belowLoadValue={value}
					belowIndex={props.index}
				/>
			) : null}
			<StreamEntryRow entry={props.entry} />
		</>
	);
}

/**
 * The ``FullPanel`` renderer for the agent-MCP service. Memoizes
 * the panel-state atom + driver atom over ``[ownStorage, stub]``
 * so identity is stable across renders; ``ownStorage`` only
 * changes when the workstation shell mounts a different panel
 * session, and ``stub`` only changes if the chain attaches to a
 * different agent service.
 *
 * ``useAtomValue(driverAtom)`` mounts the state-machine effect;
 * the returned ``void`` is discarded.
 */
export const AgentMcpFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const { panelStateAtom, driverAtom } = useMemo(() => {
		const stateAtom: FxtrSessionAtom<PanelState> = atomWithDefault(
			ownStorage as FxtrSessionAtom<PanelState | null>,
			INITIAL_PANEL_STATE,
		);
		const typedStub = stub as ServiceRefStub<AgentMcpRunningSessionProto>;
		return {
			panelStateAtom: stateAtom,
			driverAtom: makeAgentPanelDriver(stateAtom, typedStub),
		};
	}, [ownStorage, stub]);

	useAtomValue(driverAtom);
	const state = useAtomValue(panelStateAtom);

	if (state.phase.kind === "errored") {
		return (
			<div style={panelStyle}>
				<div style={errorCardStyle}>agent_mcp subscription failed: {state.phase.message}</div>
			</div>
		);
	}

	if (state.phase.kind === "init" || state.phase.kind === "subscribing") {
		return (
			<div style={panelStyle}>
				<div style={stateCardStyle}>subscribing to the agent's event log…</div>
			</div>
		);
	}

	// running
	const typedStub = stub as ServiceRefStub<AgentMcpRunningSessionProto>;
	return (
		<div style={panelStyle}>
			<div style={streamScrollStyle}>
				{state.entries.length === 0 ? (
					<div style={stateCardStyle}>
						subscribed. nothing's happened yet — send a message below to start the conversation, or
						wait for the agent to extend the chain.
					</div>
				) : (
					<EntriesStream panelStateAtom={panelStateAtom} entries={state.entries} />
				)}
			</div>
			<SendBox panelStateAtom={panelStateAtom} serviceStub={typedStub} />
		</div>
	);
};
