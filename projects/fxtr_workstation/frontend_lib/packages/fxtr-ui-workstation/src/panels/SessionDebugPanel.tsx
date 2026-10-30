/**
 * `SessionDebugPanel` — dockview content renderer for the
 * per-session debug surface.
 *
 * Stateless w.r.t. its own session: attaches to the target
 * session via `connector.attachSession` for the panel's
 * lifetime (used only to observe lifecycle + call `terminate()`)
 * and opens two raw passthrough subscriptions for the event log
 * + debug snapshot.
 *
 * Five sections, top-to-bottom:
 *
 *   1. Header — sessionId, seq / ack_seq / slot_counter, lifecycle
 *      badge, "Force terminate" button.
 *   2. Last state — collapsible JSON tree of `last_state.state`
 *      with `EntityRefStub` / `ServiceRefStub` leaves rendered
 *      as `<DebugRefChip>` (no dispatch — avoids tracking refs
 *      in the dispatcher session).
 *   3. Session pins — entities + services pinned by this session,
 *      cross-referenced from the connector's resource lists, with
 *      refcount badges from the debug snapshot.
 *   4. Open slots — three tables (callbacks / invokes / loads)
 *      driven by the debug snapshot.
 *   5. Event log — chronological list of events with three-way
 *      coloring (unacked / past-horizon / preserved).
 *
 * Doesn't provide `PanelContext`. `DebugRefChip` instances inside
 * route through `restoreIntoNewPanel` on click, so the debug
 * panel never mutates a source panel's chain.
 */

import type {
	EntityRecord,
	NonStateSessionEvent,
	ServiceRecord,
	SessionDebugSnapshotEvent,
	SessionEvent,
	StateUpdateEvent,
	WorkstationSession,
} from "@fxtr-ui/connector-workstation";
import { DebugRefChip } from "@fxtr-ui/panel-viewer";
import { JsonRefTree, type RefLeafRenderer } from "@fxtr-ui/workstation-dispatch";
import type { IDockviewPanelProps } from "dockview";
import { useAtomValue } from "jotai";
import { type CSSProperties, useEffect, useState } from "react";
import { useWorkstationConnector } from "../useWorkstationConnector.js";
import type { SessionDebugPanelParams } from "./SessionDebugPanelParams.js";
import {
	chipStyle,
	headerStyle,
	mutedStyle,
	rowKeyStyle,
	tableStyle,
	thStyle,
} from "./tables/table_styles.js";

interface StreamSnapshot {
	readonly last_state: StateUpdateEvent;
	readonly events: readonly NonStateSessionEvent[];
}

const DEBUG_REF_RENDERER: RefLeafRenderer = (stub) => (
	<DebugRefChip entry={{ stub, display: "default" }} />
);

