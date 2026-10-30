/**
 * Dispatch handlers for RootReport: MainView, ReferenceLink, SummaryCard.
 */

import type { FxtrID } from "@fxtr/core";
import type { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { Render } from "@fxtr/react-dispatch";
import type { RootReport } from "@fxtr/reports-entities";
import {
	AttachMarginNote,
	type JsonValue,
	MarginNote,
	useReportNavigation,
	vtName,
} from "@fxtr/reports-react";
import { Suspense } from "react";
import { ReportTreeRenderer } from "../treeRenderer.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const mainViewStyle: React.CSSProperties = {
	maxWidth: "720px",
};

const titleStyle: React.CSSProperties = {
	fontSize: "28px",
	fontWeight: 700,
	marginBottom: "16px",
	lineHeight: 1.3,
};

const linkStyle: React.CSSProperties = {
	color: "#0066cc",
	cursor: "pointer",
	textDecoration: "none",
	borderBottom: "1px solid transparent",
};

const linkHoverStyle: React.CSSProperties = {
	borderBottom: "1px solid #0066cc",
};

const cardStyle: React.CSSProperties = {
	border: "1px solid #e0e0e0",
	borderRadius: "8px",
	padding: "16px",
	cursor: "pointer",
	transition: "box-shadow 0.15s",
};

const cardTitleStyle: React.CSSProperties = {
	fontSize: "18px",
	fontWeight: 600,
	marginBottom: "8px",
};

const cardSummaryStyle: React.CSSProperties = {
	color: "#666",
	fontSize: "14px",
	lineHeight: 1.5,
};

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function RootReportMainView({
	entityRef,
	annotations,
}: {
	entityRef: EntityRef<RootReport>;
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}) {
	const report = useFxtrLoad(entityRef);

	return (
		<div style={mainViewStyle}>
			{/* Annotations as margin notes */}
			{annotations && annotations.length > 0 && (
				<AttachMarginNote>
					{annotations.map((annId) => (
						<Suspense key={annId.key()} fallback={<div>...</div>}>
							<Render entity={annId} as={MarginNote} />
						</Suspense>
					))}
				</AttachMarginNote>
			)}

			<div style={{ ...titleStyle, viewTransitionName: vtName(entityRef.entityId) }}>
				{report.title}
			</div>
			<ReportTreeRenderer document={report.contents} />
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function RootReportReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<RootReport>;
	linkText?: string;
}) {
	const report = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? report.title;

	return (
		<span
			style={linkStyle}
			onClick={() => nav.navigateTo(entityRef.entityId)}
			onMouseEnter={(e) => Object.assign(e.currentTarget.style, linkHoverStyle)}
			onMouseLeave={(e) =>
				Object.assign(e.currentTarget.style, { borderBottom: "1px solid transparent" })
			}
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

// ---------------------------------------------------------------------------
// SummaryCard
// ---------------------------------------------------------------------------

export function RootReportSummaryCard({
	entityRef,
}: {
	entityRef: EntityRef<RootReport>;
}) {
	const report = useFxtrLoad(entityRef);
	const nav = useReportNavigation();

	return (
		<div
			style={{ ...cardStyle, viewTransitionName: vtName(entityRef.entityId) }}
			onClick={() => nav.navigateTo(entityRef.entityId)}
			onKeyDown={(e) => {
				if (e.key === "Enter") nav.navigateTo(entityRef.entityId);
			}}
			role="button"
			tabIndex={0}
		>
			<div style={cardTitleStyle}>{report.title}</div>
			<div style={cardSummaryStyle}>{report.summary}</div>
		</div>
	);
}
