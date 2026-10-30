/**
 * Hardcoded metadata view components. Rendered by `PanelWrapper`
 * when the active panel-view entry has `display: "metadata"`,
 * bypassing the consumer's dispatch registry.
 */

export { EntityMetadataView, type EntityMetadataViewProps } from "./EntityMetadataView.js";
export { JsonRefTree, type JsonRefTreeProps, type RefLeafRenderer } from "./JsonRefTree.js";
export {
	ServiceMetadataBody,
	ServiceMetadataView,
	type ServiceMetadataViewProps,
} from "./ServiceMetadataView.js";
