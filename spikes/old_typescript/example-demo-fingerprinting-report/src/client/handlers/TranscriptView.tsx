/**
 * Dispatch handlers for FingerprintedTranscript: MainView, FigureBlock, ReferenceLink.
 *
 * MainView: sim bio description, grader result cards, then loads transcript for messages.
 * FigureBlock: metadata + fingerprint badges + chat bubbles (loaded from transcript).
 */

import type { FxtrID } from "@fxtr/core";
import { EntityRef } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { Render } from "@fxtr/react-dispatch";
import type { JsonValue } from "@fxtr/reports-react";
import { FigureBlock, useReportNavigation, vtName } from "@fxtr/reports-react";
import { Suspense } from "react";
import { dimColor } from "../../dimColors.js";
import {
	FingerprintGraderResult,
	type FingerprintedTranscript,
	SimulatorBio,
	Transcript,
} from "../../entities.js";
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

const graderGridStyle: React.CSSProperties = {
	display: "grid",
	gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
	gap: "8px",
	marginBottom: "20px",
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
// Inline: loads Transcript to render messages
// ---------------------------------------------------------------------------

function TranscriptMessages({
	transcriptId,
	fontSize,
}: {
	transcriptId: FxtrID;
	fontSize: number;
}) {
	const ref = new EntityRef(transcriptId, Transcript);
	const t = useFxtrLoad(ref);
	return <MessageBubbles messages={t.messages} fontSize={fontSize} />;
}

// ---------------------------------------------------------------------------
// Inline: loads grader result for badge rendering in FigureBlock
// ---------------------------------------------------------------------------

function GraderBadgesInline({ graderResultId }: { graderResultId: FxtrID }) {
	const ref = new EntityRef(graderResultId, FingerprintGraderResult);
	const gr = useFxtrLoad(ref);
	const color = dimColor(gr.dimensionName);

	if (gr.activeCategories.length === 0) return null;

	return (
		<>
			{gr.activeCategories.map((cat) => (
				<span
					key={`${gr.dimensionName}-${cat}`}
					style={{
						display: "inline-block",
						fontSize: "9px",
						padding: "1px 4px",
						borderRadius: "2px",
						backgroundColor: color,
						color: "#fff",
						fontFamily: "monospace",
						whiteSpace: "nowrap",
					}}
				>
					{cat.replaceAll("_", " ")}
				</span>
			))}
		</>
	);
}

// ---------------------------------------------------------------------------
// MainView
// ---------------------------------------------------------------------------

export function FingerprintedTranscriptMainView({
	entityRef,
}: {
	entityRef: EntityRef<FingerprintedTranscript>;
	navState?: JsonValue | undefined;
	annotations?: FxtrID[] | undefined;
}) {
	const ft = useFxtrLoad(entityRef);

	return (
		<div style={{ maxWidth: "800px", viewTransitionName: vtName(entityRef.entityId) }}>
			{/* Simulator bio */}
			<div style={{ marginBottom: "20px" }}>
				<div style={sectionLabelStyle}>User Simulator</div>
				<Suspense
					fallback={<div style={{ fontSize: "13px", color: "#999" }}>Loading simulator...</div>}
				>
					<SimBioDescription bioId={ft.simulatorBio} />
				</Suspense>
			</div>

			{/* Grader results */}
			{ft.graderResults.length > 0 && (
				<div style={{ marginBottom: "20px" }}>
					<div style={sectionLabelStyle}>
						Fingerprint Results ({ft.graderResults.length} dimensions)
					</div>
					<div style={graderGridStyle}>
						{ft.graderResults.map((grId) => (
							<Suspense
								key={grId.key()}
								fallback={<div style={{ fontSize: "12px", color: "#999" }}>...</div>}
							>
								<Render entity={grId} as={FigureBlock} />
							</Suspense>
						))}
					</div>
				</div>
			)}

			{/* Metadata header */}
			<div style={metaHeaderStyle}>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Transcript</span>
					<span style={metaValueStyle}>{ft.transcriptId}</span>
				</div>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Model</span>
					<span style={metaValueStyle}>{ft.modelName}</span>
				</div>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Family</span>
					<span style={metaValueStyle}>{ft.familyName}</span>
				</div>
				<div style={metaItemStyle}>
					<span style={metaLabelStyle}>Simulator</span>
					<span style={metaValueStyle}>{ft.simKey}</span>
				</div>
			</div>

			{/* Messages (loaded from Transcript entity) */}
			<Suspense fallback={<div style={{ color: "#999" }}>Loading transcript...</div>}>
				<TranscriptMessages transcriptId={ft.transcript} fontSize={14} />
			</Suspense>
		</div>
	);
}

// ---------------------------------------------------------------------------
// FigureBlock — metadata + badges + chat bubbles
// ---------------------------------------------------------------------------

export function FingerprintedTranscriptFigureBlock({
	entityRef,
}: {
	entityRef: EntityRef<FingerprintedTranscript>;
}) {
	const ft = useFxtrLoad(entityRef);
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
				Transcript {ft.transcriptId}
			</div>
			<div style={{ fontSize: "11px", color: "#666", marginBottom: "6px" }}>
				{ft.familyName} &middot; {ft.simKey}
			</div>
			{ft.graderResults.length > 0 && (
				<div style={{ display: "flex", flexWrap: "wrap", gap: "3px", marginBottom: "10px" }}>
					{ft.graderResults.map((grId) => (
						<Suspense key={grId.key()} fallback={null}>
							<GraderBadgesInline graderResultId={grId} />
						</Suspense>
					))}
				</div>
			)}
			<Suspense fallback={<div style={{ fontSize: "11px", color: "#999" }}>Loading...</div>}>
				<TranscriptMessages transcriptId={ft.transcript} fontSize={11} />
			</Suspense>
		</div>
	);
}

// ---------------------------------------------------------------------------
// ReferenceLink
// ---------------------------------------------------------------------------

export function FingerprintedTranscriptReferenceLink({
	entityRef,
	linkText,
}: {
	entityRef: EntityRef<FingerprintedTranscript>;
	linkText?: string;
}) {
	const ft = useFxtrLoad(entityRef);
	const nav = useReportNavigation();
	const text = linkText ?? `Transcript ${ft.transcriptId}`;

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
