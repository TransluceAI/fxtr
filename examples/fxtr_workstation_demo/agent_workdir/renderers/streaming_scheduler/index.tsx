/**
 * Renderers for the `streaming_scheduler` demo (nested channels via
 * subchannels). Three pieces:
 *
 * **Thin scheduler-service FullPanel** — identical pattern to
 * `renderers/simple_scheduler/`: subscribe to the scheduler's
 * announcement stream, follow the latest `run_channel`, nested
 * FullPanel dispatch on the announced channel **reader** stub (which
 * routes through `@fxtr-ui/rd-types-channel`'s `ChannelReaderView` —
 * get_tag() → tag — into the run card below).
 *
 * **`StreamingRunCard`** — `ChannelContentCard` renderer for the
 * `streaming_run_tag` type. A grid like the simple scheduler's, but
 * driven by `cell_channel` / `cell_result` messages: a *streaming*
 * cell embeds its progress channel inline via `<SubchannelView>`,
 * which opens the cell subchannel by its **key** off the run reader
 * (`open_subchannel_reader`) and drills get_tag() → the cell's
 * `ChannelContentCard` (landing on `TaskProgressCard` below);
 * completed cells collapse to the output reflink. A *cached* cell
 * (served from the scheduler's cache without running the task) arrives
 * as a `cell_result` with `cached: true` and no `cell_channel`, so it
 * renders straight to the output reflink with a "cached" badge.
 *
 * **`TaskProgressCard`** — `ChannelContentCard` renderer for the
 * `task_progress_tag` type: the string-appending streaming view.
 * Renders the latest `partial` text live; on complete, swaps to the
 * output reflink.
 */

