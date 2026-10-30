/**
 * Renderer for the `DemoDataEntity` (from
 * `packages/demo_data/src/demo_data/__init__.py`) — the worked example for
 * the nix/git UI-preset path.
 *
 * The agent's flake declares `fxtr.uiPresets.demo_preset` keyed
 * by this type's id_string (printed from a `run_script`:
 * `print(DemoDataEntity.fxtr_type.id_string())`), with
 * `renderer = esbuildRenderer { src = ./renderers/demo_data; }`. The
 * `NixConfigurableDispatchImpl` builds this module into an ESM derivation,
 * wraps the bytes in a `RendererEntity` at serve time, and serves it through
 * the unchanged `WorkstationDispatcherProto` — so the frontend's
 * `createDynamicDispatcher` loads it exactly like any other renderer.
 *
 * Modeled on the `note` renderer in `fxtr_workstation_demo`'s agent_workdir.
 * Imports are restricted to `lib.fxtr.workstationExternals` (the default
 * `esbuildRenderer` externals): `react`, `react/jsx-runtime`, `jotai`,
 * `jotai-effect`, and the `@fxtr-ui/{core,state-util,react,react-dispatch,
 * workstation-dispatch}` packages. Anything else fails the build (no
 * `node_modules`).
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";

interface DemoDataLoaded {
	readonly title: string;
	readonly body: string;
}

interface DemoPanelState {
	readonly load: OpResultStub<DemoDataLoaded> | null;
}

const INITIAL_PANEL_STATE: DemoPanelState = { load: null };

// ----- FullPanel -----

export const DemoDataFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<DemoPanelState>(
			ownStorage as FxtrSessionAtom<DemoPanelState | null>,
			INITIAL_PANEL_STATE,
		);
		return makeLoadResultAtom<DemoDataLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	if (result.kind === "errored") {
		return <span style={errorStyle}>load failed: {result.message}</span>;
	}
	if (result.kind === "loading") {
		return <span style={mutedStyle}>loading demo data…</span>;
	}
	return (
		<div style={panelStyle}>
			<h2 style={titleStyle}>{result.value.title}</h2>
			<div style={shortIdStyle}>
				demo_data — short id <code>{stub.shortId}</code>
			</div>
			<p style={bodyStyle}>{result.value.body}</p>
		</div>
	);
};

// ----- BlockLink / InlineLink / HoverPreview (shared load via dispatch atom) -

function useTitle(stub: EntityRefStub): string {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DemoDataLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DemoDataLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return result.kind === "filled" ? result.value.title : "…";
}

export const DemoDataBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => (
	<span style={pillStyle}>
		<span style={pillTagStyle}>demo_data</span>
		<span>{linkText ?? useTitle(stub)}</span>
	</span>
);

export const DemoDataInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => <code style={inlineStyle}>demo_data:{linkText ?? useTitle(stub)}</code>;

export const DemoDataHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => <div style={hoverStyle}>{useTitle(stub)}</div>;

const panelStyle: CSSProperties = { padding: 16, background: "#eef6ff", color: "#0a2a4a" };
const titleStyle: CSSProperties = { margin: 0, fontSize: 18 };
const shortIdStyle: CSSProperties = { marginTop: 8, fontSize: 11, color: "#3a6ea5" };
const bodyStyle: CSSProperties = { marginTop: 12, whiteSpace: "pre-wrap" };
const pillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	background: "#eef6ff",
	border: "1px solid #aaccee",
	color: "#0a2a4a",
	fontSize: 12,
};
const pillTagStyle: CSSProperties = { fontWeight: 600 };
const inlineStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#eef6ff",
	color: "#3a6ea5",
	fontSize: 12,
};
const hoverStyle: CSSProperties = {
	padding: 8,
	background: "#eef6ff",
	border: "1px solid #aaccee",
	borderRadius: 6,
	color: "#0a2a4a",
	fontSize: 12,
	minWidth: 160,
};
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { color: "#3a6ea5", fontStyle: "italic", fontSize: 12 };

// The outer key is the preset's `subtypeKey` ("demo_data"); the inner keys
// are the four shell dispatch-key UUIDs. The preset's `byDispatchKey` map
// must route the same UUIDs to this renderer with subtypeKey="demo_data".
export default {
	demo_data: {
		[FullPanel.uuid]: DemoDataFullPanel,
		[BlockLink.uuid]: DemoDataBlockLink,
		[InlineLink.uuid]: DemoDataInlineLink,
		[HoverPreview.uuid]: DemoDataHoverPreview,
	},
};