export function SessionDebugPanel(props: IDockviewPanelProps<SessionDebugPanelParams>) {
	const { sessionId } = props.params;
	const connector = useWorkstationConnector();

	// Attach a `WorkstationSession` for the panel's lifetime — used
	// for `terminate()` and observing `lifecycle`. Detach on unmount.
	const [session, setSession] = useState<WorkstationSession | null>(null);
	useEffect(() => {
		const s = connector.attachSession(sessionId);
		setSession(s);
		return () => {
			setSession(null);
			s.detach().catch(() => {});
		};
	}, [connector, sessionId]);

	// Raw session event stream — feeds the last-state display + event
	// log. Folds snapshot + subsequent events into a single
	// `{last_state, events}` view, mirroring the debug UI's shape.
	// Goes through `connector.connection.registerSubscription`
	// directly: the connector doesn't wrap session-event /
	// session-debug subscriptions (they'd be pure passthroughs);
	// debug surfaces consume the wire-level subscription directly.
	const [stream, setStream] = useState<StreamSnapshot | null>(null);
	useEffect(() => {
		setStream(null);
		const subscription = connector.connection.registerSubscription(
			{ kind: "session", session_id: sessionId },
			{
				onFrame: (frame) => {
					if (frame.type === "session_snapshot") {
						setStream({
							last_state: frame.last_state,
							events: [...frame.preserved_events, ...frame.subsequent_events],
						});
					} else if (frame.type === "session_event") {
						setStream((prev) => {
							if (prev === null) return prev;
							const ev = frame.event;
							if (ev.kind === "state_update") {
								return { last_state: ev, events: prev.events };
							}
							return { last_state: prev.last_state, events: [...prev.events, ev] };
						});
					}
				},
				onClose: () => {},
			},
		);
		return () => subscription.close();
	}, [connector, sessionId]);

	// Debug snapshot — refcounts / open slots / preserved-event
	// seqs. Re-pushed by the server on every relevant change.
	const [debug, setDebug] = useState<SessionDebugSnapshotEvent | null>(null);
	useEffect(() => {
		setDebug(null);
		const subscription = connector.connection.registerSubscription(
			{ kind: "session_debug", session_id: sessionId },
			{
				onFrame: (frame) => {
					if (frame.type === "session_debug_snapshot") {
						setDebug(frame);
					}
				},
				onClose: () => {},
			},
		);
		return () => subscription.close();
	}, [connector, sessionId]);

	const services = useAtomValue(connector.listServices({ debug: true }).records);
	const entities = useAtomValue(connector.listEntities({ debug: true }).records);

	const handleTerminate = () => {
		if (session === null) return;
		session.terminate().catch((err: unknown) => {
			console.error("[fxtr-ui] session terminate failed", err);
		});
	};

	return (
		<div className="fw-panel" style={bodyStyle}>
			<Header
				sessionId={sessionId}
				session={session}
				stream={stream}
				debug={debug}
				onTerminate={handleTerminate}
			/>
			{stream === null ? (
				<div style={mutedStyle}>loading…</div>
			) : (
				<>
					<LastStateSection lastState={stream.last_state} />
					<PinsSection
						sessionId={sessionId}
						lastState={stream.last_state}
						debug={debug}
						services={services}
						entities={entities}
					/>
					<OpenSlotsSection debug={debug} />
					<EventLogSection stream={stream} debug={debug} />
				</>
			)}
		</div>
	);
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

function Header({
	sessionId,
	session,
	stream,
	debug,
	onTerminate,
}: {
	readonly sessionId: string;
	readonly session: WorkstationSession | null;
	readonly stream: StreamSnapshot | null;
	readonly debug: SessionDebugSnapshotEvent | null;
	readonly onTerminate: () => void;
}) {
	const lifecycle = useAtomValue(session?.lifecycle ?? PLACEHOLDER_LIFECYCLE_ATOM);
	const ls = stream?.last_state;
	return (
		<header style={headerRowStyle}>
			<div style={headerLineStyle}>
				<strong>session</strong>
				<code style={sessionIdStyle} title={sessionId}>
					{sessionId}
				</code>
				<span style={lifecycleBadgeStyle}>{lifecycle}</span>
			</div>
			<div style={headerStatsStyle}>
				{ls !== undefined ? (
					<>
						<span>
							seq=<code>{ls.seq}</code>
						</span>
						<span>
							ack_seq=<code>{ls.ack_seq}</code>
						</span>
						<span>
							slot_counter=<code>{debug?.slot_counter ?? ls.slot_counter}</code>
						</span>
						{ls.closed && <span style={closedBadgeStyle}>closed</span>}
					</>
				) : (
					<span style={mutedStyle}>—</span>
				)}
				<button
					type="button"
					style={terminateButtonStyle}
					onClick={onTerminate}
					disabled={session === null || lifecycle === "closed"}
					title="Force-terminate the session — no confirmation"
				>
					Force terminate
				</button>
			</div>
		</header>
	);
}

// Stable placeholder so the lifecycle hook can be called
// unconditionally before the session has been attached.
import { atom as createAtom } from "jotai";
const PLACEHOLDER_LIFECYCLE_ATOM = createAtom<"connecting">("connecting");

// ---------------------------------------------------------------------------
// Last state
// ---------------------------------------------------------------------------

function LastStateSection({ lastState }: { readonly lastState: StateUpdateEvent }) {
	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>Last state</header>
			<div style={sectionBodyStyle}>
				<JsonRefTree value={lastState.state} renderRef={DEBUG_REF_RENDERER} />
			</div>
		</section>
	);
}

