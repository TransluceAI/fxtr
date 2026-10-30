/**
 * Dispatch handlers for SimulatorBio: MainView, FigureBlock, ReferenceLink.
 *
 * FigureBlock shows the full bio and description (rich preview).
 */

import type { FxtrID } from "@fxtr/core";
import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import type { JsonValue } from "@fxtr/reports-react";
import { useReportNavigation, vtName } from "@fxtr/reports-react";
import type { SimulatorBio } from "../../entities.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const titleStyle: React.CSSProperties = {
	fontSize: "24px",
	fontWeight: 700,
	marginBottom: "8px",
};

const sectionStyle: React.CSSProperties = {
	marginBottom: "20px",
};

const sectionLabelStyle: React.CSSProperties = {
	fontSize: "14px",
	fontWeight: 600,
	color: "#555",
	marginBottom: "8px",
};

const bioTextStyle: React.CSSProperties = {
	whiteSpace: "pre-wrap",
	fontSize: "14px",
	lineHeight: 1.7,
	color: "#333",
	backgroundColor: "#f8f9fa",
	padding: "16px 20px",
	borderRadius: "8px",
	border: "1px solid #e0e0e0",
};

const descriptionStyle: React.CSSProperties = {
	fontSize: "14px",
	lineHeight: 1.6,
	color: "#444",
};

const tagsContainerStyle: React.CSSProperties = {
	display: "flex",
	flexWrap: "wrap",
	gap: "6px",
};

const tagStyle: React.CSSProperties = {
	display: "inline-block",
	fontSize: "12px",
	padding: "3px 8px",
	borderRadius: "4px",
	backgroundColor: "#e8f0fe",
	color: "#1a73e8",
	fontFamily: "monospace",
};

const initialMessageStyle: React.CSSProperties = {
	fontSize: "14px",
	lineHeight: 1.6,
	color: "#333",
	padding: "12px 16px",
	borderLeft: "4px solid #1a73e8",
	backgroundColor: "#f6f8fa",
	borderRadius: "0 8px 8px 0",
	whiteSpace: "pre-wrap",
};

const figureCardStyle: React.CSSProperties = {
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
	padding: "12px 16px",
	backgroundColor: "#fafafa",
	cursor: "pointer",
};

const linkStyle: React.CSSProperties = {
	color: "#0066cc",
	cursor: "pointer",
	textDecoration: "none",
	borderBottom: "1px solid transparent",
};

// ---------------------------------------------------------------------------
// Shared content (used by MainView and FigureBlock)
// ---------------------------------------------------------------------------

function SimContent({
	sim,
	fontSize,
}: {
	sim: SimulatorBio;
	fontSize: number;
}) {
	const scale = fontSize / 14; // relative to base 14px
	return (
		<>
			{sim.description && (
				<div style={{ ...sectionStyle, marginBottom: `${16 * scale}px` }}>
					<div style={{ ...sectionLabelStyle, fontSize: `${fontSize}px` }}>Description</div>
					<div style={{ ...descriptionStyle, fontSize: `${fontSize}px` }}>{sim.description}</div>
				</div>
			)}

			{sim.tags.length > 0 && (
				<div style={{ ...sectionStyle, marginBottom: `${16 * scale}px` }}>
					<div style={{ ...sectionLabelStyle, fontSize: `${fontSize}px` }}>Tags</div>
					<div style={tagsContainerStyle}>
						{sim.tags.map((tag) => (
							<span key={tag} style={{ ...tagStyle, fontSize: `${Math.max(10, fontSize - 2)}px` }}>
								{tag}
							</span>
						))}
					</div>
				</div>
			)}

			{sim.initialMessage && (
				<div style={{ ...sectionStyle, marginBottom: `${16 * scale}px` }}>
					<div style={{ ...sectionLabelStyle, fontSize: `${fontSize}px` }}>Opening Message</div>
					<div style={{ ...initialMessageStyle, fontSize: `${fontSize}px` }}>
						{sim.initialMessage}
					</div>
				</div>
			)}

			<div style={sectionStyle}>
				<div style={{ ...sectionLabelStyle, fontSize: `${fontSize}px` }}>Persona Bio</div>
				<div style={{ ...bioTextStyle, fontSize: `${fontSize}px` }}>{sim.bio}</div>
			</div>
		</>
	);
}

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function SimulatorBioMainView({
	entityRef,
}: {
	entityRef: EntityRef<SimulatorBio>;
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}) {
	const sim = useFxtrLoad(entityRef);

	return (
		<div style={{ maxWidth: "720px", viewTransitionName: vtName(entityRef.entityId) }}>
			<div style={titleStyle}>{sim.label}</div>
			<SimContent sim={sim} fontSize={14} />
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock — rich preview with full bio
// ---------------------------------------------------------------------------

export function SimulatorBioFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<SimulatorBio>;
}) {
	const sim = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

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
			<div style={{ fontSize: "16px", fontWeight: 600, marginBottom: "8px" }}>{sim.label}</div>
			<SimContent sim={sim} fontSize={12} />
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function SimulatorBioReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<SimulatorBio>;
	linkText?: string;
}) {
	const sim = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? sim.label;

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
