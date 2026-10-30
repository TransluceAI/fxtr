/**
 * Renderers for the `simple_scheduler` demo — two halves:
 *
 * **The scheduler-service FullPanel is thin.** It subscribes to the
 * scheduler's event stream on mount (`ctx.allocateCallback()` + a
 * same-tick callback-target `allocateInvoke(inbox.handle,
 * "subscribe_to", [schedulerRef])`); the stream carries `run_channel`
 * announcements (the seed is the current/most-recent run's) and
 * `scheduler_closing`. Each `run_channel` carries the run's channel
 * **reader** ref. The panel keeps the latest announcement and
 * re-dispatches that reader stub as a nested `FullPanel` — which routes
 * through `@fxtr-ui/rd-types-channel`'s `ChannelReaderView` (get_tag() →
 * tag) into the `ChannelContentCard` dispatched on the tag's type, i.e.
 * the grid card below. Per-channel panel storage is carved by the
 * reader's shortId so a new run never inherits the previous channel's
 * persisted subscription slots.
 *
 * **The grid is a `ChannelContentCard` renderer for the
 * `scheduler_run_tag` type** — the demo of overriding the generic
 * channel card. The dispatched `stub` is the run tag (run_id /
 * step_labels / input_names — the static metadata); it subscribes to
 * the channel via `useChannelSubscription` (the reader's
 * `subscribe_full`) and folds the `run_inputs` + `task_*` messages into
 * the live grid. Step columns are labelled from the tag's `step_labels`
 * (channel messages are data-only, so the task service refs don't ride
 * the stream); the input column reflinks the `run_inputs` entity ids.
 * On `complete` the message log is gone by design — the card shows the
 * durable `SimpleRunResult` reflink (which has its own grid renderer).
 * On `aborted` it shows the reason banner.
 */

import {
	type CallbackInboxStub,
	EntityRefStub,
	type FxtrSessionAtom,
	type OpResultStub,
	type ResultValue,
	type ServiceRefStub,
} from "@fxtr-ui/core";
import {
	ChannelContentCard,
	type ChannelContentCardProps,
	type ChannelStateEventWire,
	type ChannelViewState,
	useChannelStateSubscription,
	useChannelSubscription,
} from "@fxtr-ui/rd-types-channel";
import { type DispatchedComponent, RenderDispatch } from "@fxtr-ui/react-dispatch";
import { type LoadResult, atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
	RefLink,
} from "@fxtr-ui/workstation-dispatch";
import { atom, useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, useEffect, useMemo } from "react";

// ----- scheduler-level event shapes (structural; wire-decoded) -----

interface SchedulerAnnouncement {
	readonly kind: string;
	readonly run_id?: string;
	readonly reader?: ServiceRefStub;
}

// ----- thin scheduler panel -----

const STORAGE_KIND = "simple_scheduler/v4";

interface SchedulerStorage {
	readonly kind: typeof STORAGE_KIND;
	readonly inbox: CallbackInboxStub<SchedulerAnnouncement> | null;
	/** Anchors the `subscribe_to` callback-target invoke's slot (the
	 * return value is `null`; the field exists so the allocated stub
	 * stays referenced from userState). */
	readonly subInvoke: OpResultStub<null> | null;
	/** Per-channel storage slots for the dispatched channel panels,
	 * keyed by the channel storage stub's shortId — a new run's panel
	 * must not inherit the previous channel's persisted slots. */
	readonly channelPanels: Readonly<Record<string, unknown>>;
}

const INITIAL: SchedulerStorage = {
	kind: STORAGE_KIND,
	inbox: null,
	subInvoke: null,
	channelPanels: {},
};

function isValidStorage(s: SchedulerStorage): boolean {
	return s?.kind === STORAGE_KIND;
}

const EMPTY_ANNOUNCEMENTS = atom<readonly SchedulerAnnouncement[]>([]);

interface SchedulerDerived {
	readonly latest: { readonly runId: string; readonly reader: ServiceRefStub } | null;
	readonly closed: boolean;
}

function deriveScheduler(events: readonly SchedulerAnnouncement[]): SchedulerDerived {
	let latest: SchedulerDerived["latest"] = null;
	let closed = false;
	for (const ev of events) {
		if (ev.kind === "run_channel" && ev.reader !== undefined) {
			latest = { runId: ev.run_id ?? "?", reader: ev.reader };
		} else if (ev.kind === "scheduler_closing") {
			closed = true;
		}
	}
	return { latest, closed };
}

