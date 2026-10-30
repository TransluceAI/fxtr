/**
 * Derive a CSS-safe view-transition-name from an FxtrID.
 *
 * The name is used as a `view-transition-name` to match shared elements
 * across navigations — e.g. a SummaryCard's hero element matches
 * the corresponding MainView's hero element.
 *
 * @param entityId - The entity whose transition name to compute
 * @param suffix - Optional suffix to distinguish multiple named regions
 *                 within the same entity (e.g. "title", "figure")
 */
import type { FxtrID } from "@fxtr/core";

export function vtName(entityId: FxtrID, suffix?: string): string {
	// FxtrID.idString() may contain characters invalid for CSS <custom-ident>.
	// Replace non-alphanumeric/non-hyphen/non-underscore with hyphens.
	const base = `fxtr-${entityId.idString().replace(/[^a-zA-Z0-9_-]/g, "-")}`;
	return suffix ? `${base}--${suffix}` : base;
}
