/**
 * createWorkstationDispatchHandler — thin wrapper around
 * createDispatchRegistry that appends catch-all fallback entries for
 * the four workstation dispatch keys.
 *
 * User entries are tried first in declaration order (per registry.tsx's
 * per-key grouping). Workstation fallbacks — registered last with
 * `unknownLoadCodec`, which returns `true` for every type — act as
 * catch-alls.
 *
 * The registry is **pure display**: it does not attach interactivity
 * to BlockLink / InlineLink entries. Consumers who want an interactive
 * ref reach for `<EntityLink>`, which composes this dispatch path with
 * the interaction hooks; non-interactive surfaces (e.g. the open-
 * panels sidebar) render `<Render as={BlockLink}>` directly and skip
 * the interactions.
 */

import { unknownLoadCodec } from "@fxtr/core";
import {
	type DispatchHandler,
	type RegistryEntry,
	type RegistryOptions,
	createDispatchRegistry,
	entry,
} from "@fxtr/react-dispatch";
import { BlockLinkFallback } from "./fallbacks/BlockLinkFallback.js";
import { FullPanelFallback } from "./fallbacks/FullPanelFallback.js";
import { HoverPreviewFallback } from "./fallbacks/HoverPreviewFallback.js";
import { InlineLinkFallback } from "./fallbacks/InlineLinkFallback.js";
import { BlockLink, FullPanel, HoverPreview, InlineLink } from "./keys.js";

export function createWorkstationDispatchHandler<Meta = unknown>(
	// biome-ignore lint/suspicious/noExplicitAny: entries have heterogeneous types
	userEntries: RegistryEntry<any, any, Meta>[],
	options?: RegistryOptions<Meta>,
): DispatchHandler<Meta> {
	return createDispatchRegistry(
		[
			...userEntries,
			entry(FullPanel, unknownLoadCodec, FullPanelFallback),
			entry(BlockLink, unknownLoadCodec, BlockLinkFallback),
			entry(InlineLink, unknownLoadCodec, InlineLinkFallback),
			entry(HoverPreview, unknownLoadCodec, HoverPreviewFallback),
		],
		options,
	);
}
