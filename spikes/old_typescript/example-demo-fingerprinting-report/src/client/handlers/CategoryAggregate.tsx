/**
 * Dispatch handlers for CategoryAggregate: MainView, FigureBlock, ReferenceLink.
 *
 * MainView: header, rubric bubble, transcript list, and navState-selected preview below.
 * FigureBlock: same header + rubric bubble + transcript list, but clicking navigates
 * to main view rather than selecting navState. No drill-down previews below.
 */

import type { FxtrID } from "@fxtr/core";
import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { Render } from "@fxtr/react-dispatch";
import type { JsonValue } from "@fxtr/reports-react";
import { FigureBlock, useReportNavigation, vtName } from "@fxtr/reports-react";
import { Suspense, useCallback } from "react";
import type { CategoryAggregate } from "../../entities.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const headerStyle: React.CSSProperties = {
	marginBottom: "16px",
};

const titleStyle: React.CSSProperties = {
	fontSize: "24px",
	fontWeight: 700,
	marginBottom: "4px",
};

const dimensionBadgeStyle: React.CSSProperties = {
	display: "inline-block",
	fontSize: "12px",
	fontWeight: 600,
	padding: "2px 8px",
	borderRadius: "4px",
	backgroundColor: "#e8f0fe",
	color: "#1a73e8",
	marginBottom: "8px",
};

const statsStyle: React.CSSProperties = {
	color: "#666",
	fontSize: "14px",
	marginBottom: "16px",
};

const rubricBubbleStyle: React.CSSProperties = {
	border: "1px solid #d0d7de",
	borderRadius: "8px",
	padding: "12px 16px",
	backgroundColor: "#f6f8fa",
	marginBottom: "20px",
	cursor: "pointer",
	transition: "border-color 0.15s",
};

const rubricBubbleSelectedStyle: React.CSSProperties = {
	...rubricBubbleStyle,
	borderColor: "#0066cc",
	boxShadow: "0 0 0 1px #0066cc",
};

const rubricLabelStyle: React.CSSProperties = {
	fontSize: "12px",
	fontWeight: 600,
	color: "#666",
	marginBottom: "4px",
};

const transcriptListStyle: React.CSSProperties = {
	maxHeight: "400px",
	overflowY: "auto",
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
};

const transcriptItemStyle: React.CSSProperties = {
	padding: "8px 12px",
	borderBottom: "1px solid #f0f0f0",
	cursor: "pointer",
	fontSize: "13px",
	display: "flex",
	justifyContent: "space-between",
	alignItems: "center",
	transition: "background-color 0.1s",
};

const transcriptItemSelectedStyle: React.CSSProperties = {
	...transcriptItemStyle,
	backgroundColor: "#e8f0fe",
};

const selectedSectionStyle: React.CSSProperties = {
	marginTop: "20px",
	paddingTop: "16px",
	borderTop: "1px solid #e0e0e0",
};

const selectedLabelStyle: React.CSSProperties = {
	fontSize: "13px",
	fontWeight: 600,
	color: "#666",
	marginBottom: "8px",
};

const figureCardStyle: React.CSSProperties = {
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
	padding: "12px 16px",
	margin: "8px 0",
	backgroundColor: "#fafafa",
};

