/**
 * Renderer for the `ExpNote` entity (from
 * `packages/experiment_notes/src/experiment_notes/__init__.py`) — the
 * experiment demo's universal value type, so every cell value the
 * workflow produces opens as something readable. Same pattern as the
 * workstation demo's `renderers/note/` (see that file's walkthrough
 * comments for how to write one of these).
 *
 * Imports are restricted to the curated externals list; anything else
 * would be bundled and `renderers/` has no `node_modules`.
 */

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

interface ExpNoteLoaded {
	readonly title: string;
	readonly body: string;
}

// ----- FullPanel -----

interface ExpNotePanelState {
	readonly load: OpResultStub<ExpNoteLoaded> | null;
}

const INITIAL_PANEL_STATE: ExpNotePanelState = { load: null };

export const ExpNoteFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<ExpNotePanelState>(
			ownStorage as FxtrSessionAtom<ExpNotePanelState | null>,
			INITIAL_PANEL_STATE,
		);
		return makeLoadResultAtom<ExpNoteLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	return (
		<div style={panelBodyStyle}>
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading exp note…</span>
			) : (
				<>
					<h2 style={titleStyle}>{result.value.title}</h2>
					<div style={shortIdLineStyle}>
						exp note — short id <code>{stub.shortId}</code>
					</div>
					<p style={bodyStyle}>{result.value.body}</p>
				</>
			)}
		</div>
	);
};

// ----- BlockLink / InlineLink / HoverPreview -----

export const ExpNoteBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<ExpNoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<ExpNoteLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>exp note</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

export const ExpNoteInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<ExpNoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<ExpNoteLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return <code style={inlineLinkStyle}>note:{label}</code>;
};

export const ExpNoteHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<ExpNoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<ExpNoteLoaded>(sharedAtom, stub),
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
				<span style={mutedStyle}>loading exp note…</span>
			</div>
		);
	}
	const snippet =
		result.value.body.length > 160 ? `${result.value.body.slice(0, 160)}…` : result.value.body;
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>{result.value.title}</div>
			{snippet && <div style={hoverBodyStyle}>{snippet}</div>}
		</div>
	);
};

// ----- styles (a green family, to read distinctly from the
// workstation demo's amber notes) -----

const panelBodyStyle: CSSProperties = {
	padding: 16,
	background: "#f2fbf4",
	color: "#14522a",
	height: "100%",
	overflow: "auto",
};
const titleStyle: CSSProperties = { margin: 0, fontSize: 18 };
const shortIdLineStyle: CSSProperties = { marginTop: 8, fontSize: 11, color: "#3f7a52" };
const bodyStyle: CSSProperties = { marginTop: 12, whiteSpace: "pre-wrap" };
const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	background: "#f2fbf4",
	border: "1px solid #9ed3ae",
	color: "#14522a",
	fontSize: 12,
};
const linkTagStyle: CSSProperties = { fontWeight: 600 };
const linkLabelStyle: CSSProperties = { fontSize: 11 };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#f2fbf4",
	color: "#3f7a52",
	fontSize: 12,
};
const hoverCardStyle: CSSProperties = {
	padding: 8,
	background: "#f2fbf4",
	border: "1px solid #9ed3ae",
	borderRadius: 6,
	color: "#14522a",
	fontSize: 12,
	minWidth: 160,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600 };
const hoverBodyStyle: CSSProperties = { fontSize: 11, color: "#3f7a52", marginTop: 4 };
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { color: "#3f7a52", fontStyle: "italic", fontSize: 12 };

export default {
	exp_note: {
		[FullPanel.uuid]: ExpNoteFullPanel,
		[BlockLink.uuid]: ExpNoteBlockLink,
		[InlineLink.uuid]: ExpNoteInlineLink,
		[HoverPreview.uuid]: ExpNoteHoverPreview,
	},
};
