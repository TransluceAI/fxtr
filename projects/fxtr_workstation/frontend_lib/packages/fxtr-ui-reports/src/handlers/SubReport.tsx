/**
 * Renderers for `SubReport` across all five dispatch keys
 * (the four workstation keys plus `Figure`).
 *
 * Same per-renderer pattern as `RootReport.tsx`: narrow state
 * → build a `LoadResult` atom (+ figure storage for the panel
 * /figure renderers) → useAtomValue → render.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
import {
	type FullPanelProps,
	type HoverPreviewProps,
	type LinkProps,
	RefLink,
	useWorkstationNavigate,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { useMemo } from "react";
import { type FigureStorageMap, makeFigureStorage } from "../figure_storage.js";
import type { FigureProps } from "../keys.js";
import { ReportTreeRenderer } from "../tree_renderer.js";
import type { SubReportValue } from "../types.js";
import {
	errorStyle,
	figureBodyStyle,
	figureHeaderStyle,
	figureShellInteractiveStyle,
	hoverBodyStyle,
	hoverCardStyle,
	hoverTitleStyle,
	linkLabelStyle,
	linkPillStyle,
	linkTagStyle,
	mutedStyle,
	panelBodyStyle,
	sectionLabelStyle,
} from "./shared.js";

// ----- FullPanel -----

interface SubReportPanelState {
	readonly load: OpResultStub<SubReportValue> | null;
	readonly overviewFigures: FigureStorageMap;
	readonly detailFigures: FigureStorageMap;
}

const INITIAL_PANEL_STATE: SubReportPanelState = {
	load: null,
	overviewFigures: {},
	detailFigures: {},
};

export const SubReportFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const { resultAtom, overviewStorage, detailStorage } = useMemo(() => {
		const panelStateAtom = atomWithDefault<SubReportPanelState>(
			ownStorage as FxtrSessionAtom<SubReportPanelState | null>,
			INITIAL_PANEL_STATE,
		);
		const ourStateAtom = subAtom(panelStateAtom, "load");
		return {
			resultAtom: makeLoadResultAtom<SubReportValue>(ourStateAtom, stub),
			overviewStorage: makeFigureStorage(subAtom(panelStateAtom, "overviewFigures")),
			detailStorage: makeFigureStorage(subAtom(panelStateAtom, "detailFigures")),
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
					<h1 style={sectionLabelStyle}>{result.value.label}</h1>
					<ReportTreeRenderer document={result.value.overview} figureStorage={overviewStorage} />
					<ReportTreeRenderer document={result.value.detail} figureStorage={detailStorage} />
				</>
			)}
		</div>
	);
};

// ----- Figure -----

interface SubReportFigureState {
	readonly load: OpResultStub<SubReportValue> | null;
	readonly overviewFigures: FigureStorageMap;
}

const INITIAL_FIGURE_STATE: SubReportFigureState = { load: null, overviewFigures: {} };

export const SubReportFigure: DispatchedComponent<EntityRefStub, FigureProps> = ({
	stub,
	ownStorage,
}) => {
	const { resultAtom, overviewStorage } = useMemo(() => {
		const figStateAtom = atomWithDefault<SubReportFigureState>(
			ownStorage as FxtrSessionAtom<SubReportFigureState | null>,
			INITIAL_FIGURE_STATE,
		);
		const ourStateAtom = subAtom(figStateAtom, "load");
		return {
			resultAtom: makeLoadResultAtom<SubReportValue>(ourStateAtom, stub),
			overviewStorage: makeFigureStorage(subAtom(figStateAtom, "overviewFigures")),
		};
	}, [ownStorage, stub]);

	const result = useAtomValue(resultAtom);

	const navigate = useWorkstationNavigate();
	const onClick = (e: React.MouseEvent<HTMLDivElement>): void => {
		// Skip navigation if the user has selected text (e.g.
		// they're click-dragging inside the prose to copy something).
		const sel = typeof window !== "undefined" ? window.getSelection() : null;
		if (sel && sel.toString().length > 0) return;
		// Reflinks / interactive content inside the body call
		// e.stopPropagation in their own handlers — they don't reach
		// this point.
		e.preventDefault();
		e.stopPropagation();
		navigate(stub);
	};

	return (
		<div
			style={figureShellInteractiveStyle}
			onClick={onClick}
			onKeyDown={(e) => {
				if (e.key === "Enter" || e.key === " ")
					onClick(e as unknown as React.MouseEvent<HTMLDivElement>);
			}}
			role="link"
			tabIndex={0}
			title="Click to open this sub-report"
		>
			<div style={figureHeaderStyle}>
				<span>sub-report</span>
				<RefLink stub={stub} as="inline" />
			</div>
			<div style={figureBodyStyle}>
				{result.kind === "errored" ? (
					<span style={errorStyle}>load failed: {result.message}</span>
				) : result.kind === "loading" ? (
					<span style={mutedStyle}>…</span>
				) : (
					<ReportTreeRenderer document={result.value.overview} figureStorage={overviewStorage} />
				)}
			</div>
		</div>
	);
};

// ----- BlockLink -----

export const SubReportBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<SubReportValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<SubReportValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.label : "…");
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>sub-report</span>
			<span style={linkLabelStyle}>{label}</span>
		</span>
	);
};

// ----- InlineLink -----

export const SubReportInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<SubReportValue> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<SubReportValue>(sharedAtom, stub),
		[sharedAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	const label = linkText ?? (result.kind === "filled" ? result.value.label : "…");
	return <span style={linkLabelStyle}>{label}</span>;
};

// ----- HoverPreview -----

interface SubReportHoverState {
	readonly load: OpResultStub<SubReportValue> | null;
	readonly overviewFigures: FigureStorageMap;
}

const INITIAL_HOVER_STATE: SubReportHoverState = { load: null, overviewFigures: {} };

export const SubReportHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<SubReportHoverState>(INITIAL_HOVER_STATE);
	const { resultAtom, overviewStorage } = useMemo(
		() => ({
			resultAtom: makeLoadResultAtom<SubReportValue>(subAtom(sharedAtom, "load"), stub),
			overviewStorage: makeFigureStorage(subAtom(sharedAtom, "overviewFigures")),
		}),
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
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>{result.value.label}</div>
			<div style={hoverBodyStyle}>
				<ReportTreeRenderer document={result.value.overview} figureStorage={overviewStorage} />
			</div>
		</div>
	);
};