export const SchedulerFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		() =>
			atomWithDefault<SchedulerStorage>(
				ownStorage as FxtrSessionAtom<SchedulerStorage | null>,
				INITIAL,
				isValidStorage,
			),
		[ownStorage],
	);
	const state = useAtomValue(typedStorage);
	const setState = useSetAtom(typedStorage);

	// Subscribe exactly once per panel session: allocate the inbox and
	// dispatch the callback-target `subscribe_to(schedulerRef)` invoke
	// on its handle in the same tick (one wire mutation). The inbox +
	// pending invoke persist in ownStorage; the function-updater
	// re-checks so double-fires are no-ops.
	useEffect(() => {
		setState((prev, ctx) => {
			if (prev.inbox != null) return prev;
			const inbox = ctx.allocateCallback<SchedulerAnnouncement>();
			const subInvoke = ctx.allocateInvoke<null>(inbox.handle, "subscribe_to", [
				stub as ServiceRefStub<unknown>,
			]);
			return { ...prev, kind: STORAGE_KIND, inbox, subInvoke };
		});
	}, [setState, stub]);

	const events = useAtomValue(state.inbox !== null ? state.inbox.events : EMPTY_ANNOUNCEMENTS);
	const { latest, closed } = useMemo(() => deriveScheduler(events), [events]);

	const channelPanelsAtom = useMemo(() => subAtom(typedStorage, "channelPanels"), [typedStorage]);
	const channelSlot = useMemo(
		() =>
			latest === null
				? null
				: (subAtom(
						channelPanelsAtom as FxtrSessionAtom<Record<string, unknown>>,
						latest.reader.shortId,
					) as FxtrSessionAtom<unknown>),
		[channelPanelsAtom, latest],
	);

	return (
		<div className="fw-panel__body" style={rootStyle}>
			<h2 style={headerStyle}>
				simple_scheduler · {stub.shortId}
				{closed ? <span style={closedBadgeStyle}>scheduler shut down</span> : null}
			</h2>
			{latest === null || channelSlot === null ? (
				<div style={mutedStyle}>
					{state.inbox === null
						? "subscribing…"
						: closed
							? "scheduler shut down before any run"
							: "no run yet — waiting for the agent to call run_batched(...)"}
				</div>
			) : (
				<>
					<div style={mutedStyle}>
						run <code>{latest.runId}</code>
					</div>
					{/* Nested dispatch: the channel-reader FullPanel
					    (@fxtr-ui/rd-types-channel) fetches get_tag() and
					    re-dispatches the grid card on the run tag's type. */}
					<RenderDispatch
						key={latest.reader.shortId}
						stub={latest.reader}
						as={FullPanel}
						ownStorage={channelSlot}
					/>
				</>
			)}
		</div>
	);
};

// ----- the run-tag grid card (ChannelContentCard for scheduler_run_tag) -----

/** Wire shape of the loaded `SchedulerRunTag` entity (structural). */
interface RunTagData {
	readonly run_id?: string;
	readonly step_labels?: readonly string[];
	readonly input_names?: readonly string[];
}

const CARD_KIND = "scheduler-run-card/v1";

interface RunCardStorage {
	readonly kind: typeof CARD_KIND;
	readonly tagLoad: OpResultStub<RunTagData> | null;
	/** `useChannelSubscription`'s slot. */
	readonly subscription: unknown;
	/** `useChannelStateSubscription`'s slot (the live RunStatus badge). */
	readonly state: unknown;
}

const CARD_INITIAL: RunCardStorage = {
	kind: CARD_KIND,
	tagLoad: null,
	subscription: null,
	state: null,
};

/** The live RunStatus badge, driven by the cheap state stream. */
function RunStatusBadge({ state }: { readonly state: ChannelStateEventWire | null }) {
	if (state === null || state.kind === "aborted_before_start") return null;
	const status = state.status as { readonly phase?: string; readonly detail?: string } | null;
	if (status === null || typeof status !== "object") return null;
	const phase = typeof status.phase === "string" ? status.phase : "?";
	return (
		<span style={statusBadgeStyle}>
			{phase}
			{typeof status.detail === "string" ? ` · ${status.detail}` : ""}
		</span>
	);
}

function isValidCardStorage(s: RunCardStorage): boolean {
	return s?.kind === CARD_KIND;
}

// ----- grid derivation from channel messages -----

type CellState =
	| { readonly status: "waiting" }
	| { readonly status: "running" }
	| { readonly status: "done"; readonly output: EntityRefStub }
	| { readonly status: "failed"; readonly error: string };

interface RunInputs {
	readonly inputs: Readonly<Record<string, EntityRefStub>>;
}

