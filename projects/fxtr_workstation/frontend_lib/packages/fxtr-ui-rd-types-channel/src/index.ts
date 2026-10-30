/**
 * Public surface of ``@fxtr-ui/rd-types-channel``.
 *
 * Frontend support for fxtr2 output channels (`fxtr2.common.channels`),
 * organized around rendering **any** channel from its **reader** — with no
 * knowledge of the consumer's message schema:
 *
 * - `ChannelReaderView` — the reusable unit: given a reader ref, fetch its
 *   tag (`get_tag`) and re-dispatch `ChannelContentCard` on the tag's
 *   entity type. `SubchannelView` / `useSubchannelReader` open a subchannel
 *   by key (`open_subchannel_reader`) and render the child the same way
 *   (the uniform recursion).
 * - The two tag-dispatched keys — `ChannelContentCard` (full live view) and
 *   `ChannelStatusPreview` (compact status) — plus their Props and default
 *   fallbacks. The content card's NoMatch is the generic channel card, so
 *   unregistered tag types still get a working live view + subchannel list.
 * - The wire types + pure folds (`foldChannelUpdates` / `latestChannelState`)
 *   and the subscription hooks (`useChannelSubscription` over
 *   `subscribe_full`, `useChannelStateSubscription` over `subscribe_state`,
 *   `useDebugSubchannels` over `subscribe_debug_subchannels`).
 * - `channelReaderRenderers` — the standard-workstation-key renderers for
 *   the fixed channel-**reader** service type, exported as a
 *   dispatch-key-uuid-keyed record. No `default` export: the consuming
 *   renderer slice picks the subtype key and wraps
 *   (`export default { channel_reader: channelReaderRenderers }`).
 */

export {
	ChannelContentCard,
	type ChannelContentCardProps,
	ChannelStatusPreview,
	type ChannelStatusPreviewProps,
} from "./keys.js";
export {
	type ChannelStateEventWire,
	type ChannelStateWire,
	type ChannelUpdateWire,
	type ChannelViewState,
	type SubchannelOpenedWire,
	foldChannelUpdates,
	latestChannelState,
} from "./types.js";
export {
	useChannelStateSubscription,
	useChannelSubscription,
	useDebugSubchannels,
} from "./subscription.js";
export {
	ChannelReaderView,
	type ChannelReaderViewProps,
	SubchannelView,
	type SubchannelViewProps,
	useSubchannelReader,
} from "./reader_view.js";
export {
	ChannelCardLoadingFallback,
	ChannelStatusPillFallback,
	ChannelStatusPreviewLoadingFallback,
	GenericChannelCard,
} from "./fallbacks.js";
export {
	ChannelReaderBlockLink,
	ChannelReaderFullPanel,
	ChannelReaderHoverPreview,
	ChannelReaderInlineLink,
	channelReaderRenderers,
} from "./reader_renderers.js";