// ---------------------------------------------------------------------------
// Pins
// ---------------------------------------------------------------------------

function PinsSection({
	sessionId,
	lastState,
	debug,
	services,
	entities,
}: {
	readonly sessionId: string;
	readonly lastState: StateUpdateEvent;
	readonly debug: SessionDebugSnapshotEvent | null;
	readonly services: ReadonlyArray<ServiceRecord>;
	readonly entities: ReadonlyArray<EntityRecord>;
}) {
	const explicitEntities = new Set(lastState.tracked_entities);
	const explicitServices = new Set(lastState.tracked_services);

	const pinnedEntities = entities.filter((e) => (e.pinned_by_sessions ?? []).includes(sessionId));
	const pinnedServices = services.filter((s) => (s.pinned_by_sessions ?? []).includes(sessionId));

	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>Session pins</header>
			<div style={sectionBodyStyle}>
				<PinTable
					kind="entity"
					rows={pinnedEntities.map((e) => ({
						shortId: e.short_id,
						extra: e.fxtr_id,
						refcount: debug?.entity_refcounts[e.short_id] ?? null,
						source: explicitEntities.has(e.short_id) ? "explicit pin" : "retained-event only",
					}))}
					extraLabel="fxtr_id"
				/>
				<PinTable
					kind="service"
					rows={pinnedServices.map((s) => ({
						shortId: s.short_id,
						extra: s.service_type_id,
						refcount: debug?.service_refcounts[s.short_id] ?? null,
						source: explicitServices.has(s.short_id) ? "explicit pin" : "retained-event only",
					}))}
					extraLabel="type id"
				/>
			</div>
		</section>
	);
}

function PinTable({
	kind,
	rows,
	extraLabel,
}: {
	readonly kind: "entity" | "service";
	readonly rows: ReadonlyArray<{
		shortId: string;
		extra: string;
		refcount: number | null;
		source: string;
	}>;
	readonly extraLabel: string;
}) {
	const connector = useWorkstationConnector();
	return (
		<div style={pinTableWrap}>
			<div style={subHeaderStyle}>
				{kind === "entity" ? "entities" : "services"} ({rows.length})
			</div>
			{rows.length === 0 ? (
				<div style={mutedStyle}>no pins</div>
			) : (
				<table style={tableStyle}>
					<thead>
						<tr>
							<th style={thStyle}>short_id</th>
							<th style={thStyle}>{extraLabel}</th>
							<th style={thStyle}>refcount</th>
							<th style={thStyle}>source</th>
						</tr>
					</thead>
					<tbody>
						{rows.map((row) => (
							<tr key={row.shortId}>
								<td style={rowKeyStyle}>
									<DebugRefChip
										entry={{
											stub:
												kind === "entity"
													? connector.entityRef(row.shortId)
													: connector.serviceRef(row.shortId),
											display: "default",
										}}
									/>
								</td>
								<td style={extraCellStyle} title={row.extra}>
									{row.extra}
								</td>
								<td style={tdStyle}>
									<code>{row.refcount ?? "?"}</code>
								</td>
								<td style={tdStyle}>{row.source}</td>
							</tr>
						))}
					</tbody>
				</table>
			)}
		</div>
	);
}

// ---------------------------------------------------------------------------
// Open slots
// ---------------------------------------------------------------------------

