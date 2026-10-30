/**
 * The reusable units for rendering a channel from its **reader** — the
 * heart of the package's uniform recursion.
 *
 * A channel is fronted only by a reader service. To render one you must
 * fetch its tag (`get_tag`, which blocks server-side until the channel
 * `start`s) to know *which* per-stream card to dispatch, then subscribe.
 * `ChannelReaderView` does the first half (reader → tag → dispatch
 * `ChannelContentCard`); the dispatched card does the rest (subscribe +
 * render). A subchannel is just a reader you open by key, so
 * `SubchannelView` opens it (`open_subchannel_reader`) and feeds the child
 * reader back into `ChannelReaderView` — the recursion closes.
 *
 * The package never interprets message contents; recognizing "this message
 * field is a subchannel key" is the consuming card's job (app convention).
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub, ServiceRefStub } from "@fxtr-ui/core";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import {
	type LoadResult,
	atomWithDefault,
	makeInvokeResultAtom,
	subAtom,
} from "@fxtr-ui/state-util";
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";
import { ChannelContentCard } from "./keys.js";

// ----- ChannelReaderView: reader → get_tag → dispatch the tag's card -----

const READER_VIEW_KIND = "channel-reader-view/v1";

interface ReaderViewStorage {
	readonly kind: typeof READER_VIEW_KIND;
	/** The `get_tag` invoke; its filled value is the tag this view dispatches on. */
	readonly getTagInvoke: OpResultStub<EntityRefStub> | null;
	/** Storage handed to the dispatched `ChannelContentCard`. */
	readonly card: unknown;
}

const READER_VIEW_INITIAL: ReaderViewStorage = {
	kind: READER_VIEW_KIND,
	getTagInvoke: null,
	card: null,
};

function isValidReaderViewStorage(s: ReaderViewStorage): boolean {
	return s?.kind === READER_VIEW_KIND;
}

export interface ChannelReaderViewProps {
	/** A channel **reader** service ref. */
	readonly reader: ServiceRefStub;
	/** Persistent storage slot (the `get_tag` invoke + the card's own state). */
	readonly ownStorage: FxtrSessionAtom<unknown>;
}

/**
 * Render any channel given its reader: fetch the tag (showing a "starting…"
 * placeholder while the channel hasn't started yet, or an aborted note if
 * it aborted before starting), then re-dispatch `ChannelContentCard` on the
 * tag's entity type (no-match → the generic card). The service-type
 * `FullPanel` wraps this in panel chrome; `SubchannelView` embeds it inline.
 */
export function ChannelReaderView({ reader, ownStorage }: ChannelReaderViewProps) {
	const typed = useMemo(
		() =>
			atomWithDefault<ReaderViewStorage>(
				ownStorage as FxtrSessionAtom<ReaderViewStorage | null>,
				READER_VIEW_INITIAL,
				isValidReaderViewStorage,
			),
		[ownStorage],
	);
	const tagResultAtom = useMemo(
		() =>
			makeInvokeResultAtom<EntityRefStub>(
				subAtom(typed, "getTagInvoke") as FxtrSessionAtom<OpResultStub<EntityRefStub> | null>,
				reader,
				"get_tag",
				[],
			),
		[typed, reader],
	);
	const tagResult: LoadResult<EntityRefStub> = useAtomValue(tagResultAtom);
	const cardSlot = useMemo(() => subAtom(typed, "card") as FxtrSessionAtom<unknown>, [typed]);

	if (tagResult.kind === "loading") {
		return <span style={mutedStyle}>starting…</span>;
	}
	if (tagResult.kind === "errored") {
		// `get_tag` raises only when the channel aborted before it ever started.
		return <div style={errorStyle}>channel never started: {tagResult.message}</div>;
	}
	return (
		<RenderDispatch
			stub={tagResult.value}
			as={ChannelContentCard}
			channel={reader}
			ownStorage={cardSlot}
		/>
	);
}

// ----- useSubchannelReader: key → open_subchannel_reader → child reader -----

const SUBREADER_KIND = "channel-subreader/v1";

