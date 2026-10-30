/**
 * Default fallback components for the two channel dispatch keys —
 * including the **generic channel card**, which doubles as
 * `ChannelContentCard`'s NoMatch: a channel whose tag type has no
 * registered renderer still gets a fully functional live view (message
 * log → final value, plus a schema-blind subchannel list), not an error
 * state.
 *
 * Style tokens follow the workstation-dispatch convention (CSS custom
 * properties like `--sp-2` / `--border-subtle`); consumers re-theme by
 * overriding those at any ancestor.
 */

import { EntityRefStub, type FxtrSessionAtom, ServiceRefStub } from "@fxtr-ui/core";
import { atomWithDefault, subAtom } from "@fxtr-ui/state-util";
import { JsonRefTree, RefLink } from "@fxtr-ui/workstation-dispatch";
import type { CSSProperties } from "react";
import { useMemo } from "react";
import type { ChannelContentCardProps, ChannelStatusPreviewProps } from "./keys.js";
import { useSubchannelReader } from "./reader_view.js";
import { useChannelSubscription, useDebugSubchannels } from "./subscription.js";
import type { ChannelStateEventWire, ChannelViewState, SubchannelOpenedWire } from "./types.js";

const GENERIC_CARD_KIND = "generic-channel-card/v1";

interface GenericCardStorage {
	readonly kind: typeof GENERIC_CARD_KIND;
	/** `useChannelSubscription`'s slot. */
	readonly subscription: unknown;
	/** `useDebugSubchannels`'s slot. */
	readonly debug: unknown;
	/** Per-subchannel `useSubchannelReader` slots, keyed by subchannel key. */
	readonly subchannels: Readonly<Record<string, unknown>>;
}

const GENERIC_CARD_INITIAL: GenericCardStorage = {
	kind: GENERIC_CARD_KIND,
	subscription: null,
	debug: null,
	subchannels: {},
};

function isValidGenericCardStorage(s: GenericCardStorage): boolean {
	return s?.kind === GENERIC_CARD_KIND;
}

/**
 * The generic channel card: header reflink for the tag, then the folded
 * view of the full subscription — scrolling message rows while open (each
 * value through `JsonRefTree`, so refs become reflinks), the final value
 * once complete, a reason banner on abort. While open it also lists the
 * channel's **subchannels** (from the debug feed) as openable reflinks —
 * a schema-blind way to trace sub-computations without knowing which
 * message fields are keys.
 *
 * This is `ChannelContentCard.NoMatch`, and a named export so custom tag
 * renderers can compose it.
 */
export function GenericChannelCard(props: { stub: EntityRefStub } & ChannelContentCardProps) {
	const typed = useMemo(
		() =>
			atomWithDefault<GenericCardStorage>(
				props.ownStorage as FxtrSessionAtom<GenericCardStorage | null>,
				GENERIC_CARD_INITIAL,
				isValidGenericCardStorage,
			),
		[props.ownStorage],
	);
	const subscriptionSlot = useMemo(
		() => subAtom(typed, "subscription") as FxtrSessionAtom<unknown>,
		[typed],
	);
	const debugSlot = useMemo(() => subAtom(typed, "debug") as FxtrSessionAtom<unknown>, [typed]);
	const subchannelsAtom = useMemo(
		() => subAtom(typed, "subchannels") as FxtrSessionAtom<Record<string, unknown>>,
		[typed],
	);
	const view = useChannelSubscription(props.channel, subscriptionSlot);
	// Enumerate subchannels regardless of phase (the hook must run
	// unconditionally); the section only renders while open.
	const subchannels = useDebugSubchannels(props.channel, debugSlot);

	return (
		<div style={cardStyle}>
			<div style={headerRowStyle}>
				<RefLink stub={props.stub} as="block" />
				<PhaseBadge view={view} />
			</div>
			<GenericChannelBody view={view} />
			{view.phase === "open" && subchannels.length > 0 ? (
				<SubchannelList
					parentReader={props.channel}
					subchannels={subchannels}
					subchannelsAtom={subchannelsAtom}
				/>
			) : null}
		</div>
	);
}

/**
 * The subchannel list: each `SubchannelOpened` becomes a lazily-opened
 * child reader rendered as a `RefLink` (which routes through the
 * channel-reader compact preview — a cheap live status pill, clickable to
 * open the full stream). Each entry holds its own open-invoke slot.
 */
function SubchannelList({
	parentReader,
	subchannels,
	subchannelsAtom,
}: {
	readonly parentReader: ServiceRefStub;
	readonly subchannels: readonly SubchannelOpenedWire[];
	readonly subchannelsAtom: FxtrSessionAtom<Record<string, unknown>>;
}) {
	return (
		<div style={subchannelsBoxStyle}>
			<span style={resultLabelStyle}>subchannels</span>
			{subchannels.map((info) => (
				<DebugSubchannelLink
					key={info.key}
					parentReader={parentReader}
					info={info}
					subchannelsAtom={subchannelsAtom}
				/>
			))}
		</div>
	);
}

function DebugSubchannelLink({
	parentReader,
	info,
	subchannelsAtom,
}: {
	readonly parentReader: ServiceRefStub;
	readonly info: SubchannelOpenedWire;
	readonly subchannelsAtom: FxtrSessionAtom<Record<string, unknown>>;
}) {
	const slot = useMemo(
		() => subAtom(subchannelsAtom, info.key) as FxtrSessionAtom<unknown>,
		[subchannelsAtom, info.key],
	);
	const child = useSubchannelReader(parentReader, info.key, slot);
	const label = info.name ?? info.key;
	if (child.kind === "loading") {
		return (
			<div style={subchannelRowStyle}>
				<span style={mutedStyle}>{label} · opening…</span>
			</div>
		);
	}
	if (child.kind === "errored") {
		return (
			<div style={subchannelRowStyle}>
				<span style={mutedStyle}>
					{label} · unavailable: {child.message}
				</span>
			</div>
		);
	}
	return (
		<div style={subchannelRowStyle}>
			<RefLink stub={child.value} as="block" linkText={label} />
		</div>
	);
}