function OpenSlotsSection({ debug }: { readonly debug: SessionDebugSnapshotEvent | null }) {
	const callbacks = Object.entries(debug?.open_callbacks ?? {}).sort(
		(a, b) => Number(a[0]) - Number(b[0]),
	);
	const invokes = Object.entries(debug?.open_invokes ?? {}).sort(
		(a, b) => Number(a[0]) - Number(b[0]),
	);
	const loads = Object.entries(debug?.open_loads ?? {}).sort((a, b) => Number(a[0]) - Number(b[0]));

	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>Open slots</header>
			<div style={sectionBodyStyle}>
				<SlotTable
					title="callbacks"
					cols={["slot", "service"]}
					rows={callbacks.map(([slot, shortId]) => [slot, { kind: "service" as const, shortId }])}
				/>
				<SlotTable
					title="invokes"
					cols={["slot", "service", "method"]}
					rows={invokes.map(([slot, info]) => [
						slot,
						{ kind: "service" as const, shortId: info.service_short_id },
						info.method_name,
					])}
				/>
				<SlotTable
					title="loads"
					cols={["slot", "entity"]}
					rows={loads.map(([slot, shortId]) => [slot, { kind: "entity" as const, shortId }])}
				/>
			</div>
		</section>
	);
}

type SlotCell = string | { kind: "entity" | "service"; shortId: string };

function SlotTable({
	title,
	cols,
	rows,
}: {
	readonly title: string;
	readonly cols: readonly string[];
	readonly rows: ReadonlyArray<readonly SlotCell[]>;
}) {
	const connector = useWorkstationConnector();
	return (
		<div style={pinTableWrap}>
			<div style={subHeaderStyle}>
				{title} ({rows.length})
			</div>
			{rows.length === 0 ? (
				<div style={mutedStyle}>none open</div>
			) : (
				<table style={tableStyle}>
					<thead>
						<tr>
							{cols.map((c) => (
								<th key={c} style={thStyle}>
									{c}
								</th>
							))}
						</tr>
					</thead>
					<tbody>
						{rows.map((row, i) => (
							// biome-ignore lint/suspicious/noArrayIndexKey: slot index is the natural key
							<tr key={i}>
								{row.map((cell, j) => (
									<td
										// biome-ignore lint/suspicious/noArrayIndexKey: column index is the natural key
										key={j}
										style={tdStyle}
									>
										{typeof cell === "string" ? (
											<code>{cell}</code>
										) : (
											<DebugRefChip
												entry={{
													stub:
														cell.kind === "entity"
															? connector.entityRef(cell.shortId)
															: connector.serviceRef(cell.shortId),
													display: "default",
												}}
											/>
										)}
									</td>
								))}
							</tr>
						))}
					</tbody>
				</table>
			)}
		</div>
	);
}

// ---------------------------------------------------------------------------
// Event log
// ---------------------------------------------------------------------------

function EventLogSection({
	stream,
	debug,
}: {
	readonly stream: StreamSnapshot;
	readonly debug: SessionDebugSnapshotEvent | null;
}) {
	const ackHorizon = stream.last_state.ack_seq;
	const preservedSeqs = new Set(debug?.preserved_events ?? []);
	// Sort by seq ascending; show all events.
	const events = [...stream.events].sort((a, b) => a.seq - b.seq);

	return (
		<section style={sectionStyle}>
			<header style={headerStyle}>Event log</header>
			<div style={sectionBodyStyle}>
				{events.length === 0 ? (
					<div style={mutedStyle}>no events yet</div>
				) : (
					<ol style={eventListStyle}>
						{events.map((ev) => (
							<EventRow
								key={ev.seq}
								event={ev}
								ackHorizon={ackHorizon}
								preservedSeqs={preservedSeqs}
							/>
						))}
					</ol>
				)}
			</div>
		</section>
	);
}

