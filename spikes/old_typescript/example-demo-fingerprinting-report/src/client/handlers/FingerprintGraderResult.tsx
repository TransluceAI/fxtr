/**
 * Dispatch handlers for FingerprintGraderResult: MainView, FigureBlock, ReferenceLink.
 *
 * FigureBlock: compact card with active category badges, color-coded by dimension.
 * MainView: full reasoning + all categories (active highlighted) + link to rubric.
 */

import type { FxtrID } from "@fxtr/core";
import { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { Render } from "@fxtr/react-dispatch";
import type { JsonValue } from "@fxtr/reports-react";
import { FigureBlock, useReportNavigation, vtName } from "@fxtr/reports-react";
import { Suspense } from "react";
import { dimColor } from "../../dimColors.js";
import { type FingerprintGraderResult, Transcript } from "../../entities.js";
import { MessageBubbles } from "./shared.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const figureCardStyle: React.CSSProperties = {
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
	padding: "10px 14px",
	backgroundColor: "#fafafa",
	cursor: "pointer",
};

const linkStyle: React.CSSProperties = {
	color: "#0066cc",
	cursor: "pointer",
	textDecoration: "none",
	borderBottom: "1px solid transparent",
};

const reasoningStyle: React.CSSProperties = {
	whiteSpace: "pre-wrap",
	fontSize: "13px",
	lineHeight: 1.6,
	color: "#333",
	backgroundColor: "#f8f9fa",
	padding: "16px 20px",
	borderRadius: "8px",
	border: "1px solid #e0e0e0",
};

// ---------------------------------------------------------------------------
// Shared: category badges
// ---------------------------------------------------------------------------

function CategoryBadges({
	activeCategories,
	allCategories,
	color,
	showAll,
}: {
	activeCategories: string[];
	allCategories?: string[];
	color: string;
	showAll?: boolean;
}) {
	const activeSet = new Set(activeCategories);
	const cats = showAll && allCategories ? allCategories : activeCategories;

	if (cats.length === 0 && !showAll) {
		return (
			<span style={{ fontSize: "12px", color: "#999", fontStyle: "italic" }}>none active</span>
		);
	}

	return (
		<div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
			{cats.map((cat) => {
				const isActive = activeSet.has(cat);
				return (
					<span
						key={cat}
						style={{
							display: "inline-block",
							fontSize: "11px",
							padding: "2px 6px",
							borderRadius: "3px",
							backgroundColor: isActive ? color : "#f0f0f0",
							color: isActive ? "#fff" : "#999",
							fontFamily: "monospace",
							whiteSpace: "nowrap",
						}}
					>
						{cat.replaceAll("_", " ")}
					</span>
				);
			})}
		</div>
	);
}

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

/** Loads a Transcript and renders its messages */
function TranscriptMessages({
	transcriptId,
	fontSize,
}: { transcriptId: FxtrID; fontSize: number }) {
	const ref = new EntityRef(transcriptId, Transcript);
	const t = useFxtrLoad(ref);
	return <MessageBubbles messages={t.messages} fontSize={fontSize} />;
}

export function FingerprintGraderResultMainView({
	entityRef,
}: {
	entityRef: EntityRef<FingerprintGraderResult>;
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}) {
	const gr = useFxtrLoad(entityRef);
	const color = dimColor(gr.dimensionName);

	return (
		<div style={{ viewTransitionName: vtName(entityRef.entityId) }}>
			{/* Header */}
			<div
				style={{
					fontSize: "14px",
					fontWeight: 700,
					color,
					marginBottom: "4px",
				}}
			>
				{gr.dimensionName.replaceAll("_", " ")}
			</div>
			<div style={{ fontSize: "24px", fontWeight: 700, marginBottom: "12px" }}>Grader Result</div>

			{/* Active categories */}
			<div style={{ marginBottom: "16px" }}>
				<div style={{ fontSize: "13px", fontWeight: 600, color: "#555", marginBottom: "6px" }}>
					Active Categories ({gr.activeCategories.length} / {gr.allCategories.length})
				</div>
				<CategoryBadges activeCategories={gr.activeCategories} color={color} />
			</div>

			{/* Rubric — collapsed by default */}
			<details style={{ marginBottom: "20px" }}>
				<summary
					style={{
						fontSize: "13px",
						fontWeight: 600,
						color: "#555",
						cursor: "pointer",
						marginBottom: "8px",
					}}
				>
					Rubric
				</summary>
				<Suspense fallback={<div>Loading rubric...</div>}>
					<Render entity={gr.rubric} as={FigureBlock} />
				</Suspense>
			</details>

			{/* Two-column: transcript (left) + reasoning (right) */}
			<div
				style={{
					display: "grid",
					gridTemplateColumns: "1fr 1fr",
					gap: "20px",
					alignItems: "start",
				}}
			>
				{/* Left: transcript */}
				<div
					style={{
						border: "1px solid #e0e0e0",
						borderRadius: "8px",
						padding: "12px 16px",
						backgroundColor: "#fafafa",
						maxHeight: "80vh",
						overflowY: "auto",
					}}
				>
					<div style={{ fontSize: "12px", fontWeight: 600, color: "#666", marginBottom: "10px" }}>
						Transcript
					</div>
					<Suspense fallback={<div style={{ color: "#999" }}>Loading transcript...</div>}>
						<TranscriptMessages transcriptId={gr.transcript} fontSize={12} />
					</Suspense>
				</div>

				{/* Right: reasoning */}
				<div
					style={{
						maxHeight: "80vh",
						overflowY: "auto",
					}}
				>
					<div style={{ fontSize: "12px", fontWeight: 600, color: "#666", marginBottom: "10px" }}>
						Reasoning
					</div>
					<div style={reasoningStyle}>{gr.reasoning}</div>
				</div>
			</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock — compact: dimension badge + active category badges
// ---------------------------------------------------------------------------

export function FingerprintGraderResultFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<FingerprintGraderResult>;
}) {
	const gr = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const color = dimColor(gr.dimensionName);

	return (
		<div
			style={{ ...figureCardStyle, viewTransitionName: vtName(entityRef.entityId) }}
			onClick={() => nav.navigateTo(entityRef.entityId)}
			onKeyDown={(e) => {
				if (e.key === "Enter") nav.navigateTo(entityRef.entityId);
			}}
			role="button"
			tabIndex={0}
		>
			<div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "6px" }}>
				<span
					style={{
						fontSize: "11px",
						fontWeight: 700,
						color,
						whiteSpace: "nowrap",
					}}
				>
					{gr.dimensionName.replaceAll("_", " ")}
				</span>
				<span style={{ fontSize: "11px", color: "#888" }}>
					{gr.activeCategories.length} / {gr.allCategories.length}
				</span>
			</div>
			<CategoryBadges activeCategories={gr.activeCategories} color={color} />
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function FingerprintGraderResultReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<FingerprintGraderResult>;
	linkText?: string;
}) {
	const gr = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? `${gr.dimensionName.replaceAll("_", " ")} result`;

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
