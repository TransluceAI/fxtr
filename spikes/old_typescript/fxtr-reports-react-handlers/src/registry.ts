/**
 * Assembles a DispatchHandler for the standard report entity renderers.
 */

import type { DispatchHandler } from "@fxtr/react-dispatch";
import { createDispatchRegistry, entry } from "@fxtr/react-dispatch";
import { LinkedSVGFigure, RootReport, SimpleComment, SubReport } from "@fxtr/reports-entities";
import { FigureBlock, MainView, MarginNote, ReferenceLink, SummaryCard } from "@fxtr/reports-react";
import { GenericFallback } from "./handlers/GenericFallback.js";
import {
	LinkedSVGFigureFigureBlock,
	LinkedSVGFigureMainView,
	LinkedSVGFigureReferenceLink,
} from "./handlers/LinkedSVGFigure.js";
import {
	RootReportMainView,
	RootReportReferenceLink,
	RootReportSummaryCard,
} from "./handlers/RootReport.js";
import {
	SimpleCommentMainView,
	SimpleCommentMarginNote,
	SimpleCommentReferenceLink,
} from "./handlers/SimpleComment.js";
import {
	SubReportFigureBlock,
	SubReportMainView,
	SubReportReferenceLink,
} from "./handlers/SubReport.js";

/**
 * Create a DispatchHandler with renderers for all standard report entity types.
 */
export function createReportDispatchHandler(): DispatchHandler {
	return createDispatchRegistry(
		[
			// RootReport
			entry(MainView, RootReport, RootReportMainView),
			entry(ReferenceLink, RootReport, RootReportReferenceLink),
			entry(SummaryCard, RootReport, RootReportSummaryCard),

			// SubReport
			entry(MainView, SubReport, SubReportMainView),
			entry(FigureBlock, SubReport, SubReportFigureBlock),
			entry(ReferenceLink, SubReport, SubReportReferenceLink),

			// SimpleComment
			entry(MarginNote, SimpleComment, SimpleCommentMarginNote),
			entry(MainView, SimpleComment, SimpleCommentMainView),
			entry(ReferenceLink, SimpleComment, SimpleCommentReferenceLink),

			// LinkedSVGFigure
			entry(MainView, LinkedSVGFigure, LinkedSVGFigureMainView),
			entry(FigureBlock, LinkedSVGFigure, LinkedSVGFigureFigureBlock),
			entry(ReferenceLink, LinkedSVGFigure, LinkedSVGFigureReferenceLink),
		],
		{ fallback: GenericFallback },
	);
}
