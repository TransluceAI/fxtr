/**
 * Sample renderer for the `Note` entity (from
 * `packages/presentables/src/presentables/__init__.py`).
 *
 * Modeled on `RootReportFullPanel` / `RootReportBlockLink` /
 * `RootReportInlineLink` / `RootReportHoverPreview` in
 * `@fxtr-ui/reports/handlers/RootReport.tsx`. The same pattern
 * works for any agent-defined entity:
 *
 *   1. Pick a slot atom — `ownStorage` (panel-scoped, persists
 *      across the panel session) for FullPanel / Figure;
 *      `useDispatchSharedAtom` (per-dispatcher-session, shared
 *      with sibling renders) for the link / hover keys.
 *   2. Inside `useMemo`, lift the slot through `atomWithDefault`,
 *      narrow it with `subAtom` (when the slot holds richer state
 *      than just the load stub), and pass to
 *      `makeLoadResultAtom<T>(slotAtom, stub)`. The result atom
 *      yields a discriminated `LoadResult<T>` with kinds
 *      `loading | filled | errored`.
 *   3. `useAtomValue(resultAtom)`, switch on `result.kind`, render.
 *
 * To write a renderer for a new entity type the agent defines:
 *
 *   1. Define the entity in Python (see `Note` in
 *      `presentables/__init__.py`).
 *   2. Grab the entity's `fxtr_type.id_string()` from a sandbox
 *      script, paste it into the flake's `mkType` call, and declare
 *      the (type, dispatch-key) pairs in the renderer dir's
 *      `fxtr_renderer.nix` slice (merged into the preset by the
 *      root `flake.nix`).
 *   3. Replace `NoteLoaded` here with your entity's field shape,
 *      adjust the FullPanel / BlockLink / InlineLink components,
 *      then commit and re-push the preset by git ref —
 *      `set_preset(current_commit(), "fxtr.uiPresets.default")`
 *      — see the README's "Writing a custom visualizer".
 *
 * Imports are restricted to the curated externals list — `react`,
 * `react/jsx-runtime`, `jotai`, `jotai-effect`, the `@fxtr-ui/*`
 * packages, plus `@fxtr-ui/agent-plugin-renderer`,
 * `@fxtr-ui/rd-types-stream`, and `@fxtr-ui/reports`. Anything
 * else gets bundled; since `renderers/` has no `node_modules`,
 * non-external imports fail the compile.
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

// ----- FullPanel -----

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
				<NoteShell stub={stub} title="…">
					<span style={mutedStyle}>loading note…</span>
				</NoteShell>
			) : (
				<NoteShell stub={stub} title={result.value.title}>
					<p style={bodyStyle}>{result.value.body}</p>
				</NoteShell>
			)}
		</div>
	);
};

// ----- BlockLink / InlineLink / HoverPreview -----
//
// The link / hover keys don't get an `ownStorage` slot. We share a
// load stub across the renderer's siblings via
// `useDispatchSharedAtom` so multiple links pointing at the same
// stub dedupe the load. The shared atom is per (dispatcher-session,
// stub, key) — identity-stable across renders for the same entry.

export const NoteBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<NoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<NoteLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>note</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

export const NoteInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<NoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<NoteLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return <code style={inlineLinkStyle}>note:{label}</code>;
};

export const NoteHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<NoteLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<NoteLoaded>(sharedAtom, stub),
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
				<span style={mutedStyle}>loading note…</span>
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

// ----- Shared layout -----

function NoteShell(props: {
	readonly stub: EntityRefStub;
	readonly title: string;
	readonly children: React.ReactNode;
}) {
	return (
		<>
			<h2 style={titleStyle}>{props.title}</h2>
			<div style={shortIdLineStyle}>
				note — short id <code>{props.stub.shortId}</code>
			</div>
			{props.children}
		</>
	);
}

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
