/**
 * The experiment **dashboard** renderer module — the workstation panel
 * for `fxtr_experiment`'s user capability (`experiment-workflow`), per
 * `projects/fxtr_experiment/notes/monitoring_ui.md`.
 *
 * This file is the dynamic-dispatch renderer entry: the default export
 * is the 2-level `{subtype_key: {dispatch_key_uuid: Component}}` map,
 * esbuild-bundled by the repo nix lib's slice against the standard
 * curated externals — so everything here must be reachable by relative
 * import, and bare imports are limited to the externals set (react,
 * jotai, jotai-effect, `@fxtr-ui/{core,state-util,react,react-dispatch,
 * workstation-dispatch}`) plus `elkjs` (bundled INTO the slice, not
 * externalized — the plate view's layout engine).
 */

import { BlockLink, FullPanel, HoverPreview, InlineLink } from "@fxtr-ui/workstation-dispatch";

import { gridConfigRenderers, sliceTemplateRenderers } from "./config_entities.js";
import { FrameFullPanel, FrameHoverPreview } from "./frame_table.js";
import { DashboardBlockLink, DashboardHoverPreview, DashboardInlineLink } from "./links.js";
import { ExperimentDashboardPanel } from "./panel.js";

export { gridConfigRenderers, sliceTemplateRenderers } from "./config_entities.js";
export { ExperimentDashboardPanel } from "./panel.js";
export {
	DashboardBlockLink,
	DashboardHoverPreview,
	DashboardInlineLink,
} from "./links.js";
export {
	FrameFullPanel,
	FrameHoverPreview,
	FrameTableView,
	decodeFrameValue,
	formatFrameCell,
} from "./frame_table.js";
export type { ExperimentWorkflowProto } from "./wire/proto.js";
export * from "./wire/types.js";
export { lintClean, lintSound, viewSel } from "./wire/helpers.js";

export default {
	experiment_workflow: {
		[FullPanel.uuid]: ExperimentDashboardPanel,
		[BlockLink.uuid]: DashboardBlockLink,
		[InlineLink.uuid]: DashboardInlineLink,
		[HoverPreview.uuid]: DashboardHoverPreview,
	},
	// The canonical frame entity (`fxtr_experiment/frame_encoding.py`,
	// type `frame_v0`): a table wherever a frame ref renders. The cell
	// drawer's inline table is a static import, independent of dispatch.
	// Link slots deliberately fall back to the generic entity links.
	frame_v0: {
		[FullPanel.uuid]: FrameFullPanel,
		[HoverPreview.uuid]: FrameHoverPreview,
	},
	// The stored config entities (`fxtr_experiment/encoding.py`, types
	// `grid_config_v0` / `slice_template_v0`): a grid's identity-bearing
	// config and the standalone slice-template form (config_entities.tsx).
	grid_config: gridConfigRenderers,
	slice_template: sliceTemplateRenderers,
};
