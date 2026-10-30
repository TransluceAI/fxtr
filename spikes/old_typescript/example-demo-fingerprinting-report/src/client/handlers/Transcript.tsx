/**
 * Dispatch handlers for Transcript (raw conversation): MainView, FigureBlock, ReferenceLink.
 *
 * MainView: sim bio description, metadata header, message bubbles.
 * FigureBlock: chat bubbles in smaller font.
 */

import type { FxtrID } from "@fxtr/core";
import { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import type { JsonValue } from "@fxtr/reports-react";
import { useReportNavigation, vtName } from "@fxtr/reports-react";
import { Suspense } from "react";
import { SimulatorBio, type Transcript } from "../../entities.js";
import { MessageBubbles } from "./shared.js";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const metaHeaderStyle: React.CSSProperties = {
	display: "flex",
	gap: "12px",
	flexWrap: "wrap",
	marginBottom: "20px",
	padding: "12px 16px",
	backgroundColor: "#f6f8fa",
	borderRadius: "8px",
	border: "1px solid #d0d7de",
	fontSize: "13px",
};

const metaItemStyle: React.CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "2px",
};

const metaLabelStyle: React.CSSProperties = {
	fontSize: "11px",
	fontWeight: 600,
	color: "#666",
	textTransform: "uppercase",
};

const metaValueStyle: React.CSSProperties = {
	color: "#333",
};

const sectionLabelStyle: React.CSSProperties = {
	fontSize: "13px",
	fontWeight: 600,
	color: "#666",
	marginBottom: "8px",
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
// Inline sim bio description
// ---------------------------------------------------------------------------

function SimBioDescription({ bioId }: { bioId: FxtrID }) {
	const ref = new EntityRef(bioId, SimulatorBio);
	const sim = useFxtrLoad(ref);
	const nav = useReportNavigation();

	return (
		<div
			style={{
				border: "1px solid #d0d7de",
				borderRadius: "8px",
				padding: "10px 14px",
				backgroundColor: "#f6f8fa",
				cursor: "pointer",
			}}
			onClick={() => nav.navigateTo(bioId)}
			onKeyDown={(e) => {
				if (e.key === "Enter") nav.navigateTo(bioId);
			}}
			role="button"
			tabIndex={0}
		>
			<div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "4px" }}>{sim.label}</div>
			{sim.description && (
				<div style={{ fontSize: "13px", color: "#444", lineHeight: 1.5 }}>{sim.description}</div>
			)}
			<div style={{ fontSize: "11px", color: "#0066cc", marginTop: "4px" }}>
				View full simulator bio &rarr;
			</div>
		</div>
	);
}

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function TranscriptMainView({
	entityRef,
}: {
	entityRef: EntityRef<Transcript>;
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}) {
	const t = useFxtrLoad(entityRef);

	return (
		<div style={{ maxWidth: "800px", viewTransitionName: vtName(entityRef.entityId) }}>
			<div style={{ marginBottom: "20px" }}>
				<div style={sectionLabelStyle}>User Simulator</div>
				<Suspense
					fallback={<div style={{ fontSize: "13px", color: "#999" }}>Loading simulator...</div>}
				>
					<SimBioDescription bioId={t.simulatorBio} />
				</Suspense>
			</div>

			<div style={metaHeaderStyle}>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Transcript</span>
					<span style={metaValueStyle}>{t.transcriptId}</span>
				</div>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Model</span>
					<span style={metaValueStyle}>{t.modelName}</span>
				</div>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Family</span>
					<span style={metaValueStyle}>{t.familyName}</span>
				</div>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Simulator</span>
					<span style={metaValueStyle}>{t.simKey}</span>
				</div>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Turns</span>
					<span style={metaValueStyle}>{t.messages.length}</span>
				</div>
			</div>

			<MessageBubbles messages={t.messages} fontSize={14} />
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock
// ---------------------------------------------------------------------------

export function TranscriptFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<Transcript>;
}) {
	const t = useFxtrLoad(entityRef);
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
			<div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "4px" }}>
				Transcript {t.transcriptId}
			</div>
			<div style={{ fontSize: "11px", color: "#666", marginBottom: "10px" }}>
				{t.familyName} &middot; {t.simKey} &middot; {t.messages.length} turns
			</div>
			<MessageBubbles messages={t.messages} fontSize={11} />
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function TranscriptReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<Transcript>;
	linkText?: string;
}) {
	const t = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? `Transcript ${t.transcriptId}`;

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
