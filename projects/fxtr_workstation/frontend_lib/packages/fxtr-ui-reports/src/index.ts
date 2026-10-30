/**
 * Public surface of `@fxtr-ui/reports`.
 *
 * The `Figure` dispatch key + its fallbacks, the MDX tree
 * renderer + figure-storage helper, the per-type report
 * renderers (RootReport / SubReport), plus the
 * `makeLoadResultAtom` helper that powers each renderer's
 * load lifecycle.
 *
 * Consumers wire renderers into a dispatcher manually via
 * `createDispatchRegistry` from `@fxtr-ui/react-dispatch`; see
 * `LOCAL_CODE_OVERVIEW.md` for the canonical pattern.
 */

export { Figure, type FigureProps } from "./keys.js";
export {
	FigureErrorFallback,
	type FigureErrorFallbackProps,
	FigureLoadingFallback,
	FigureNoMatchFallback,
} from "./fallbacks.js";
export {
	type FigureStorageLookup,
	type FigureStorageMap,
	makeFigureStorage,
} from "./figure_storage.js";
// Re-exported from `@fxtr-ui/state-util` for back-compat — the
// helper used to live here under `./load_effect.js`. New consumers
// should import directly from `@fxtr-ui/state-util`.
export { type LoadResult, makeLoadResultAtom } from "@fxtr-ui/state-util";
export { ReportTreeRenderer, type ReportTreeRendererProps } from "./tree_renderer.js";
export type { MdxTree, RootReportValue, SubReportValue } from "./types.js";
export {
	RootReportBlockLink,
	RootReportFullPanel,
	RootReportHoverPreview,
	RootReportInlineLink,
} from "./handlers/RootReport.js";
export {
	SubReportBlockLink,
	SubReportFigure,
	SubReportFullPanel,
	SubReportHoverPreview,
	SubReportInlineLink,
} from "./handlers/SubReport.js";
