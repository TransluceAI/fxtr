/**
 * Dispatch handlers for LinkedSVGFigure: MainView, FigureBlock, ReferenceLink.
 *
 * In both MainView and FigureBlock, SVG elements with `data-preview-target`
 * attributes become clickable. Clicking navigates to this figure as the main
 * view with the target key as navState.
 */

import type { FxtrCbor, FxtrID } from "@fxtr/core";
import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { Render } from "@fxtr/react-dispatch";
import type { LinkedSVGFigure } from "@fxtr/reports-entities";
import type { JsonValue } from "@fxtr/reports-react";
import { FigureBlock, useReportNavigation, vtName } from "@fxtr/reports-react";
import { Suspense, useCallback } from "react";
import { ReportTreeRenderer } from "../treeRenderer.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const figureContainerStyle: React.CSSProperties = {
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
	padding: "16px",
	margin: "16px 0",
	backgroundColor: "#fafafa",
};

const labelStyle: React.CSSProperties = {
	fontSize: "14px",
	fontWeight: 600,
	color: "#666",
	marginBottom: "8px",
};

const selectedSectionStyle: React.CSSProperties = {
	marginTop: "24px",
	paddingTop: "16px",
	borderTop: "1px solid #e0e0e0",
};

const selectedLabelStyle: React.CSSProperties = {
	fontSize: "14px",
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
// Helpers
// ---------------------------------------------------------------------------

function svgAsDocument(svg: FxtrCbor): readonly FxtrCbor[] {
	// The svg field is a single node tuple; wrap it in an array for the tree renderer
	return [svg];
}

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function LinkedSVGFigureMainView({
	entityRef,
	navState,
}: {
	entityRef: EntityRef<LinkedSVGFigure>;
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}) {
	const figure = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

	// navState is the selected preview target key
	const selectedKey = typeof navState === "string" ? navState : undefined;
	const selectedEntityId: FxtrID | undefined =
		selectedKey !== undefined ? figure.links[selectedKey] : undefined;

	const handlePreviewTargetClick = useCallback(
		(targetKey: string) => {
			// Same entity, change navState → replaces breadcrumb entry
			nav.navigateTo(entityRef.entityId, targetKey);
		},
		[nav, entityRef.entityId],
	);

	return (
		<div>
			<div style={labelStyle}>{figure.label}</div>
			<div style={{ display: "inline-block", viewTransitionName: vtName(entityRef.entityId) }}>
				<ReportTreeRenderer
					document={svgAsDocument(figure.svg)}
					onPreviewTargetClick={handlePreviewTargetClick}
					selectedPreviewTarget={selectedKey}
				/>
			</div>

			{selectedEntityId && (
				<div style={selectedSectionStyle}>
					<div style={selectedLabelStyle}>Selected:</div>
					<Suspense fallback={<div>Loading...</div>}>
						<Render entity={selectedEntityId} as={FigureBlock} />
					</Suspense>
				</div>
			)}
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock
// ---------------------------------------------------------------------------

export function LinkedSVGFigureFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<LinkedSVGFigure>;
}) {
	const figure = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

	const handlePreviewTargetClick = useCallback(
		(targetKey: string) => {
			// Navigate to this figure as main view with the target as navState
			nav.navigateTo(entityRef.entityId, targetKey);
		},
		[nav, entityRef.entityId],
	);

	return (
		<div style={figureContainerStyle}>
			<div style={labelStyle}>{figure.label}</div>
			<div style={{ display: "inline-block", viewTransitionName: vtName(entityRef.entityId) }}>
				<ReportTreeRenderer
					document={svgAsDocument(figure.svg)}
					onPreviewTargetClick={handlePreviewTargetClick}
				/>
			</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function LinkedSVGFigureReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<LinkedSVGFigure>;
	linkText?: string;
}) {
	const figure = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? figure.label;

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