function EventRow({
	event,
	ackHorizon,
	preservedSeqs,
}: {
	readonly event: SessionEvent;
	readonly ackHorizon: number;
	readonly preservedSeqs: ReadonlySet<number>;
}) {
	const isPreserved = preservedSeqs.has(event.seq);
	const isPastHorizon = event.seq <= ackHorizon && !isPreserved;
	// Past-horizon and preserved both color the row; preserved
	// takes precedence (a preserved event is past-horizon by
	// construction but we want to surface it as alive, not stale).
	const rowColor = isPreserved
		? "var(--accent-warning, #c60)"
		: isPastHorizon
			? "var(--text-muted)"
			: undefined;
	const style: CSSProperties = { ...eventRowStyle, color: rowColor };
	return (
		<li style={style}>
			{event.kind === "state_update" ? (
				<StateUpdateRow event={event} />
			) : (
				<SingleEventRow event={event} />
			)}
			{isPreserved && <PreservedChip />}
		</li>
	);
}

function StateUpdateRow({ event }: { readonly event: StateUpdateEvent }) {
	const [open, setOpen] = useState(false);
	return (
		<>
			<span style={seqStyle}>
				seq={event.seq} ack={event.ack_seq}
			</span>{" "}
			<span
				style={triggerStyle}
				role="button"
				tabIndex={0}
				onClick={() => setOpen((v) => !v)}
				onKeyDown={(e) => {
					if (e.key === "Enter" || e.key === " ") {
						e.preventDefault();
						setOpen((v) => !v);
					}
				}}
			>
				{open ? "▾" : "▸"} state_update
			</span>
			{event.closed && <span style={mutedInline}> (closed)</span>}
			{open && (
				<div style={expandedBodyStyle}>
					<div style={fieldRowStyle}>
						<span style={fieldLabelStyle}>tracked_entities:</span>{" "}
						<code style={monoInlineStyle}>[{event.tracked_entities.join(", ")}]</code>
					</div>
					<div style={fieldRowStyle}>
						<span style={fieldLabelStyle}>tracked_services:</span>{" "}
						<code style={monoInlineStyle}>[{event.tracked_services.join(", ")}]</code>
					</div>
					<div style={fieldRowStyle}>
						<span style={fieldLabelStyle}>state:</span>
						<div style={treeBlockStyle}>
							<JsonRefTree value={event.state} renderRef={DEBUG_REF_RENDERER} />
						</div>
					</div>
				</div>
			)}
		</>
	);
}

function SingleEventRow({
	event,
}: {
	readonly event: Exclude<SessionEvent, StateUpdateEvent>;
}) {
	const [open, setOpen] = useState(false);
	const isError =
		event.kind === "load_error" ||
		event.kind === "invoke_error" ||
		event.kind === "entity_metadata_error" ||
		event.kind === "service_metadata_error";
	// LoadResultSilent has no body — flat row, no expand triangle.
	const isSilent = event.kind === "load_result_silent";

	if (isSilent) {
		return (
			<>
				<span style={seqStyle}>seq={event.seq}</span> <span style={kindStyle}>{event.kind}</span>{" "}
				<span style={mutedInline}>slot={event.slot}</span>
			</>
		);
	}

	return (
		<>
			<span style={seqStyle}>seq={event.seq}</span>{" "}
			<span
				style={triggerStyle}
				role="button"
				tabIndex={0}
				onClick={() => setOpen((v) => !v)}
				onKeyDown={(e) => {
					if (e.key === "Enter" || e.key === " ") {
						e.preventDefault();
						setOpen((v) => !v);
					}
				}}
			>
				{open ? "▾" : "▸"} <span style={isError ? errorKindStyle : kindStyle}>{event.kind}</span>
			</span>{" "}
			<span style={mutedInline}>slot={event.slot}</span>
			{open && (
				<div style={expandedBodyStyle}>
					<EventBodyDetail event={event} />
				</div>
			)}
		</>
	);
}

function EventBodyDetail({
	event,
}: {
	readonly event: Exclude<SessionEvent, StateUpdateEvent>;
}) {
	if ("payload" in event) {
		return (
			<div style={fieldRowStyle}>
				<span style={fieldLabelStyle}>payload:</span>
				<div style={treeBlockStyle}>
					<JsonRefTree value={event.payload} renderRef={DEBUG_REF_RENDERER} />
				</div>
			</div>
		);
	}
	if ("metadata" in event) {
		return (
			<div style={fieldRowStyle}>
				<span style={fieldLabelStyle}>metadata:</span>
				<div style={treeBlockStyle}>
					<JsonRefTree value={event.metadata as unknown} renderRef={DEBUG_REF_RENDERER} />
				</div>
			</div>
		);
	}
	if ("message" in event) {
		return (
			<div style={fieldRowStyle}>
				<span style={fieldLabelStyle}>message:</span>{" "}
				<code style={monoInlineStyle}>{event.message}</code>
			</div>
		);
	}
	return null;
}

