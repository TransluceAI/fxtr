import { ANY_SUBJECT_TYPE, EntityPanel, EntityHoverPreview, EntityInlineLink, renderer, type AnyRenderer } from "fxtr-view";
import { CborTreeHoverPreview, CborTreeInlineLink, CborTreePanel } from "./views";

export { DataTree, type CollapsedPaths } from "./tree";
export {
  CborTreeHoverPreview,
  CborTreeInlineLink,
  CborTreePanel,
  CompactValue,
  PREVIEW_FIELD_LIMIT,
} from "./views";
export { shortName } from "./names";

/** The id the generic entity views go by in a host's switcher. */
export const ENTITY_DATA = "entity_data";

/** The generic views as wildcard renderers of the built-in entity slots, so every type has a
 *  panel (a data tree), an inline link (type plus cid), and a preview (type, cid, and first
 *  fields). Being wildcards they follow a type's own renderers, so they are the default
 *  only for a type without any. */
export const cborTreeRenderers: readonly AnyRenderer[] = [
  renderer(EntityPanel, ANY_SUBJECT_TYPE, ENTITY_DATA, CborTreePanel),
  renderer(EntityInlineLink, ANY_SUBJECT_TYPE, ENTITY_DATA, CborTreeInlineLink),
  renderer(EntityHoverPreview, ANY_SUBJECT_TYPE, ENTITY_DATA, CborTreeHoverPreview),
];