import {
	type CallbackInboxStub,
	EntityRefStub,
	type FxtrSessionAtom,
	type OpResultStub,
	type ResultValue,
	ServiceRefStub,
} from "@fxtr-ui/core";
import {
	ChannelContentCard,
	type ChannelContentCardProps,
	type ChannelViewState,
	SubchannelView,
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

const STORAGE_KIND = "streaming_scheduler/v1";

interface SchedulerStorage {
	readonly kind: typeof STORAGE_KIND;
	readonly inbox: CallbackInboxStub<SchedulerAnnouncement> | null;
	/** Anchors the `subscribe_to` callback-target invoke's slot. */
	readonly subInvoke: OpResultStub<null> | null;
	/** Per-channel storage slots, keyed by the channel storage stub's
	 * shortId — a new run's panel must not inherit the previous
	 * channel's persisted slots. */
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

function deriveScheduler(events: readonly SchedulerAnnouncement[]): {
	readonly latest: { readonly runId: string; readonly reader: ServiceRefStub } | null;
	readonly closed: boolean;
} {
	let latest: { readonly runId: string; readonly reader: ServiceRefStub } | null = null;
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

export const StreamingSchedulerFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
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
				streaming_scheduler · {stub.shortId}
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

// ----- shared: tag-load + subscription card storage shape -----

interface RunTagData {
	readonly run_id?: string;
	readonly step_labels?: readonly string[];
	readonly input_names?: readonly string[];
}

const RUN_CARD_KIND = "streaming-run-card/v1";

interface RunCardStorage {
	readonly kind: typeof RUN_CARD_KIND;
	readonly tagLoad: OpResultStub<RunTagData> | null;
	readonly subscription: unknown;
	/** Per-cell storage slots for the embedded progress cards, keyed
	 * by the cell's subchannel key. */
	readonly cellCards: Readonly<Record<string, unknown>>;
}

const RUN_CARD_INITIAL: RunCardStorage = {
	kind: RUN_CARD_KIND,
	tagLoad: null,
	subscription: null,
	cellCards: {},
};

function isValidRunCardStorage(s: RunCardStorage): boolean {
	return s?.kind === RUN_CARD_KIND;
}

// ----- run-grid derivation from channel messages -----

type CellState =
	| { readonly status: "waiting" }
	| {
			readonly status: "streaming";
			readonly channelKey: string;
	  }
	| { readonly status: "done"; readonly output: EntityRefStub; readonly cached: boolean }
	| { readonly status: "failed"; readonly error: string };

interface RunInputs {
	readonly inputs: Readonly<Record<string, EntityRefStub>>;
}

function deriveStreamingGrid(messages: readonly ResultValue[]): {
	readonly runInputs: RunInputs | null;
	readonly cells: ReadonlyMap<string, CellState>;
} {
	let runInputs: RunInputs | null = null;
	const cells = new Map<string, CellState>();
	for (const raw of messages) {
		if (typeof raw !== "object" || raw === null || Array.isArray(raw)) continue;
		const m = raw as { readonly [key: string]: unknown };
		if (m.kind === "run_inputs") {
			runInputs = {
				inputs: (m.inputs as Readonly<Record<string, EntityRefStub>> | undefined) ?? {},
			};
			continue;
		}
		if (m.kind !== "cell_channel" && m.kind !== "cell_result") continue;
		const key = `${m.input_name}:${m.step_index}`;
		if (m.kind === "cell_channel" && typeof m.key === "string") {
			// Don't regress a terminal cell (defensive — results follow
			// announcements in the log).
			const existing = cells.get(key)?.status;
			if (existing !== "done" && existing !== "failed") {
				cells.set(key, { status: "streaming", channelKey: m.key });
			}
		} else if (m.kind === "cell_result") {
			if (m.ok === true && m.output instanceof EntityRefStub) {
				// A cached cell arrives as a cell_result with cached:true and no
				// preceding cell_channel — rendered directly from this event.
				cells.set(key, { status: "done", output: m.output, cached: m.cached === true });
			} else {
				cells.set(key, {
					status: "failed",
					error: typeof m.error === "string" ? m.error : "(unknown error)",
				});
			}
		}
	}
	return { runInputs, cells };
}

export const StreamingRunCard: DispatchedComponent<EntityRefStub, ChannelContentCardProps> = ({
	stub,
	channel,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		() =>
			atomWithDefault<RunCardStorage>(
				ownStorage as FxtrSessionAtom<RunCardStorage | null>,
				RUN_CARD_INITIAL,
				isValidRunCardStorage,
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
	const cellCardsAtom = useMemo(() => subAtom(typedStorage, "cellCards"), [typedStorage]);

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

	const { runInputs, cells } = deriveStreamingGrid(view.values);
	return (
		<div style={cardRootStyle}>
			<div style={statusRowStyle}>
				<span style={mutedStyle}>
					run <code>{tag.run_id ?? "?"}</code> — in progress
				</span>
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
										<StreamingGridCell
											cell={cells.get(`${name}:${i}`)}
											parentReader={channel}
											cellCardsAtom={cellCardsAtom as FxtrSessionAtom<Record<string, unknown>>}
										/>
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

function StreamingGridCell({
	cell,
	parentReader,
	cellCardsAtom,
}: {
	readonly cell: CellState | undefined;
	readonly parentReader: ServiceRefStub;
	readonly cellCardsAtom: FxtrSessionAtom<Record<string, unknown>>;
}) {
	const channelKey = cell?.status === "streaming" ? cell.channelKey : null;
	const cellSlot = useMemo(
		() =>
			channelKey === null
				? null
				: (subAtom(cellCardsAtom, channelKey) as FxtrSessionAtom<unknown>),
		[cellCardsAtom, channelKey],
	);
	if (cell === undefined || cell.status === "waiting") {
		return <span style={waitingStyle}>· waiting</span>;
	}
	if (cell.status === "failed") {
		return <div style={errorStyle}>✗ {cell.error}</div>;
	}
	if (cell.status === "done") {
		return (
			<div style={doneCellStyle}>
				{cell.cached ? <span style={cachedBadgeStyle}>cached</span> : null}
				<RefLink stub={cell.output} as="block" />
			</div>
		);
	}
	// Streaming: open the cell subchannel by key off the run reader and embed
	// it — SubchannelView drills get_tag → the cell's ChannelContentCard
	// (lands on TaskProgressCard).
	return cellSlot === null || channelKey === null ? null : (
		<SubchannelView
			key={channelKey}
			parentReader={parentReader}
			channelKey={channelKey}
			ownStorage={cellSlot}
		/>
	);
}

// ----- the per-cell streaming view (ChannelContentCard for task_progress_tag) -----

const PROGRESS_CARD_KIND = "task-progress-card/v1";

interface ProgressCardStorage {
	readonly kind: typeof PROGRESS_CARD_KIND;
	readonly subscription: unknown;
}

const PROGRESS_CARD_INITIAL: ProgressCardStorage = {
	kind: PROGRESS_CARD_KIND,
	subscription: null,
};

function isValidProgressCardStorage(s: ProgressCardStorage): boolean {
	return s?.kind === PROGRESS_CARD_KIND;
}

function latestProgress(messages: readonly ResultValue[]): {
	readonly text: string | null;
	readonly fraction: number | null;
} {
	let text: string | null = null;
	let fraction: number | null = null;
	for (const raw of messages) {
		if (typeof raw !== "object" || raw === null || Array.isArray(raw)) continue;
		const m = raw as { readonly [key: string]: unknown };
		if (m.kind === "partial" && typeof m.text === "string") {
			text = m.text;
			fraction = null;
		} else if (m.kind === "progress") {
			if (typeof m.text === "string") text = m.text;
			if (typeof m.fraction === "number") fraction = m.fraction;
		}
	}
	return { text, fraction };
}

export const TaskProgressCard: DispatchedComponent<EntityRefStub, ChannelContentCardProps> = ({
	channel,
	ownStorage,
}) => {
	const typedStorage = useMemo(
		() =>
			atomWithDefault<ProgressCardStorage>(
				ownStorage as FxtrSessionAtom<ProgressCardStorage | null>,
				PROGRESS_CARD_INITIAL,
				isValidProgressCardStorage,
			),
		[ownStorage],
	);
	const subscriptionSlot = useMemo(
		() => subAtom(typedStorage, "subscription") as FxtrSessionAtom<unknown>,
		[typedStorage],
	);
	const view = useChannelSubscription(channel, subscriptionSlot);

	if (view.phase === "connecting") {
		return <span style={mutedStyle}>connecting…</span>;
	}
	if (view.phase === "aborted") {
		return <div style={errorStyle}>✗ {view.reason}</div>;
	}
	if (view.phase === "complete") {
		return view.result instanceof EntityRefStub ? (
			<RefLink stub={view.result} as="block" />
		) : (
			<span style={mutedStyle}>done</span>
		);
	}
	const { text, fraction } = latestProgress(view.values);
	return (
		<div style={progressBoxStyle}>
			{text === null ? (
				<span style={runningStyle}>⟳ running…</span>
			) : (
				<span style={progressTextStyle}>
					{text}
					<span style={cursorStyle}>▌</span>
				</span>
			)}
			{fraction !== null ? <span style={fractionStyle}>{Math.round(fraction * 100)}%</span> : null}
		</div>
	);
};

// ----- link keys: static pills -----

export const StreamingSchedulerBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => (
	<span style={linkPillStyle}>
		<span style={linkTagStyle}>streaming scheduler</span>
		<span style={linkLabelStyle}>{linkText ?? `service · ${stub.shortId}`}</span>
	</span>
);

export const StreamingSchedulerInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({
	stub,
	linkText,
}) => <code style={inlineLinkStyle}>streaming_scheduler:{linkText ?? stub.shortId}</code>;

export const StreamingSchedulerHoverPreview: DispatchedComponent<
	ServiceRefStub,
	HoverPreviewProps
> = ({ stub }) => (
	<div style={hoverCardStyle}>
		<div style={hoverTitleStyle}>streaming_scheduler service</div>
		<div style={hoverBodyStyle}>
			<code>{stub.shortId}</code> — open the panel to watch runs stream every cell's interior
			progress live.
		</div>
	</div>
);

// ----- styles (shared vocabulary with renderers/simple_scheduler) -----

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
	minWidth: 200,
	maxWidth: 420,
};
const inputCellHeaderStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	marginBottom: "var(--sp-1, 4px)",
};
const waitingStyle: CSSProperties = { color: "var(--text-muted)", fontSize: "var(--text-body-sm)" };
const doneCellStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
	alignItems: "flex-start",
};
const cachedBadgeStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--status-info, #06c)",
	border: "1px solid var(--status-info, #06c)",
	borderRadius: "var(--radius-badge, 6px)",
	padding: "0 5px",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
};
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
const progressBoxStyle: CSSProperties = {
	display: "flex",
	alignItems: "baseline",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-1) var(--sp-2)",
	borderLeft: "2px solid var(--status-info, #06c)",
	background: "var(--surface-sunken)",
};
const progressTextStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
const cursorStyle: CSSProperties = {
	color: "var(--status-info, #06c)",
	marginLeft: 2,
};
const fractionStyle: CSSProperties = {
	marginLeft: "auto",
	color: "var(--text-muted)",
	fontSize: "var(--text-caption)",
	fontFamily: "var(--font-mono)",
};
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
	streaming_scheduler: {
		[FullPanel.uuid]: StreamingSchedulerFullPanel,
		[BlockLink.uuid]: StreamingSchedulerBlockLink,
		[InlineLink.uuid]: StreamingSchedulerInlineLink,
		[HoverPreview.uuid]: StreamingSchedulerHoverPreview,
	},
	streaming_run_tag: {
		[ChannelContentCard.uuid]: StreamingRunCard,
	},
	task_progress_tag: {
		[ChannelContentCard.uuid]: TaskProgressCard,
	},
};
