/**
 * Renderer module for the `Note` entity type.
 *
 * This file lives in the source tree as a template; the Python
 * backend copies it into `<note_type_id_string>/index.tsx` at
 * startup (the actual type id is content-addressed and unknown
 * until the Python side runs `define_type`) and hands the
 * directory to `BasicDynamicDispatchImpl`, which esbuild-compiles
 * it with `--jsx=automatic`. The compiled module's default
 * export is a 2-level map `[subtype_key][dispatch_key_uuid] →
 * Component`; the `DynamicDispatcher` looks up
 * `module["default"][key.uuid]` after fetching the bytes.
 *
 * Exports entries for two sets of dispatch keys:
 *
 *   - The demo's own `CardKey` (UUID hardcoded; matches
 *     `src/client/dispatch_keys.ts`) — used by the legacy
 *     card-grid route.
 *   - The four workstation shell dispatch keys (`FullPanel`,
 *     `BlockLink`, `InlineLink`, `HoverPreview`), imported from
 *     `@fxtr-ui/workstation-dispatch` — used by the new
 *     workstation-shell route. The shell expects entities of this
 *     type to render through these slots when present.
 *
 * Both routes share the same renderer entity (and the same
 * compiled bytes); the dispatcher picks the right component out
 * of the 2-level map per call-site key.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import type { DispatchedComponent } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	InlineLink,
	RefLink,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { useMemo } from "react";

// Keep in sync with `src/client/dispatch_keys.ts`.
const CARD_KEY_UUID = "00000000-0000-4000-8000-000000010001";

/** The wire-decoded `Note` shape (a `links` dict of named entity refs). */
interface NoteLoaded {
	readonly title: string;
	readonly body: string;
	readonly links: Readonly<Record<string, EntityRefStub>>;
}

/** Load the note's data into the FullPanel's own (persisted) storage. */
function usePanelLoad(stub: EntityRefStub, storage: FxtrSessionAtom<unknown>) {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<{ load: OpResultStub<NoteLoaded> | null }>(
			storage as FxtrSessionAtom<{ load: OpResultStub<NoteLoaded> | null } | null>,
			{ load: null },
		);
		return makeLoadResultAtom<NoteLoaded>(subAtom(panelAtom, "load"), stub);
	}, [storage, stub]);
	return useAtomValue(resultAtom);
}

/** Render the note's `links` as named block RefLinks (empty → nothing). */
function LinksSection({ links }: { readonly links: Readonly<Record<string, EntityRefStub>> }) {
	const names = Object.keys(links);
	if (names.length === 0) return null;
	return (
		<>
			<hr
				style={{ marginTop: 16, marginBottom: 12, border: "none", borderTop: "1px solid #e7d27a" }}
			/>
			<div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
				{names.map((name) => (
					<div key={name} style={{ display: "flex", alignItems: "center", gap: 8 }}>
						<code style={{ fontSize: 11, color: "#806c2c" }}>{name}:</code>
						<RefLink stub={links[name] as EntityRefStub} as="block" />
					</div>
				))}
			</div>
		</>
	);
}

function NoteCard(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<div
			style={{
				padding: 12,
				borderRadius: 8,
				background: "#fffaea",
				border: "1px solid #e7d27a",
				color: "#5b4400",
			}}
		>
			<div style={{ fontWeight: 600, marginBottom: 4 }}>NoteCard</div>
			<div style={{ fontSize: 11, color: "#806c2c" }}>
				dynamically loaded — short id {props.stub.shortId}
			</div>
		</div>
	);
}

const NoteFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const result = usePanelLoad(stub, ownStorage);
	return (
		<div
			style={{
				padding: 16,
				background: "#fffaea",
				color: "#5b4400",
				height: "100%",
				overflow: "auto",
			}}
		>
			{result.kind === "errored" ? (
				<span style={{ color: "#c00", fontSize: 12 }}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={{ fontStyle: "italic", fontSize: 12, opacity: 0.7 }}>loading note…</span>
			) : (
				<>
					<h2 style={{ margin: 0, fontSize: 18 }}>{result.value.title}</h2>
					<div style={{ marginTop: 8, fontSize: 11, color: "#806c2c" }}>
						note — short id <code>{stub.shortId}</code>
					</div>
					<p style={{ marginTop: 12, whiteSpace: "pre-wrap" }}>{result.value.body}</p>
					{/* Each link renders via the linked entity's own renderer; following
					    one (e.g. to a provenanced photo) surfaces its provenance. */}
					<LinksSection links={result.value.links} />
				</>
			)}
		</div>
	);
};

function NoteBlockLink(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<span
			style={{
				display: "inline-flex",
				alignItems: "center",
				gap: 6,
				padding: "2px 8px",
				borderRadius: 6,
				background: "#fffaea",
				border: "1px solid #e7d27a",
				color: "#5b4400",
				fontSize: 12,
			}}
		>
			<span style={{ fontWeight: 600 }}>Note</span>
			<code style={{ fontSize: 11 }}>{props.stub.shortId}</code>
		</span>
	);
}

function NoteInlineLink(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<code
			style={{
				padding: "1px 4px",
				borderRadius: 4,
				background: "#fffaea",
				color: "#806c2c",
				fontSize: 12,
			}}
		>
			note:{props.stub.shortId}
		</code>
	);
}

function NoteHoverPreview(props: { readonly stub: { readonly shortId: string } }) {
	return (
		<div
			style={{
				padding: 8,
				background: "#fffaea",
				border: "1px solid #e7d27a",
				borderRadius: 6,
				color: "#5b4400",
				fontSize: 12,
				minWidth: 160,
			}}
		>
			<div style={{ fontWeight: 600 }}>Note hover</div>
			<div style={{ fontSize: 11, color: "#806c2c", marginTop: 4 }}>{props.stub.shortId}</div>
		</div>
	);
}

export default {
	default: {
		[CARD_KEY_UUID]: NoteCard,
		[FullPanel.uuid]: NoteFullPanel,
		[BlockLink.uuid]: NoteBlockLink,
		[InlineLink.uuid]: NoteInlineLink,
		[HoverPreview.uuid]: NoteHoverPreview,
	},
};
