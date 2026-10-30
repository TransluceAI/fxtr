/**
 * Renderers for `RootReport` across the four workstation
 * dispatch keys.
 *
 * Each renderer follows the same pattern:
 *
 *   1. Get the atom(s) the renderer's session state lives in
 *      — either narrowed from `ownStorage` (FullPanel) or via
 *      `useDispatchSharedAtom` (link / hover keys).
 *   2. Inside `useMemo`, declare the renderer-specific state
 *      shape, project the slot atoms it needs, build a
 *      `LoadResult<T>` atom via `makeLoadResultAtom`, and
 *      (where needed) a figure-storage closure.
 *   3. `useAtomValue` the result atom and any other derived
 *      atoms.
 *   4. Render — switch on `result.kind` for distinct UI per
 *      state, or just fall back to a placeholder if there's
 *      nothing kind-specific to render.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
import type { FullPanelProps, HoverPreviewProps, LinkProps } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { useMemo } from "react";
import { type FigureStorageMap, makeFigureStorage } from "../figure_storage.js";
import { ReportTreeRenderer } from "../tree_renderer.js";
import type { RootReportValue } from "../types.js";
import {
	errorStyle,
	hoverBodyStyle,
	hoverCardStyle,
	hoverTitleStyle,
	linkLabelStyle,
	linkPillStyle,
	linkTagStyle,
	mutedStyle,
	panelBodyStyle,
	reportSummaryStyle,
	reportTitleStyle,
} from "./shared.js";

// ----- FullPanel -----

interface RootReportPanelState {
	readonly load: OpResultStub<RootReportValue> | null;
	readonly figures: FigureStorageMap;
}

const INITIAL_PANEL_STATE: RootReportPanelState = { load: null, figures: {} };

export const RootReportFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const { resultAtom, figureStorage } = useMemo(() => {
		const panelStateAtom = atomWithDefault<RootReportPanelState>(
			ownStorage as FxtrSessionAtom<RootReportPanelState | null>,
			INITIAL_PANEL_STATE,
		);
		const ourStateAtom = subAtom(panelStateAtom, "load");
		const figuresAtom = subAtom(panelStateAtom, "figures");
		return {
			resultAtom: makeLoadResultAtom<RootReportValue>(ourStateAtom, stub),
			figureStorage: makeFigureStorage(figuresAtom),
		};
	}, [ownStorage, stub]);

	const result = useAtomValue(resultAtom);

	return (
		<div className="fw-panel__body" style={panelBodyStyle}>
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>…</span>
			) : (
				<>
					<h1 style={reportTitleStyle}>{result.value.title}</h1>
					{result.value.summary && <p style={reportSummaryStyle}>{result.value.summary}</p>}
					<ReportTreeRenderer document={result.value.contents} figureStorage={figureStorage} />
				</>
			)}
		</div>
	);
};

// ----- BlockLink -----

export const RootReportBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<RootReportValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<RootReportValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>report</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

// ----- InlineLink -----

export const RootReportInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<RootReportValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<RootReportValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.title : "…");
	return <span style={linkLabelStyle}>{label}</span>;
};

// ----- HoverPreview -----

export const RootReportHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<RootReportValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<RootReportValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);

	if (result.kind === "errored") {
		return (
			<div style={hoverCardStyle}>
				<div style={errorStyle}>load failed: {result.message}</div>
			</div>
		);
	}
	if (result.kind === "loading") {
		return (
			<div style={hoverCardStyle}>
				<span style={mutedStyle}>…</span>
			</div>
		);
	}
	const summary =
		result.value.summary.length > 240
			? `${result.value.summary.slice(0, 240)}…`
			: result.value.summary;
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>{result.value.title}</div>
			{summary && <div style={hoverBodyStyle}>{summary}</div>}
		</div>
	);
};
