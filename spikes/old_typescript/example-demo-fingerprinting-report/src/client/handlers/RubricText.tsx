/**
 * Dispatch handlers for RubricText: MainView, FigureBlock, ReferenceLink.
 *
 * FigureBlock shows the full rubric text (not truncated).
 */

import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import type { JsonValue } from "@fxtr/reports-react";
import { useReportNavigation, vtName } from "@fxtr/reports-react";
import type { RubricText } from "../../entities.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const mainViewStyle: React.CSSProperties = {
	maxWidth: "720px",
};

const titleStyle: React.CSSProperties = {
	fontSize: "24px",
	fontWeight: 700,
	marginBottom: "4px",
};

const subtitleStyle: React.CSSProperties = {
	fontSize: "14px",
	color: "#666",
	marginBottom: "20px",
};

const rubricBlockStyle: React.CSSProperties = {
	whiteSpace: "pre-wrap",
	fontSize: "14px",
	lineHeight: 1.7,
	color: "#333",
	backgroundColor: "#f8f9fa",
	padding: "20px 24px",
	borderRadius: "8px",
	border: "1px solid #e0e0e0",
};

const figureStyle: React.CSSProperties = {
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
	padding: "16px 20px",
	backgroundColor: "#f8f9fa",
	cursor: "pointer",
};

const figureLabelStyle: React.CSSProperties = {
	fontSize: "12px",
	fontWeight: 600,
	color: "#666",
	marginBottom: "8px",
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

export function RubricTextMainView({
	entityRef,
}: {
	entityRef: EntityRef<RubricText>;
	navState?: JsonValue | undefined;
}) {
	const rubric = useFxtrLoad(entityRef);

	return (
		<div style={{ ...mainViewStyle, viewTransitionName: vtName(entityRef.entityId) }}>
			<div style={titleStyle}>
				{rubric.categoryName ? `${rubric.label}: ${rubric.categoryName}` : rubric.label}
			</div>
			<div style={subtitleStyle}>Judging Rubric</div>
			<div style={rubricBlockStyle}>{rubric.text.trim()}</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock — full rubric text
// ---------------------------------------------------------------------------

export function RubricTextFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<RubricText>;
}) {
	const rubric = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

	return (
		<div
			style={{ ...figureStyle, viewTransitionName: vtName(entityRef.entityId) }}
			onClick={() => nav.navigateTo(entityRef.entityId)}
			onKeyDown={(e) => {
				if (e.key === "Enter") nav.navigateTo(entityRef.entityId);
			}}
			role="button"
			tabIndex={0}
		>
			<div style={figureLabelStyle}>Rubric: {rubric.label}</div>
			<div style={{ whiteSpace: "pre-wrap", fontSize: "13px", lineHeight: 1.6, color: "#333" }}>
				{rubric.text.trim()}
			</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function RubricTextReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<RubricText>;
	linkText?: string;
}) {
	const rubric = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? `Rubric: ${rubric.label}`;

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
