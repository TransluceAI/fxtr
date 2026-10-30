/**
 * Custom renderer for the demo's `Note` entity (`packages/demo_types`).
 *
 * A `{ title, body }` entity, rendered across the four shell keys. The `Figure`
 * key isn't registered here: when a report body embeds a Note via
 * `<RenderFigure entity={...}/>`, the `@fxtr-ui/reports` `Figure` no-match
 * fallback re-dispatches to this `FullPanel` (framed as a figure). So a custom
 * type "just works" as a figure with only a FullPanel renderer.
 *
 * Imports are restricted to the curated externals list the host installs (see
 * `@fxtr-ui/published-app`); `renderers/` has no `node_modules`, so a
 * non-external import fails the esbuild compile.
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

interface NoteLoaded {
	readonly title: string;
	readonly body: string;
}

interface NotePanelState {
	readonly load: OpResultStub<NoteLoaded> | null;
}

const INITIAL_PANEL_STATE: NotePanelState = { load: null };

export const NoteFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<NotePanelState>(
			ownStorage as FxtrSessionAtom<NotePanelState | null>,
			INITIAL_PANEL_STATE,
		);
		return makeLoadResultAtom<NoteLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	return (
		<div style={panelBodyStyle}>
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading note…</span>
			) : (
				<>
					<h2 style={titleStyle}>{result.value.title}</h2>
					<div style={shortIdLineStyle}>
						note — short id <code>{stub.shortId}</code>
					</div>
					<p style={bodyStyle}>{result.value.body}</p>
				</>
			)}
		</div>
	);
};

function useNoteLabel(stub: EntityRefStub, linkText: string | undefined): string {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<NoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<NoteLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return linkText ?? (result.kind === "filled" ? result.value.title : "…");
}

export const NoteBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub, linkText }) => {
	const label = useNoteLabel(stub, linkText);
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>note</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

export const NoteInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub, linkText }) => {
	const label = useNoteLabel(stub, linkText);
	return <code style={inlineLinkStyle}>note:{label}</code>;
};

export const NoteHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({ stub }) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<NoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<NoteLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	if (result.kind !== "filled") {
		return <div style={hoverCardStyle}><span style={mutedStyle}>loading note…</span></div>;
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

const panelBodyStyle: CSSProperties = {
	padding: 16,
	background: "#fffaea",
	color: "#5b4400",
	height: "100%",
	overflow: "auto",
};
const titleStyle: CSSProperties = { margin: 0, fontSize: 18 };
const shortIdLineStyle: CSSProperties = { marginTop: 8, fontSize: 11, color: "#806c2c" };
const bodyStyle: CSSProperties = { marginTop: 12, whiteSpace: "pre-wrap" };
const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	background: "#fffaea",
	border: "1px solid #e7d27a",
	color: "#5b4400",
	fontSize: 12,
};
const linkTagStyle: CSSProperties = { fontWeight: 600 };
const linkLabelStyle: CSSProperties = { fontSize: 11 };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#fffaea",
	color: "#806c2c",
	fontSize: 12,
};
const hoverCardStyle: CSSProperties = {
	padding: 8,
	background: "#fffaea",
	border: "1px solid #e7d27a",
	borderRadius: 6,
	color: "#5b4400",
	fontSize: 12,
	minWidth: 160,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600 };
const hoverBodyStyle: CSSProperties = { fontSize: 11, color: "#806c2c", marginTop: 4 };
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { color: "#806c2c", fontStyle: "italic", fontSize: 12 };

export default {
	note: {
		[FullPanel.uuid]: NoteFullPanel,
		[BlockLink.uuid]: NoteBlockLink,
		[InlineLink.uuid]: NoteInlineLink,
		[HoverPreview.uuid]: NoteHoverPreview,
	},
};
