/**
 * The two channel dispatch keys.
 *
 * Both dispatch on the **tag entity stub** — that's the point of the
 * channel design: every channel reader shares one fixed service
 * type id, and per-stream-type rendering customization keys off the
 * *tag*'s entity type instead. The channel-reader service renderers
 * (`reader_renderers.tsx`) fetch `get_tag()` to learn the tag, then
 * re-dispatch through these keys with the reader ref riding along
 * as a prop (the overview's "tag-type double dispatch"). Entity-only
 * keys: the dispatched stub is always the tag.
 *
 * UUIDs are wire-stable. Persisted dispatcher state keys by
 * `(uuid, stub.shortId)`, so changing one is a breaking change for
 * any session carrying channel-keyed entries.
 */

import type { EntityRefStub, FxtrSessionAtom, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchKey,
	type ErrorFallbackComponent,
	declareEntityDispatchKey,
} from "@fxtr-ui/react-dispatch";
import { DispatchErrorFallback } from "@fxtr-ui/workstation-dispatch";
import {
	ChannelCardLoadingFallback,
	ChannelStatusPillFallback,
	ChannelStatusPreviewLoadingFallback,
	GenericChannelCard,
} from "./fallbacks.js";
import type { ChannelStateEventWire } from "./types.js";

/**
 * `DispatchErrorFallback` reads `stub` + `error` only and ignores the
 * key's extra Props at render time, so the per-key cast is sound —
 * same pattern as the workstation-dispatch keys.
 */
function castErrorFallback<Props extends object>(): ErrorFallbackComponent<EntityRefStub, Props> {
	return DispatchErrorFallback as unknown as ErrorFallbackComponent<EntityRefStub, Props>;
}

/**
 * Props for a `ChannelContentCard`-dispatched component beyond
 * `stub` (the channel's tag). The card owns the live view of one
 * channel: subscribe to `channel`, fold the events, render.
 */
export interface ChannelContentCardProps {
	/** The channel **reader** service to subscribe to (already resolved by
	 * `ChannelReaderView`, which fetched the tag this card is dispatched on). */
	readonly channel: ServiceRefStub;
	/**
	 * Per-panel persistent storage carved out by the dispatching
	 * renderer (`ChannelReaderView` hands the card a `subAtom`-carved
	 * slot of its own `ownStorage`), so a card can persist its
	 * subscription slots the way any panel renderer does — typically by
	 * handing it to `useChannelSubscription`.
	 */
	readonly ownStorage: FxtrSessionAtom<unknown>;
}

/**
 * Props for a `ChannelStatusPreview`-dispatched component beyond
 * `stub` (the channel's tag). Compact display contexts (links,
 * hover previews); the dispatching renderer subscribes to the cheap
 * **state** stream and passes the latest event live.
 */
export interface ChannelStatusPreviewProps {
	/** Latest state-stream event (or `null` before the first lands) — the
	 * dispatching renderer drives this via `useChannelStateSubscription`. */
	readonly state: ChannelStateEventWire | null;
	/** The channel **reader** service. */
	readonly channel: ServiceRefStub;
}

// Hardcoded UUIDs. Wire-stable; do not change.
const UUID_CHANNEL_CONTENT_CARD = "830e2987-08a7-410f-8c23-e01f05878c50";
const UUID_CHANNEL_STATUS_PREVIEW = "beb2e1df-699a-4a90-b5fb-490382ba2628";

/**
 * Full live view of one channel, dispatched on the tag's entity
 * type. NoMatch is the **generic stream card** (`GenericChannelCard`)
 * — an unregistered tag type still gets a working message-log /
 * final-value view, not an error state.
 */
export const ChannelContentCard: DispatchKey<ChannelContentCardProps, "entity"> =
	declareEntityDispatchKey<ChannelContentCardProps>({
		uuid: UUID_CHANNEL_CONTENT_CARD,
		description: "ChannelContentCard",
		fallbacks: {
			Loading: ChannelCardLoadingFallback,
			NoMatch: GenericChannelCard,
			Error: castErrorFallback<ChannelContentCardProps>(),
		},
	});

/**
 * Compact status view of one channel, dispatched on the tag's
 * entity type. NoMatch is the generic status pill (tag reflink +
 * lifecycle badge).
 *
 * TODO(stage13 followups): the preview gets no hint about which
 * display style invoked it (block link / inline link / hover
 * preview); an optional prop can be added compatibly if per-style
 * rendering turns out to matter.
 */
export const ChannelStatusPreview: DispatchKey<ChannelStatusPreviewProps, "entity"> =
	declareEntityDispatchKey<ChannelStatusPreviewProps>({
		uuid: UUID_CHANNEL_STATUS_PREVIEW,
		description: "ChannelStatusPreview",
		fallbacks: {
			Loading: ChannelStatusPreviewLoadingFallback,
			NoMatch: ChannelStatusPillFallback,
			Error: castErrorFallback<ChannelStatusPreviewProps>(),
		},
	});