function PhaseBadge({ view }: { readonly view: ChannelViewState }) {
	const label =
		view.phase === "connecting"
			? "connecting…"
			: view.phase === "open"
				? `open · ${view.values.length} message${view.values.length === 1 ? "" : "s"}`
				: view.phase;
	const style =
		view.phase === "aborted"
			? abortBadgeStyle
			: view.phase === "open"
				? openBadgeStyle
				: badgeStyle;
	return <span style={style}>{label}</span>;
}

function GenericChannelBody({ view }: { readonly view: ChannelViewState }) {
	switch (view.phase) {
		case "connecting":
			return <div style={mutedStyle}>waiting for the channel snapshot…</div>;
		case "open":
			return view.values.length === 0 ? (
				<div style={mutedStyle}>no messages yet</div>
			) : (
				<div style={messageListStyle}>
					{view.values.map((value, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: the log is append-only; position is the identity
						<div key={i} style={messageRowStyle}>
							{value instanceof EntityRefStub || value instanceof ServiceRefStub ? (
								<RefLink stub={value} as="block" />
							) : (
								<JsonRefTree value={value} initiallyExpanded={false} />
							)}
						</div>
					))}
				</div>
			);
		case "complete":
			return (
				<div style={resultBoxStyle}>
					<span style={resultLabelStyle}>result</span>
					{view.result instanceof EntityRefStub ? (
						<RefLink stub={view.result} as="block" />
					) : (
						<JsonRefTree value={view.result} />
					)}
				</div>
			);
		case "aborted":
			return <div style={abortBannerStyle}>channel aborted: {view.reason}</div>;
	}
}

/** Lightweight placeholder card (`ChannelContentCard.Loading`). */
export function ChannelCardLoadingFallback(
	props: { stub: EntityRefStub } & ChannelContentCardProps,
) {
	return (
		<div style={cardStyle}>
			<div style={headerRowStyle}>
				<span style={mutedStyle}>channel · {props.stub.shortId}</span>
				<span style={badgeStyle}>loading…</span>
			</div>
		</div>
	);
}

/**
 * The generic status pill (`ChannelStatusPreview.NoMatch`): tag inline
 * reflink + live lifecycle badge driven by the state stream. Compact
 * enough for all three link-ish slots.
 */
export function ChannelStatusPillFallback(
	props: { stub: EntityRefStub } & ChannelStatusPreviewProps,
) {
	const { label, tone } = lifecycle(props.state);
	const badge = tone === "abort" ? abortBadgeStyle : tone === "open" ? openBadgeStyle : badgeStyle;
	return (
		<span style={pillStyle}>
			<span style={pillTagStyle}>channel</span>
			<RefLink stub={props.stub} as="inline" />
			<span style={badge}>{label}</span>
		</span>
	);
}

/** Neutral pill while the preview's renderer is resolving. */
export function ChannelStatusPreviewLoadingFallback(
	props: { stub: EntityRefStub } & ChannelStatusPreviewProps,
) {
	return (
		<span style={pillStyle}>
			<span style={pillTagStyle}>channel</span>
			<span style={mutedStyle}>{props.stub.shortId}</span>
		</span>
	);
}

function lifecycle(state: ChannelStateEventWire | null): {
	label: string;
	tone: "open" | "abort" | "neutral";
} {
	if (state === null) return { label: "starting…", tone: "neutral" };
	if (state.kind === "aborted_before_start") return { label: "aborted", tone: "abort" };
	if (state.kind === "abort") return { label: "abort", tone: "abort" };
	if (state.kind === "open") return { label: "open", tone: "open" };
	return { label: "complete", tone: "neutral" };
}

// ----- styles -----

const cardStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3) var(--sp-4)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card, 8px)",
	background: "var(--surface-raised)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm)",
	minHeight: 0,
};
const headerRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
};
const badgeStyle: CSSProperties = {
	marginLeft: "auto",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
	padding: "1px 6px",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
	whiteSpace: "nowrap",
};
const openBadgeStyle: CSSProperties = {
	...badgeStyle,
	color: "var(--status-info, #06c)",
	border: "1px solid var(--status-info, #06c)",
};
const abortBadgeStyle: CSSProperties = {
	...badgeStyle,
	color: "var(--status-error, #c00)",
	border: "1px solid var(--status-error, #c00)",
};
const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
};
const messageListStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
	overflow: "auto",
	minHeight: 0,
};
const messageRowStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	borderLeft: "2px solid var(--border-subtle)",
};
const resultBoxStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
	padding: "var(--sp-2)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
	background: "var(--surface-sunken)",
};
const subchannelsBoxStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
	padding: "var(--sp-2)",
	border: "1px dashed var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
};
const subchannelRowStyle: CSSProperties = {
	paddingLeft: "var(--sp-2, 6px)",
};
const resultLabelStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	letterSpacing: "0.05em",
	color: "var(--text-muted)",
};
const abortBannerStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
const pillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2, 6px)",
	padding: "1px var(--sp-2, 6px)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
	background: "var(--surface-raised)",
	fontSize: "var(--text-body-sm)",
};
const pillTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	letterSpacing: "0.05em",
};