function PreservedChip() {
	return <span style={preservedChipStyle}>preserved</span>;
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const bodyStyle: CSSProperties = {
	height: "100%",
	overflow: "auto",
	padding: "var(--sp-4)",
	fontFamily: "var(--font-prose)",
	color: "var(--text-primary)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-4, 16px)",
};

const headerRowStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
	background: "var(--surface-sunken)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
};

const headerLineStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
};

const headerStatsStyle: CSSProperties = {
	display: "flex",
	flexWrap: "wrap",
	alignItems: "center",
	gap: "var(--sp-3, 12px)",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
};

const sessionIdStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
};

const lifecycleBadgeStyle: CSSProperties = {
	padding: "0 6px",
	borderRadius: "var(--radius-badge)",
	background: "var(--surface-raised)",
	border: "1px solid var(--border-subtle)",
	color: "var(--text-secondary)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-caption)",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
};

const closedBadgeStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontWeight: 600,
};

const terminateButtonStyle: CSSProperties = {
	marginLeft: "auto",
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--status-error, #c00)",
	borderRadius: "var(--radius-badge)",
	background: "transparent",
	color: "var(--status-error, #c00)",
	cursor: "pointer",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	fontWeight: 600,
};

const sectionStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
};

const sectionBodyStyle: CSSProperties = {
	padding: "var(--sp-3, 12px)",
	background: "var(--surface-sunken)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
};

const pinTableWrap: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
};

const subHeaderStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "var(--text-caption)",
	fontFamily: "var(--font-prose)",
};

const tdStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	verticalAlign: "top",
};

const extraCellStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	verticalAlign: "top",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-secondary)",
	maxWidth: "320px",
	overflow: "hidden",
	textOverflow: "ellipsis",
	whiteSpace: "nowrap",
};

const eventListStyle: CSSProperties = {
	listStyle: "none",
	margin: 0,
	padding: 0,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
};

// Plain `<li>` row, no flex — content uses inline-block / inline so
// the seq, triangle, kind, slot, and chip flow on one line; the
// expanded body opens as a block below via `<div>`. Mirrors the
// debug UI's event-log row shape.
const eventRowStyle: CSSProperties = {
	padding: "var(--sp-1, 2px) var(--sp-2, 6px)",
	borderBottom: "1px dotted var(--border-subtle)",
	fontSize: "var(--text-body-sm)",
	lineHeight: 1.5,
};

const seqStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	color: "var(--text-accent, #06c)",
};

const kindStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
};

const errorKindStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	color: "var(--status-error, #c00)",
};

const triggerStyle: CSSProperties = {
	cursor: "pointer",
	userSelect: "none",
};

const mutedInline: CSSProperties = {
	color: "var(--text-muted)",
	fontFamily: "var(--font-mono)",
};

const monoInlineStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
};

const expandedBodyStyle: CSSProperties = {
	marginLeft: "16px",
	marginTop: "4px",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
};

const fieldRowStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "2px",
};

const fieldLabelStyle: CSSProperties = {
	fontFamily: "var(--font-prose)",
	color: "var(--text-secondary)",
};

const treeBlockStyle: CSSProperties = {
	marginLeft: "8px",
};

const preservedChipStyle: CSSProperties = {
	...chipStyle,
	marginLeft: "8px",
	background: "rgba(255, 153, 0, 0.15)",
	color: "rgb(180, 90, 0)",
	border: "1px solid rgba(255, 153, 0, 0.4)",
};
