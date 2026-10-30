/**
 * Default renderers for fxtr system-defined entity types.
 * Currently houses just the `FxtrBasicType` badge; the
 * directory exists so future system-type renderers (e.g. for
 * `FallbackContextMarker`) land in a single discoverable place.
 */

export {
	BASIC_TYPE_ID_STRING,
	BasicTypeBlockLink,
	BasicTypeInlineLink,
} from "./BasicTypeBadge.js";