interface SubreaderStorage {
	readonly kind: typeof SUBREADER_KIND;
	/** The `open_subchannel_reader(key)` invoke; its filled value is the child
	 * reader. Holding this slot in user state keeps the child reader service
	 * alive server-side (its result event's ref stays pinned). */
	readonly readerInvoke: OpResultStub<ServiceRefStub> | null;
}

const SUBREADER_INITIAL: SubreaderStorage = { kind: SUBREADER_KIND, readerInvoke: null };

function isValidSubreaderStorage(s: SubreaderStorage): boolean {
	return s?.kind === SUBREADER_KIND;
}

/**
 * Open a subchannel by key and observe the resulting child **reader** as a
 * `LoadResult<ServiceRefStub>`. The persisted invoke slot is what keeps the
 * child reader alive while held; an `errored` result means the parent has
 * finished/aborted (its subchannels are gone) — render it gracefully.
 */
export function useSubchannelReader(
	parentReader: ServiceRefStub,
	key: string,
	slotAtom: FxtrSessionAtom<unknown>,
): LoadResult<ServiceRefStub> {
	const typed = useMemo(
		() =>
			atomWithDefault<SubreaderStorage>(
				slotAtom as FxtrSessionAtom<SubreaderStorage | null>,
				SUBREADER_INITIAL,
				isValidSubreaderStorage,
			),
		[slotAtom],
	);
	const readerAtom = useMemo(
		() =>
			makeInvokeResultAtom<ServiceRefStub>(
				subAtom(typed, "readerInvoke") as FxtrSessionAtom<OpResultStub<ServiceRefStub> | null>,
				parentReader,
				"open_subchannel_reader",
				[key],
			),
		[typed, parentReader, key],
	);
	return useAtomValue(readerAtom);
}

// ----- SubchannelView: open + render, the headline consumer API -----

const SUBVIEW_KIND = "channel-subview/v1";

interface SubViewStorage {
	readonly kind: typeof SUBVIEW_KIND;
	/** `useSubchannelReader`'s slot (the open invoke). */
	readonly open: unknown;
	/** `ChannelReaderView`'s slot for the opened child. */
	readonly view: unknown;
}

const SUBVIEW_INITIAL: SubViewStorage = { kind: SUBVIEW_KIND, open: null, view: null };

function isValidSubViewStorage(s: SubViewStorage): boolean {
	return s?.kind === SUBVIEW_KIND;
}

export interface SubchannelViewProps {
	/** The parent channel reader the subchannel was spawned on. */
	readonly parentReader: ServiceRefStub;
	/** The subchannel key (an app convention recognized in a parent message,
	 * or surfaced by the debug subchannel feed). */
	readonly channelKey: string;
	/** Persistent storage (the open invoke + the child view's state). */
	readonly ownStorage: FxtrSessionAtom<unknown>;
}

/**
 * Open a subchannel by key and render it the same way any channel is
 * rendered. The headline consumer API for the new subchannel affordance:
 * a custom card that recognizes a key in its messages drops in a
 * `<SubchannelView parentReader={...} channelKey={key} ownStorage={slot} />`
 * and the recursion (open → `ChannelReaderView` → `get_tag` → card → …)
 * takes over.
 */
export function SubchannelView({ parentReader, channelKey, ownStorage }: SubchannelViewProps) {
	const typed = useMemo(
		() =>
			atomWithDefault<SubViewStorage>(
				ownStorage as FxtrSessionAtom<SubViewStorage | null>,
				SUBVIEW_INITIAL,
				isValidSubViewStorage,
			),
		[ownStorage],
	);
	const openSlot = useMemo(() => subAtom(typed, "open") as FxtrSessionAtom<unknown>, [typed]);
	const viewSlot = useMemo(() => subAtom(typed, "view") as FxtrSessionAtom<unknown>, [typed]);
	const child = useSubchannelReader(parentReader, channelKey, openSlot);

	if (child.kind === "loading") {
		return <span style={mutedStyle}>opening…</span>;
	}
	if (child.kind === "errored") {
		return <div style={mutedStyle}>subchannel unavailable: {child.message}</div>;
	}
	return <ChannelReaderView reader={child.value} ownStorage={viewSlot} />;
}

// ----- styles -----

const mutedStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
	fontSize: "var(--text-body-sm)",
};
const errorStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
