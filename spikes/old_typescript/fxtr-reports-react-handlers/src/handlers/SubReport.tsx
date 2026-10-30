/**
 * Dispatch handlers for SubReport: MainView, FigureBlock, ReferenceLink.
 */

import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import type { SubReport } from "@fxtr/reports-entities";
import type { JsonValue } from "@fxtr/reports-react";
import { useReportNavigation, vtName } from "@fxtr/reports-react";
import { ReportTreeRenderer } from "../treeRenderer.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const mainViewStyle: React.CSSProperties = {
	maxWidth: "720px",
};

const labelStyle: React.CSSProperties = {
	fontSize: "24px",
	fontWeight: 600,
	marginBottom: "12px",
	lineHeight: 1.3,
};

const figureStyle: React.CSSProperties = {
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
	padding: "16px",
	margin: "16px 0",
	backgroundColor: "#fafafa",
};

const showDetailsStyle: React.CSSProperties = {
	color: "#0066cc",
	cursor: "pointer",
	fontSize: "14px",
	marginTop: "8px",
	display: "inline-block",
};

const linkStyle: React.CSSProperties = {
	color: "#0066cc",
	cursor: "pointer",
	textDecoration: "none",
	borderBottom: "1px solid transparent",
};

const detailSeparator: React.CSSProperties = {
	borderTop: "1px solid #e0e0e0",
	marginTop: "24px",
	paddingTop: "24px",
};

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function SubReportMainView({
	entityRef,
}: {
	entityRef: EntityRef<SubReport>;
	navState?: JsonValue | undefined;
	annotations?: unknown[] | undefined;
}) {
	const sub = useFxtrLoad(entityRef);

	return (
		<div style={mainViewStyle}>
			<div style={labelStyle}>{sub.label}</div>
			<div style={{ viewTransitionName: vtName(entityRef.entityId) }}>
				<ReportTreeRenderer document={sub.overview} />
			</div>
			{sub.detail.length > 0 && (
				<div style={detailSeparator}>
					<ReportTreeRenderer document={sub.detail} />
				</div>
			)}
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock
// ---------------------------------------------------------------------------

export function SubReportFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<SubReport>;
}) {
	const sub = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

	return (
		<div style={{ ...figureStyle, viewTransitionName: vtName(entityRef.entityId) }}>
			<ReportTreeRenderer document={sub.overview} />
			<span
				style={showDetailsStyle}
				onClick={() => nav.navigateTo(entityRef.entityId)}
				onKeyDown={(e) => {
					if (e.key === "Enter") nav.navigateTo(entityRef.entityId);
				}}
				role="link"
				tabIndex={0}
			>
				Show details &rarr;
			</span>
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function SubReportReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<SubReport>;
	linkText?: string;
}) {
	const sub = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? sub.label;

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
