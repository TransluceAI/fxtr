// Registry
export { createReportDispatchHandler } from "./registry.js";

// Tree renderer
export { ReportTreeRenderer, type ReportTreeRendererProps } from "./treeRenderer.js";

// Individual handlers (for advanced use / custom registries)
export {
	RootReportMainView,
	RootReportReferenceLink,
	RootReportSummaryCard,
} from "./handlers/RootReport.js";
export {
	SubReportMainView,
	SubReportFigureBlock,
	SubReportReferenceLink,
} from "./handlers/SubReport.js";
export {
	SimpleCommentMarginNote,
	SimpleCommentMainView,
	SimpleCommentReferenceLink,
} from "./handlers/SimpleComment.js";
export {
	LinkedSVGFigureMainView,
	LinkedSVGFigureFigureBlock,
	LinkedSVGFigureReferenceLink,
} from "./handlers/LinkedSVGFigure.js";
export { GenericFallback } from "./handlers/GenericFallback.js";