interface LiveGrid {
	readonly runInputs: RunInputs | null;
	readonly cells: ReadonlyMap<string, CellState>;
}

function deriveGrid(messages: readonly ResultValue[]): LiveGrid {
	let runInputs: RunInputs | null = null;
	const cells = new Map<string, CellState>();
	for (const raw of messages) {
		if (typeof raw !== "object" || raw === null || Array.isArray(raw)) continue;
		const m = raw as { readonly [key: string]: unknown };
		const kind = m.kind;
		if (kind === "run_inputs") {
			runInputs = {
				inputs: (m.inputs as Readonly<Record<string, EntityRefStub>> | undefined) ?? {},
			};
		} else if (kind === "task_started" || kind === "task_result" || kind === "task_failed") {
			const key = `${m.input_name}:${m.step_index}`;
			if (kind === "task_started") {
				if (cells.get(key)?.status !== "done") cells.set(key, { status: "running" });
			} else if (kind === "task_result" && m.output instanceof EntityRefStub) {
				cells.set(key, { status: "done", output: m.output });
			} else if (kind === "task_failed") {
				cells.set(key, {
					status: "failed",
					error: typeof m.error === "string" ? m.error : "(unknown error)",
				});
			}
		}
	}
	return { runInputs, cells };
}

export const SchedulerRunCard: DispatchedComponent<EntityRefStub, ChannelContentCardProps> = ({
	stub,
	channel,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		() =>
			atomWithDefault<RunCardStorage>(
				ownStorage as FxtrSessionAtom<RunCardStorage | null>,
				CARD_INITIAL,
				isValidCardStorage,
			),
		[ownStorage],
	);
	const tagResultAtom = useMemo(
		() => makeLoadResultAtom<RunTagData>(subAtom(typedStorage, "tagLoad"), stub),
		[typedStorage, stub],
	);
	const tagResult: LoadResult<RunTagData> = useAtomValue(tagResultAtom);
	const subscriptionSlot = useMemo(
		() => subAtom(typedStorage, "subscription") as FxtrSessionAtom<unknown>,
		[typedStorage],
	);
	const view: ChannelViewState = useChannelSubscription(channel, subscriptionSlot);
	const stateSlot = useMemo(
		() => subAtom(typedStorage, "state") as FxtrSessionAtom<unknown>,
		[typedStorage],
	);
	const liveState = useChannelStateSubscription(channel, stateSlot);

	if (tagResult.kind === "errored") {
		return <div style={errorStyle}>failed to load run tag: {tagResult.message}</div>;
	}
	if (tagResult.kind === "loading") {
		return <div style={mutedStyle}>loading run metadata…</div>;
	}
	const tag = tagResult.value;
	const stepLabels = tag.step_labels ?? [];
	const inputNames = tag.input_names ?? [];

	if (view.phase === "connecting") {
		return <div style={mutedStyle}>connecting to the run channel…</div>;
	}
	if (view.phase === "aborted") {
		return (
			<div style={errorStyle}>
				run <code>{tag.run_id ?? "?"}</code> aborted: {view.reason}
			</div>
		);
	}
	if (view.phase === "complete") {
		return (
			<div style={statusRowStyle}>
				<span style={mutedStyle}>
					run <code>{tag.run_id ?? "?"}</code> — completed
				</span>
				{view.result instanceof EntityRefStub ? (
					<span style={{ marginLeft: "auto" }}>
						<RefLink stub={view.result} as="block" />
					</span>
				) : null}
			</div>
		);
	}

	const { runInputs, cells } = deriveGrid(view.values);
	return (
		<div style={cardRootStyle}>
			<div style={statusRowStyle}>
				<span style={mutedStyle}>
					run <code>{tag.run_id ?? "?"}</code> — in progress
				</span>
				<RunStatusBadge state={liveState} />
			</div>
			<div style={{ overflow: "auto" }}>
				<table style={tableStyle}>
					<thead>
						<tr>
							<th style={thStyle}>input</th>
							{stepLabels.map((label, i) => (
								// biome-ignore lint/suspicious/noArrayIndexKey: column order is the identity
								<th key={i} style={thStyle}>
									{label}
								</th>
							))}
						</tr>
					</thead>
					<tbody>
						{inputNames.map((name) => (
							<tr key={name}>
								<td style={tdStyle}>
									<div style={inputCellHeaderStyle}>{name}</div>
									{runInputs?.inputs[name] !== undefined ? (
										<RefLink stub={runInputs.inputs[name] as EntityRefStub} as="block" />
									) : (
										<span style={mutedStyle}>(input unavailable)</span>
									)}
								</td>
								{stepLabels.map((_label, i) => (
									// biome-ignore lint/suspicious/noArrayIndexKey: column order is the identity
									<td key={i} style={tdStyle}>
										<GridCell cell={cells.get(`${name}:${i}`)} />
									</td>
								))}
							</tr>
						))}
					</tbody>
				</table>
			</div>
		</div>
	);
};

