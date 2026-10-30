/**
 * Custom renderer for the demo's `Stats` entity (`packages/demo_types`).
 *
 * `Stats` is the **provenanced** postprocess output, so when it's embedded as a
 * report figure the loaded stub carries a `provenance_parent` (visible via the
 * metadata panel). Like the `Note` renderer, this registers only the shell keys;
 * the `@fxtr-ui/reports` `Figure` no-match fallback re-dispatches to `FullPanel`.
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

interface StatsLoaded {
	readonly note_count: number;
	readonly total_words: number;
	readonly longest_title: string;
}

interface StatsPanelState {
	readonly load: OpResultStub<StatsLoaded> | null;
}

const INITIAL_PANEL_STATE: StatsPanelState = { load: null };

export const StatsFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<StatsPanelState>(
			ownStorage as FxtrSessionAtom<StatsPanelState | null>,
			INITIAL_PANEL_STATE,
		);
		return makeLoadResultAtom<StatsLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	return (
		<div style={panelBodyStyle}>
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading stats…</span>
			) : (
				<>
					<h2 style={titleStyle}>note statistics</h2>
					<div style={shortIdLineStyle}>
						stats — short id <code>{stub.shortId}</code>
					</div>
					<dl style={dlStyle}>
						<StatRow label="notes" value={String(result.value.note_count)} />
						<StatRow label="total words" value={String(result.value.total_words)} />
						<StatRow label="longest title" value={result.value.longest_title} />
					</dl>
				</>
			)}
		</div>
	);
};

function StatRow(props: { readonly label: string; readonly value: string }) {
	return (
		<>
			<dt style={dtStyle}>{props.label}</dt>
			<dd style={ddStyle}>{props.value}</dd>
		</>
	);
}

function useStatsCount(stub: EntityRefStub): string {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<StatsLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<StatsLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return result.kind === "filled" ? `${result.value.note_count} notes` : "…";
}

export const StatsBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub, linkText }) => {
	const fallback = useStatsCount(stub);
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>stats</span>
			<span style={linkLabelStyle}>{linkText ?? fallback}</span>
		</span>
	);
};

export const StatsInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub, linkText }) => {
	const fallback = useStatsCount(stub);
	return <code style={inlineLinkStyle}>stats:{linkText ?? fallback}</code>;
};

export const StatsHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({ stub }) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<StatsLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<StatsLoaded>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	if (result.kind !== "filled") {
		return <div style={hoverCardStyle}><span style={mutedStyle}>loading stats…</span></div>;
	}
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>{result.value.note_count} notes</div>
			<div style={hoverBodyStyle}>{result.value.total_words} words total</div>
		</div>
	);
};

const panelBodyStyle: CSSProperties = {
	padding: 16,
	background: "#eef6ff",
	color: "#10355c",
	height: "100%",
	overflow: "auto",
};
const titleStyle: CSSProperties = { margin: 0, fontSize: 18 };
const shortIdLineStyle: CSSProperties = { marginTop: 8, fontSize: 11, color: "#3a6ea5" };
const dlStyle: CSSProperties = {
	marginTop: 12,
	display: "grid",
	gridTemplateColumns: "max-content 1fr",
	gap: "4px 16px",
};
const dtStyle: CSSProperties = { fontWeight: 600 };
const ddStyle: CSSProperties = { margin: 0 };
const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	background: "#eef6ff",
	border: "1px solid #9cc3ec",
	color: "#10355c",
	fontSize: 12,
};
const linkTagStyle: CSSProperties = { fontWeight: 600 };
const linkLabelStyle: CSSProperties = { fontSize: 11 };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#eef6ff",
	color: "#3a6ea5",
	fontSize: 12,
};
const hoverCardStyle: CSSProperties = {
	padding: 8,
	background: "#eef6ff",
	border: "1px solid #9cc3ec",
	borderRadius: 6,
	color: "#10355c",
	fontSize: 12,
	minWidth: 160,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600 };
const hoverBodyStyle: CSSProperties = { fontSize: 11, color: "#3a6ea5", marginTop: 4 };
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { color: "#3a6ea5", fontStyle: "italic", fontSize: 12 };

export default {
	stats: {
		[FullPanel.uuid]: StatsFullPanel,
		[BlockLink.uuid]: StatsBlockLink,
		[InlineLink.uuid]: StatsInlineLink,
		[HoverPreview.uuid]: StatsHoverPreview,
	},
};
