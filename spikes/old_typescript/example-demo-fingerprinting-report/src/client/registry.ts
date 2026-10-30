/**
 * Combined dispatch registry: standard report handlers + fingerprint-specific handlers.
 */

import type { DispatchHandler } from "@fxtr/react-dispatch";
import { createDispatchRegistry, entry } from "@fxtr/react-dispatch";
import { LinkedSVGFigure, RootReport, SimpleComment, SubReport } from "@fxtr/reports-entities";
import { FigureBlock, MainView, MarginNote, ReferenceLink, SummaryCard } from "@fxtr/reports-react";
import {
	GenericFallback,
	LinkedSVGFigureFigureBlock,
	LinkedSVGFigureMainView,
	LinkedSVGFigureReferenceLink,
	RootReportMainView,
	RootReportReferenceLink,
	RootReportSummaryCard,
	SimpleCommentMainView,
	SimpleCommentMarginNote,
	SimpleCommentReferenceLink,
	SubReportFigureBlock,
	SubReportMainView,
	SubReportReferenceLink,
} from "@fxtr/reports-react-handlers";
import {
	CategoryAggregate,
	FingerprintGraderResult,
	FingerprintedTranscript,
	RubricText,
	SimulatorBio,
	Transcript,
} from "../entities.js";
import {
	CategoryAggregateFigureBlock,
	CategoryAggregateMainView,
	CategoryAggregateReferenceLink,
} from "./handlers/CategoryAggregate.js";
import {
	FingerprintGraderResultFigureBlock,
	FingerprintGraderResultMainView,
	FingerprintGraderResultReferenceLink,
} from "./handlers/FingerprintGraderResult.js";
import {
	RubricTextFigureBlock,
	RubricTextMainView,
	RubricTextReferenceLink,
} from "./handlers/RubricText.js";
import {
	SimulatorBioFigureBlock,
	SimulatorBioMainView,
	SimulatorBioReferenceLink,
} from "./handlers/SimulatorBio.js";
import {
	TranscriptFigureBlock,
	TranscriptMainView,
	TranscriptReferenceLink,
} from "./handlers/Transcript.js";
import {
	FingerprintedTranscriptFigureBlock,
	FingerprintedTranscriptMainView,
	FingerprintedTranscriptReferenceLink,
} from "./handlers/TranscriptView.js";

export function createFingerprintDispatchHandler(): DispatchHandler {
	return createDispatchRegistry(
		[
			// ---- Standard report entities ----
			entry(MainView, RootReport, RootReportMainView),
			entry(ReferenceLink, RootReport, RootReportReferenceLink),
			entry(SummaryCard, RootReport, RootReportSummaryCard),

			entry(MainView, SubReport, SubReportMainView),
			entry(FigureBlock, SubReport, SubReportFigureBlock),
			entry(ReferenceLink, SubReport, SubReportReferenceLink),

			entry(MarginNote, SimpleComment, SimpleCommentMarginNote),
			entry(MainView, SimpleComment, SimpleCommentMainView),
			entry(ReferenceLink, SimpleComment, SimpleCommentReferenceLink),

			entry(MainView, LinkedSVGFigure, LinkedSVGFigureMainView),
			entry(FigureBlock, LinkedSVGFigure, LinkedSVGFigureFigureBlock),
			entry(ReferenceLink, LinkedSVGFigure, LinkedSVGFigureReferenceLink),

			// ---- Fingerprint-specific entities ----
			entry(MainView, CategoryAggregate, CategoryAggregateMainView),
			entry(FigureBlock, CategoryAggregate, CategoryAggregateFigureBlock),
			entry(ReferenceLink, CategoryAggregate, CategoryAggregateReferenceLink),

			entry(MainView, RubricText, RubricTextMainView),
			entry(FigureBlock, RubricText, RubricTextFigureBlock),
			entry(ReferenceLink, RubricText, RubricTextReferenceLink),

			entry(MainView, Transcript, TranscriptMainView),
			entry(FigureBlock, Transcript, TranscriptFigureBlock),
			entry(ReferenceLink, Transcript, TranscriptReferenceLink),

			entry(MainView, FingerprintedTranscript, FingerprintedTranscriptMainView),
			entry(FigureBlock, FingerprintedTranscript, FingerprintedTranscriptFigureBlock),
			entry(ReferenceLink, FingerprintedTranscript, FingerprintedTranscriptReferenceLink),

			entry(MainView, FingerprintGraderResult, FingerprintGraderResultMainView),
			entry(FigureBlock, FingerprintGraderResult, FingerprintGraderResultFigureBlock),
			entry(ReferenceLink, FingerprintGraderResult, FingerprintGraderResultReferenceLink),

			entry(MainView, SimulatorBio, SimulatorBioMainView),
			entry(FigureBlock, SimulatorBio, SimulatorBioFigureBlock),
			entry(ReferenceLink, SimulatorBio, SimulatorBioReferenceLink),
		],
		{ fallback: GenericFallback },
	);
}