function GridCell({ cell }: { readonly cell: CellState | undefined }) {
	if (cell === undefined || cell.status === "waiting") {
		return <span style={waitingStyle}>· waiting</span>;
	}
	if (cell.status === "running") {
		return <span style={runningStyle}>⟳ running…</span>;
	}
	if (cell.status === "failed") {
		return <div style={errorStyle}>✗ {cell.error}</div>;
	}
	return <RefLink stub={cell.output} as="block" />;
}

// ----- link keys: static pills -----

export const SchedulerBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => (
	<span style={linkPillStyle}>
		<span style={linkTagStyle}>scheduler</span>
		<span style={linkLabelStyle}>{linkText ?? `service · ${stub.shortId}`}</span>
	</span>
);

export const SchedulerInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => <code style={inlineLinkStyle}>simple_scheduler:{linkText ?? stub.shortId}</code>;

export const SchedulerHoverPreview: DispatchedComponent<ServiceRefStub, HoverPreviewProps> = ({
	stub,
}) => (
	<div style={hoverCardStyle}>
		<div style={hoverTitleStyle}>simple_scheduler service</div>
		<div style={hoverBodyStyle}>
			<code>{stub.shortId}</code> — open the panel to watch batched runs as a live grid.
		</div>
	</div>
);

// ----- styles -----

const rootStyle: CSSProperties = {
	padding: "var(--sp-5, 16px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
	height: "100%",
};
const cardRootStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
	minHeight: 0,
};
const headerStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-3, 12px)",
};
const closedBadgeStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--status-warning, #b60)",
	border: "1px solid var(--status-warning, #b60)",
	borderRadius: "var(--radius-badge, 6px)",
	padding: "1px 6px",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
};
const statusRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
};
const statusBadgeStyle: CSSProperties = {
	marginLeft: "auto",
	fontSize: "var(--text-caption)",
	color: "var(--status-info, #06c)",
	border: "1px solid var(--status-info, #06c)",
	borderRadius: "var(--radius-badge, 6px)",
	padding: "1px 6px",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
	whiteSpace: "nowrap",
};
const tableStyle: CSSProperties = {
	borderCollapse: "collapse",
	fontSize: "var(--text-body-sm)",
};
const thStyle: CSSProperties = {
	textAlign: "left",
	padding: "var(--sp-1) var(--sp-2)",
	borderBottom: "2px solid var(--border-subtle)",
	color: "var(--text-secondary)",
	verticalAlign: "bottom",
};
const tdStyle: CSSProperties = {
	padding: "var(--sp-2)",
	border: "1px solid var(--border-subtle)",
	verticalAlign: "top",
	minWidth: 160,
	maxWidth: 360,
};
const inputCellHeaderStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	marginBottom: "var(--sp-1, 4px)",
};
const waitingStyle: CSSProperties = { color: "var(--text-muted)", fontSize: "var(--text-body-sm)" };
const runningStyle: CSSProperties = {
	color: "var(--status-info, #06c)",
	fontSize: "var(--text-body-sm)",
};
const errorStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
const mutedStyle: CSSProperties = { color: "var(--text-muted)", fontSize: "var(--text-body-sm)" };
const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2, 6px)",
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
	background: "var(--surface-raised)",
	fontSize: "var(--text-body-sm)",
};
const linkTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	letterSpacing: "0.05em",
};
const linkLabelStyle: CSSProperties = { color: "var(--text-primary)" };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "var(--surface-sunken)",
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
};
const hoverCardStyle: CSSProperties = {
	minWidth: 200,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600, color: "var(--text-primary)" };
const hoverBodyStyle: CSSProperties = {
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
	lineHeight: 1.4,
};

export default {
	simple_scheduler: {
		[FullPanel.uuid]: SchedulerFullPanel,
		[BlockLink.uuid]: SchedulerBlockLink,
		[InlineLink.uuid]: SchedulerInlineLink,
		[HoverPreview.uuid]: SchedulerHoverPreview,
	},
	scheduler_run_tag: {
		[ChannelContentCard.uuid]: SchedulerRunCard,
	},
};
