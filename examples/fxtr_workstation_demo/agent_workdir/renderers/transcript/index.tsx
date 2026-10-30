import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
import {
	BlockLink,
	type FullPanelProps,
	FullPanel,
	type HoverPreviewProps,
	HoverPreview,
	InlineLink,
	type LinkProps,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";

interface TranscriptMessage {
	readonly role: string;
	readonly content: string;
}

interface TranscriptLoaded {
	readonly transcript_id: string;
	readonly agent_run_id: string;
	readonly name: string;
	readonly description: string | null;
	readonly created_at: string;
	readonly messages: readonly TranscriptMessage[];
}

function shortTitle(t: TranscriptLoaded): string {
	const idHead = t.transcript_id.slice(0, 8);
	return t.description ? `${idHead} — ${t.description}` : idHead;
}

// ----- FullPanel -----

interface TranscriptPanelState {
	readonly load: OpResultStub<TranscriptLoaded> | null;
}

const INITIAL_PANEL_STATE: TranscriptPanelState = { load: null };

export const TranscriptFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<TranscriptPanelState>(
			ownStorage as FxtrSessionAtom<TranscriptPanelState | null>,
			INITIAL_PANEL_STATE,
		);
		return makeLoadResultAtom<TranscriptLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	if (result.kind === "errored") {
		return (
			<div style={panelBodyStyle}>
				<span style={errorStyle}>load failed: {result.message}</span>
			</div>
		);
	}
	if (result.kind === "loading") {
		return (
			<div style={panelBodyStyle}>
				<span style={mutedStyle}>loading transcript…</span>
			</div>
		);
	}
	const t = result.value;
	return (
		<div style={panelBodyStyle}>
			<h2 style={titleStyle}>Transcript {t.transcript_id.slice(0, 8)}</h2>
			<dl style={metaStyle}>
				<dt style={dtStyle}>id</dt>
				<dd style={ddStyle}>
					<code>{t.transcript_id}</code>
				</dd>
				<dt style={dtStyle}>agent run</dt>
				<dd style={ddStyle}>
					<code>{t.agent_run_id}</code>
				</dd>
				<dt style={dtStyle}>name</dt>
				<dd style={ddStyle}>{t.name}</dd>
				{t.description && (
					<>
						<dt style={dtStyle}>description</dt>
						<dd style={ddStyle}>{t.description}</dd>
					</>
				)}
				<dt style={dtStyle}>created</dt>
				<dd style={ddStyle}>{t.created_at}</dd>
				<dt style={dtStyle}>messages</dt>
				<dd style={ddStyle}>{t.messages.length}</dd>
			</dl>
			<div style={messagesStyle}>
				{t.messages.map((m, i) => (
					<div key={i} style={messageStyle(m.role)}>
						<div style={roleStyle(m.role)}>{m.role}</div>
						<div style={contentStyle}>{m.content}</div>
					</div>
				))}
			</div>
		</div>
	);
};

// ----- BlockLink / InlineLink / HoverPreview -----

export const TranscriptBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<TranscriptLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<TranscriptLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? shortTitle(result.value) : "…");
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>transcript</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

export const TranscriptInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<TranscriptLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<TranscriptLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? shortTitle(result.value) : "…");
	return <code style={inlineLinkStyle}>transcript:{label}</code>;
};

export const TranscriptHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<TranscriptLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<TranscriptLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);

	if (result.kind === "errored") {
		return (
			<div style={hoverCardStyle}>
				<span style={errorStyle}>load failed: {result.message}</span>
			</div>
		);
	}
	if (result.kind === "loading") {
		return (
			<div style={hoverCardStyle}>
				<span style={mutedStyle}>loading transcript…</span>
			</div>
		);
	}
	const t = result.value;
	const firstUser = t.messages.find((m) => m.role === "user")?.content ?? "";
	const snippet = firstUser.length > 140 ? `${firstUser.slice(0, 140)}…` : firstUser;
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>Transcript {t.transcript_id.slice(0, 8)}</div>
			<div style={hoverMetaStyle}>
				{t.messages.length} msgs · {t.description ?? t.name}
			</div>
			{snippet && <div style={hoverBodyStyle}>{snippet}</div>}
		</div>
	);
};

// ----- styles -----

const panelBodyStyle: CSSProperties = {
	padding: 16,
	background: "#f4f7fb",
	color: "#1d2a3a",
	height: "100%",
	overflow: "auto",
};
const titleStyle: CSSProperties = { margin: 0, fontSize: 18 };
const metaStyle: CSSProperties = {
	display: "grid",
	gridTemplateColumns: "auto 1fr",
	columnGap: 12,
	rowGap: 4,
	margin: "12px 0 16px",
	fontSize: 12,
};
const dtStyle: CSSProperties = { color: "#6a7a8c", fontWeight: 600 };
const ddStyle: CSSProperties = { margin: 0, color: "#1d2a3a" };
const messagesStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: 10,
};
const messageBaseStyle: CSSProperties = {
	padding: "8px 10px",
	borderRadius: 6,
	border: "1px solid #d4dde8",
};
function messageStyle(role: string): CSSProperties {
	const bg =
		role === "user"
			? "#eef4ff"
			: role === "assistant"
				? "#fff7e6"
				: role === "system"
					? "#f0f0f0"
					: "#fafafa";
	return { ...messageBaseStyle, background: bg };
}
function roleStyle(role: string): CSSProperties {
	const color =
		role === "user" ? "#2b4cb3" : role === "assistant" ? "#9a6500" : "#555";
	return { fontWeight: 600, fontSize: 11, color, textTransform: "uppercase" };
}
const contentStyle: CSSProperties = {
	marginTop: 4,
	fontSize: 13,
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	background: "#eef4ff",
	border: "1px solid #b8c8e0",
	color: "#1d2a3a",
	fontSize: 12,
};
const linkTagStyle: CSSProperties = { fontWeight: 600, color: "#2b4cb3" };
const linkLabelStyle: CSSProperties = { fontSize: 11 };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#eef4ff",
	color: "#2b4cb3",
	fontSize: 12,
};
const hoverCardStyle: CSSProperties = {
	padding: 8,
	background: "#f4f7fb",
	border: "1px solid #b8c8e0",
	borderRadius: 6,
	color: "#1d2a3a",
	fontSize: 12,
	minWidth: 200,
	maxWidth: 360,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600 };
const hoverMetaStyle: CSSProperties = { fontSize: 11, color: "#6a7a8c", marginTop: 2 };
const hoverBodyStyle: CSSProperties = {
	fontSize: 11,
	color: "#1d2a3a",
	marginTop: 6,
	whiteSpace: "pre-wrap",
};
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { color: "#6a7a8c", fontStyle: "italic", fontSize: 12 };

export default {
	transcript: {
		[FullPanel.uuid]: TranscriptFullPanel,
		[BlockLink.uuid]: TranscriptBlockLink,
		[InlineLink.uuid]: TranscriptInlineLink,
		[HoverPreview.uuid]: TranscriptHoverPreview,
	},
};
