/**
 * Renderers for the **fixed channel-reader service type**
 * (`CHANNEL_READER_TYPE_ID` in `fxtr2/common/channels.py`), covering the
 * standard workstation keys. The fixed service type is what lets the
 * frontend recognize "this service is a channel" at all.
 *
 * - The **FullPanel** wraps the reusable `ChannelReaderView` in panel
 *   chrome: it fetches `get_tag()` and re-dispatches `ChannelContentCard`
 *   on the tag's entity type (the per-stream customization point).
 * - The compact keys (block / inline / hover) drive a **live** status pill:
 *   `subscribe_state` (the primary lifecycle signal, cheap — no event log)
 *   plus a best-effort `get_tag` so a registered `ChannelStatusPreview` can
 *   style the channel's kind. While the tag is pending (channel not started)
 *   or absent (aborted before start), a generic pill renders from the state
 *   stream alone.
 *
 * Registration (the `{subtype_key: {key_uuid: Component}}` renderer module
 * map) is the consuming renderer slice's job — see `channelReaderRenderers`.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchedComponent,
	RenderDispatch,
	useDispatchSharedAtom,
} from "@fxtr-ui/react-dispatch";
import { type LoadResult, makeInvokeResultAtom, subAtom } from "@fxtr-ui/state-util";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";
import { ChannelStatusPreview } from "./keys.js";
import { ChannelReaderView } from "./reader_view.js";
import { useChannelStateSubscription } from "./subscription.js";
import type { ChannelStateEventWire } from "./types.js";

// ----- FullPanel: panel chrome around ChannelReaderView -----

export const ChannelReaderFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => (
	<div className="fw-panel__body" style={panelStyle}>
		<ChannelReaderView reader={stub} ownStorage={ownStorage as FxtrSessionAtom<unknown>} />
	</div>
);

// ----- compact keys: live status pill -----

const COMPACT_KIND = "channel-reader-compact/v1";

interface CompactSlot {
	readonly kind: typeof COMPACT_KIND;
	/** Best-effort `get_tag` invoke (enables per-type `ChannelStatusPreview`). */
	readonly getTagInvoke: OpResultStub<EntityRefStub> | null;
	/** `useChannelStateSubscription`'s slot. */
	readonly stateSub: unknown;
}

const COMPACT_INITIAL: CompactSlot = { kind: COMPACT_KIND, getTagInvoke: null, stateSub: null };

/**
 * One shared subscribe-state + best-effort-get-tag wrapper serves all
 * three compact keys. Each holds its own slots in the dispatch-entry shared
 * state (per-(stub, key)). On a filled tag, re-dispatch `ChannelStatusPreview`
 * (passing the live state); otherwise a generic state-driven pill.
 */
const ChannelReaderPreviewRedispatch: DispatchedComponent<ServiceRefStub, object> = ({ stub }) => {
	const slotAtom = useDispatchSharedAtom<CompactSlot>(COMPACT_INITIAL);
	const tagResultAtom = useMemo(
		() =>
			makeInvokeResultAtom<EntityRefStub>(
				subAtom(slotAtom, "getTagInvoke") as FxtrSessionAtom<OpResultStub<EntityRefStub> | null>,
				stub,
				"get_tag",
				[],
			),
		[slotAtom, stub],
	);
	const tagResult: LoadResult<EntityRefStub> = useAtomValue(tagResultAtom);
	const stateSlot = useMemo(
		() => subAtom(slotAtom, "stateSub") as FxtrSessionAtom<unknown>,
		[slotAtom],
	);
	const state = useChannelStateSubscription(stub, stateSlot);

	if (tagResult.kind === "filled") {
		return (
			<RenderDispatch
				stub={tagResult.value}
				as={ChannelStatusPreview}
				state={state}
				channel={stub}
			/>
		);
	}
	// Tag pending (not started) or errored (aborted before start): the state
	// stream is the lifecycle signal; render a generic, tagless pill.
	return <GenericStatePill state={state} note={stub.shortId} />;
};

export const ChannelReaderBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> =
	ChannelReaderPreviewRedispatch;
export const ChannelReaderInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> =
	ChannelReaderPreviewRedispatch;
export const ChannelReaderHoverPreview: DispatchedComponent<ServiceRefStub, HoverPreviewProps> =
	ChannelReaderPreviewRedispatch;

/**
 * The reader renderers as a dispatch-key-uuid-keyed record — the inner
 * level of the renderer-module map contract
 * (`{subtype_key: {dispatch_key_uuid: Component}}`). The consuming renderer
 * slice picks the subtype key and wraps:
 * `export default { channel_reader: channelReaderRenderers }`.
 */
export const channelReaderRenderers = {
	[FullPanel.uuid]: ChannelReaderFullPanel,
	[BlockLink.uuid]: ChannelReaderBlockLink,
	[InlineLink.uuid]: ChannelReaderInlineLink,
	[HoverPreview.uuid]: ChannelReaderHoverPreview,
} as const;

// ----- the generic (tagless) state pill -----

function GenericStatePill({
	state,
	note,
}: {
	readonly state: ChannelStateEventWire | null;
	readonly note: string;
}) {
	const { label, tone } = lifecycle(state);
	const badge = tone === "abort" ? abortBadgeStyle : tone === "open" ? openBadgeStyle : badgeStyle;
	return (
		<span style={pillStyle}>
			<span style={pillTagStyle}>channel</span>
			<span style={pillNoteStyle}>{note}</span>
			<span style={badge}>{label}</span>
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

const panelStyle: CSSProperties = {
	padding: "var(--sp-5, 16px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
	height: "100%",
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
const pillNoteStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontFamily: "var(--font-mono)",
};
const badgeStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
	padding: "0 5px",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
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
