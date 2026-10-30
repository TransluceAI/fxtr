/**
 * Report renderer for the demo's `RootReport` / `SubReport` entities.
 *
 * Pure re-export of the `@fxtr-ui/reports` handlers, registered under the report
 * type ids (the `forType`s in this dir's `fxtr_renderer.nix` come from
 * `lib.fxtr.entities.{rootReport,subReport}`). `RootReportFullPanel` walks the
 * MDX `contents` tree and dispatches each `RenderFigure` through the `Figure`
 * key; `SubReportFigure` renders a nested subreport inline, while Note/Stats
 * figures fall through to the `Figure` no-match fallback (their own FullPanel).
 *
 * The `default` export is the 2-level map `{subtype_key: {dispatch_key_uuid:
 * Component}}` the dynamic dispatcher resolves against.
 */

import {
	Figure,
	RootReportBlockLink,
	RootReportFullPanel,
	RootReportHoverPreview,
	RootReportInlineLink,
	SubReportBlockLink,
	SubReportFigure,
	SubReportFullPanel,
	SubReportHoverPreview,
	SubReportInlineLink,
} from "@fxtr-ui/reports";
import { BlockLink, FullPanel, HoverPreview, InlineLink } from "@fxtr-ui/workstation-dispatch";

export default {
	root_report: {
		[FullPanel.uuid]: RootReportFullPanel,
		[BlockLink.uuid]: RootReportBlockLink,
		[InlineLink.uuid]: RootReportInlineLink,
		[HoverPreview.uuid]: RootReportHoverPreview,
	},
	sub_report: {
		[FullPanel.uuid]: SubReportFullPanel,
		[BlockLink.uuid]: SubReportBlockLink,
		[InlineLink.uuid]: SubReportInlineLink,
		[HoverPreview.uuid]: SubReportHoverPreview,
		[Figure.uuid]: SubReportFigure,
	},
};
