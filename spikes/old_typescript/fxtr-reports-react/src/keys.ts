/**
 * Dispatch keys for the report rendering system.
 *
 * Each key represents a rendering slot that entity types can fill.
 * The Props type on each key defines the extra props (beyond entityId)
 * that the dispatched component receives.
 */

import type { FxtrID } from "@fxtr/core";
import { declareDispatchKey } from "@fxtr/react-dispatch";
import type { JsonValue } from "./types.js";

/**
 * Main view: how an entity renders when it is the primary thing on the page.
 *
 * - navState: arbitrary JSON stored in navigation state (e.g. selected item)
 * - annotations: entity IDs of annotations that apply to this entity
 */
export const MainView = declareDispatchKey<{
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}>("reports:MainView");

/** Figure block: how an entity renders as a full-width section inside another entity's main view. */
export const FigureBlock = declareDispatchKey<object>("reports:FigureBlock");

/**
 * Reference link: how an entity renders as an inline rich link.
 *
 * - linkText: optional override for the link text
 */
export const ReferenceLink = declareDispatchKey<{
	linkText?: string;
}>("reports:ReferenceLink");

/**
 * Margin note: how an entity renders in the right sidebar.
 *
 * - highlighted: visual emphasis (e.g. for the currently focused annotation)
 */
export const MarginNote = declareDispatchKey<{
	highlighted?: boolean;
}>("reports:MarginNote");

/** Summary card: how an entity renders as a small preview card. */
export const SummaryCard = declareDispatchKey<object>("reports:SummaryCard");
