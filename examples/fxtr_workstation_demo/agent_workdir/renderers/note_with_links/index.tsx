/**
 * Renderers for the provenance-carrying types from
 * `packages/simple_tasks/`:
 *
 * - `NoteWithLinks` — a Note variant with a `links` dict of named
 *   entity references. Rendered like the `Note` renderer (same
 *   note-paper look) plus a links section at the very bottom, after
 *   an hrule: one `name: <block RefLink>` row per entry — so you can
 *   follow the derivation chain entity-by-entity in the UI.
 * - `PromptTemplate` — a stored LLM prompt template (label +
 *   template text); `prompt_task` outputs link back to one of these
 *   under the `"prompt"` key.
 *
 * Both follow the standard load pattern (`makeLoadResultAtom` off
 * `ownStorage` for FullPanel, `useDispatchSharedAtom` for the link /
 * hover keys) — see `renderers/note/` for the annotated walkthrough.
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
	RefLink,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";

// ----- wire-decoded shapes -----

interface NoteWithLinksLoaded {
	readonly title: string;
	readonly body: string;
	readonly links: Readonly<Record<string, EntityRefStub>>;
}

interface PromptTemplateLoaded {
	readonly label: string;
	readonly template: string;
}

// ----- shared load hooks -----

function usePanelLoad<T>(stub: EntityRefStub, storage: FxtrSessionAtom<unknown>) {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<{ load: OpResultStub<T> | null }>(
			storage as FxtrSessionAtom<{ load: OpResultStub<T> | null } | null>,
			{ load: null },
		);
		return makeLoadResultAtom<T>(subAtom(panelAtom, "load"), stub);
	}, [storage, stub]);
	return useAtomValue(resultAtom);
}

function useSharedLoad<T>(stub: EntityRefStub) {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<T> | null>(null);
	const resultAtom = useMemo(() => makeLoadResultAtom<T>(sharedAtom, stub), [sharedAtom, stub]);
	return useAtomValue(resultAtom);
}

// ----- NoteWithLinks: FullPanel -----

function LinksSection({ links }: { readonly links: Readonly<Record<string, EntityRefStub>> }) {
	const names = Object.keys(links);
	if (names.length === 0) return null;
	return (
		<>
			<hr style={hruleStyle} />
			<div style={linksListStyle}>
				{names.map((name) => (
					<div key={name} style={linkRowStyle}>
						<code style={linkNameStyle}>{name}:</code>
						<RefLink stub={links[name] as EntityRefStub} as="block" />
					</div>
				))}
			</div>
		</>
	);
}

export const NoteWithLinksFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const result = usePanelLoad<NoteWithLinksLoaded>(stub, ownStorage);
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
						note with links — short id <code>{stub.shortId}</code>
					</div>
					<p style={bodyStyle}>{result.value.body}</p>
					<LinksSection links={result.value.links} />
				</>
			)}
		</div>
	);
};

// ----- NoteWithLinks: link / hover keys -----

export const NoteWithLinksBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const result = useSharedLoad<NoteWithLinksLoaded>(stub);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>note</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

export const NoteWithLinksInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const result = useSharedLoad<NoteWithLinksLoaded>(stub);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return <code style={inlineLinkStyle}>note:{label}</code>;
};

export const NoteWithLinksHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const result = useSharedLoad<NoteWithLinksLoaded>(stub);
	if (result.kind !== "filled") {
		return (
			<div style={hoverCardStyle}>
				{result.kind === "errored" ? (
					<span style={errorStyle}>load failed: {result.message}</span>
				) : (
					<span style={mutedStyle}>loading note…</span>
				)}
			</div>
		);
	}
	const snippet =
		result.value.body.length > 160 ? `${result.value.body.slice(0, 160)}…` : result.value.body;
	const linkNames = Object.keys(result.value.links);
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>{result.value.title}</div>
			{snippet && <div style={hoverBodyStyle}>{snippet}</div>}
			{linkNames.length > 0 && (
				<div style={hoverLinksLineStyle}>links: {linkNames.join(", ")}</div>
			)}
		</div>
	);
};

// ----- PromptTemplate -----

export const PromptTemplateFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const result = usePanelLoad<PromptTemplateLoaded>(stub, ownStorage);
	return (
		<div style={promptPanelBodyStyle}>
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading prompt template…</span>
			) : (
				<>
					<h2 style={titleStyle}>{result.value.label}</h2>
					<div style={shortIdLineStyle}>
						prompt template — short id <code>{stub.shortId}</code>; substitutes{" "}
						<code>{"{title}"}</code> / <code>{"{body}"}</code> from the input note
					</div>
					<pre style={templateTextStyle}>{result.value.template}</pre>
				</>
			)}
		</div>
	);
};

export const PromptTemplateBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const result = useSharedLoad<PromptTemplateLoaded>(stub);
	const label = linkText ?? (result.kind === "filled" ? result.value.label : "…");
	return (
		<span style={promptPillStyle}>
			<span style={linkTagStyle}>prompt</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

export const PromptTemplateInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const result = useSharedLoad<PromptTemplateLoaded>(stub);
	const label = linkText ?? (result.kind === "filled" ? result.value.label : "…");
	return <code style={inlineLinkStyle}>prompt:{label}</code>;
};

export const PromptTemplateHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const result = useSharedLoad<PromptTemplateLoaded>(stub);
	if (result.kind !== "filled") {
		return (
			<div style={promptHoverCardStyle}>
				{result.kind === "errored" ? (
					<span style={errorStyle}>load failed: {result.message}</span>
				) : (
					<span style={mutedStyle}>loading prompt template…</span>
				)}
			</div>
		);
	}
	return (
		<div style={promptHoverCardStyle}>
			<div style={hoverTitleStyle}>{result.value.label}</div>
			<pre style={hoverTemplateStyle}>{result.value.template}</pre>
		</div>
	);
};

// ----- styles: note look matches renderers/note/; prompt is violet -----

const panelBodyStyle: CSSProperties = {
	padding: 16,
	background: "#fffaea",
	color: "#5b4400",
	height: "100%",
	overflow: "auto",
};
const promptPanelBodyStyle: CSSProperties = {
	padding: 16,
	background: "#f7f3ff",
	color: "#3a2a66",
	height: "100%",
	overflow: "auto",
};
const titleStyle: CSSProperties = { margin: 0, fontSize: 18 };
const shortIdLineStyle: CSSProperties = { marginTop: 8, fontSize: 11, opacity: 0.7 };
const bodyStyle: CSSProperties = { marginTop: 12, whiteSpace: "pre-wrap" };
const templateTextStyle: CSSProperties = {
	marginTop: 12,
	whiteSpace: "pre-wrap",
	fontFamily: "var(--font-mono, monospace)",
	fontSize: 12,
	background: "#efe8ff",
	padding: 12,
	borderRadius: 6,
};
const hruleStyle: CSSProperties = {
	marginTop: 16,
	marginBottom: 12,
	border: "none",
	borderTop: "1px solid #e7d27a",
};
const linksListStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: 6,
};
const linkRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: 8,
};
const linkNameStyle: CSSProperties = { fontSize: 11, color: "#806c2c" };
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
const promptPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	background: "#f7f3ff",
	border: "1px solid #c4b0f0",
	color: "#3a2a66",
	fontSize: 12,
};
const linkTagStyle: CSSProperties = { fontWeight: 600 };
const linkLabelStyle: CSSProperties = { fontSize: 11 };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#f3ecd2",
	color: "#5b4400",
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
const promptHoverCardStyle: CSSProperties = {
	padding: 8,
	background: "#f7f3ff",
	border: "1px solid #c4b0f0",
	borderRadius: 6,
	color: "#3a2a66",
	fontSize: 12,
	minWidth: 200,
	maxWidth: 360,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600 };
const hoverBodyStyle: CSSProperties = { fontSize: 11, opacity: 0.8, marginTop: 4 };
const hoverLinksLineStyle: CSSProperties = { fontSize: 10, opacity: 0.6, marginTop: 6 };
const hoverTemplateStyle: CSSProperties = {
	marginTop: 6,
	whiteSpace: "pre-wrap",
	fontFamily: "var(--font-mono, monospace)",
	fontSize: 10,
	maxHeight: 140,
	overflow: "hidden",
};
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { fontStyle: "italic", fontSize: 12, opacity: 0.7 };

export default {
	note_with_links: {
		[FullPanel.uuid]: NoteWithLinksFullPanel,
		[BlockLink.uuid]: NoteWithLinksBlockLink,
		[InlineLink.uuid]: NoteWithLinksInlineLink,
		[HoverPreview.uuid]: NoteWithLinksHoverPreview,
	},
	prompt_template: {
		[FullPanel.uuid]: PromptTemplateFullPanel,
		[BlockLink.uuid]: PromptTemplateBlockLink,
		[InlineLink.uuid]: PromptTemplateInlineLink,
		[HoverPreview.uuid]: PromptTemplateHoverPreview,
	},
};