const linkStyle: React.CSSProperties = {
	color: "#0066cc",
	cursor: "pointer",
	textDecoration: "none",
	borderBottom: "1px solid transparent",
};

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function CategoryAggregateMainView({
	entityRef,
	navState,
}: {
	entityRef: EntityRef<CategoryAggregate>;
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}) {
	const cat = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

	const selectedKey = typeof navState === "string" ? navState : undefined;
	const isRubricSelected = selectedKey === "rubric";

	const selectedTranscriptId: FxtrID | undefined =
		selectedKey && selectedKey !== "rubric"
			? cat.transcripts.find((_, i) => `t${i}` === selectedKey)
			: undefined;

	const handleRubricClick = useCallback(() => {
		nav.navigateTo(entityRef.entityId, "rubric");
	}, [nav, entityRef.entityId]);

	const handleTranscriptClick = useCallback(
		(index: number) => {
			nav.navigateTo(entityRef.entityId, `t${index}`);
		},
		[nav, entityRef.entityId],
	);

	return (
		<div style={{ maxWidth: "800px", viewTransitionName: vtName(entityRef.entityId) }}>
			<div style={headerStyle}>
				<div style={dimensionBadgeStyle}>{cat.dimensionName}</div>
				<div style={titleStyle}>{cat.label}</div>
				<div style={statsStyle}>
					{(cat.prevalence * 100).toFixed(1)}% prevalence &middot; {cat.transcriptCount} transcripts
				</div>
			</div>

			{/* Rubric bubble */}
			<div
				style={isRubricSelected ? rubricBubbleSelectedStyle : rubricBubbleStyle}
				onClick={handleRubricClick}
				onKeyDown={(e) => {
					if (e.key === "Enter") handleRubricClick();
				}}
				role="button"
				tabIndex={0}
			>
				<div style={rubricLabelStyle}>Dimension Rubric</div>
				{cat.rubricExcerpt ? (
					<div style={{ fontSize: "13px", color: "#333", lineHeight: 1.5 }}>
						{cat.rubricExcerpt}
					</div>
				) : (
					<div style={{ fontSize: "13px", color: "#999" }}>
						Click to view the full judging rubric
					</div>
				)}
			</div>

			{/* Transcript list */}
			<div style={{ fontSize: "13px", fontWeight: 600, color: "#666", marginBottom: "8px" }}>
				Matching Transcripts ({cat.transcriptCount})
			</div>
			<div style={transcriptListStyle}>
				{cat.transcripts.map((tid, i) => (
					<div
						key={tid.key()}
						style={`t${i}` === selectedKey ? transcriptItemSelectedStyle : transcriptItemStyle}
						onClick={() => handleTranscriptClick(i)}
						onKeyDown={(e) => {
							if (e.key === "Enter") handleTranscriptClick(i);
						}}
						role="button"
						tabIndex={0}
					>
						<span>Transcript {tid.idString().slice(0, 16)}...</span>
						<span style={{ color: "#999", fontSize: "11px" }}>#{i + 1}</span>
					</div>
				))}
			</div>

			{/* Selected preview below */}
			{isRubricSelected && (
				<div style={selectedSectionStyle}>
					<div style={selectedLabelStyle}>Rubric:</div>
					<Suspense fallback={<div>Loading rubric...</div>}>
						<Render entity={cat.rubric} as={FigureBlock} />
					</Suspense>
				</div>
			)}

			{selectedTranscriptId && (
				<div style={selectedSectionStyle}>
					<div style={selectedLabelStyle}>Transcript Preview:</div>
					<Suspense fallback={<div>Loading transcript...</div>}>
						<Render entity={selectedTranscriptId} as={FigureBlock} />
					</Suspense>
				</div>
			)}
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock — rich preview: same header + rubric + transcript list, no drill-down
// ---------------------------------------------------------------------------

export function CategoryAggregateFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<CategoryAggregate>;
}) {
	const cat = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

	return (
		<div style={{ ...figureCardStyle, viewTransitionName: vtName(entityRef.entityId) }}>
			<div
				style={{ ...headerStyle, cursor: "pointer" }}
				onClick={() => nav.navigateTo(entityRef.entityId)}
				onKeyDown={(e) => {
					if (e.key === "Enter") nav.navigateTo(entityRef.entityId);
				}}
				role="button"
				tabIndex={0}
			>
				<div style={dimensionBadgeStyle}>{cat.dimensionName}</div>
				<div style={{ ...titleStyle, fontSize: "18px" }}>{cat.label}</div>
				<div style={{ ...statsStyle, fontSize: "13px" }}>
					{(cat.prevalence * 100).toFixed(1)}% prevalence &middot; {cat.transcriptCount} transcripts
				</div>
			</div>

			{/* Rubric bubble — clicks navigate to main view with rubric selected */}
			<div
				style={{ ...rubricBubbleStyle, cursor: "pointer" }}
				onClick={() => nav.navigateTo(entityRef.entityId, "rubric")}
				onKeyDown={(e) => {
					if (e.key === "Enter") nav.navigateTo(entityRef.entityId, "rubric");
				}}
				role="button"
				tabIndex={0}
			>
				<div style={rubricLabelStyle}>Dimension Rubric</div>
				{cat.rubricExcerpt ? (
					<div style={{ fontSize: "13px", color: "#333", lineHeight: 1.5 }}>
						{cat.rubricExcerpt}
					</div>
				) : (
					<div style={{ fontSize: "13px", color: "#999" }}>
						Click to view the full judging rubric
					</div>
				)}
			</div>

			{/* Transcript list — each item navigates to main view with that transcript selected */}
			<div style={{ fontSize: "12px", fontWeight: 600, color: "#666", marginBottom: "6px" }}>
				Matching Transcripts ({cat.transcriptCount})
			</div>
			<div style={{ ...transcriptListStyle, maxHeight: "200px" }}>
				{cat.transcripts.map((tid, i) => (
					<div
						key={tid.key()}
						style={{ ...transcriptItemStyle, fontSize: "12px" }}
						onClick={() => nav.navigateTo(entityRef.entityId, `t${i}`)}
						onKeyDown={(e) => {
							if (e.key === "Enter") nav.navigateTo(entityRef.entityId, `t${i}`);
						}}
						role="button"
						tabIndex={0}
					>
						<span>Transcript {tid.idString().slice(0, 16)}...</span>
						<span style={{ color: "#999", fontSize: "10px" }}>#{i + 1}</span>
					</div>
				))}
			</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function CategoryAggregateReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<CategoryAggregate>;
	linkText?: string;
}) {
	const cat = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? cat.label;

	return (
		<span
			style={linkStyle}
			onClick={() => nav.navigateTo(entityRef.entityId)}
			onKeyDown={(e) => {
				if (e.key === "Enter") nav.navigateTo(entityRef.entityId);
			}}
			role="link"
			tabIndex={0}
		>
			{text}
		</span>
	);
}
