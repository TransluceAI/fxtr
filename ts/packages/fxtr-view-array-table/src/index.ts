import { ARRAY_TYPE, EntityInlineLink, EntityPanel, renderer, type AnyRenderer } from "fxtr-view";
import { ArrayInlineLink, ArrayTablePanel } from "./views";

export { ArrayInlineLink, ArrayTablePanel, Cell } from "./views";

/** The Array renderers, for a registry to concatenate: the table panel and the inline link. */
export const arrayTableRenderers: readonly AnyRenderer[] = [
  renderer(EntityPanel, ARRAY_TYPE, "table", ArrayTablePanel),
  renderer(EntityInlineLink, ARRAY_TYPE, "default", ArrayInlineLink),
];
