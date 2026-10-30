/**
 * Renderers for the entity types in
 * `packages/docent_fxtr_proxy/src/docent_fxtr_proxy/__init__.py`:
 *
 *   - DqlResult        — rows + columns from `execute_dql`
 *   - DqlSchema        — table/column shape from `get_dql_schema`
 *   - MetadataFields   — agent-run metadata listing from
 *                        `get_metadata_fields`
 *
 * All three follow the same pattern the `renderers/note/` example
 * established — see that file for a longer walkthrough of the
 * slot-atom / `makeLoadResultAtom` flow.
 *
 * Visual conventions:
 *
 *   - FullPanel: a card-style layout with a header (entity tag +
 *     short id) and a body tailored to each type.
 *   - HoverPreview: a compact two-line summary.
 *   - BlockLink / InlineLink: a small black-outlined pill with a
 *     `tag : shortId` body, per the demo's request that the link
 *     renderers still surface the short id but be visually
 *     distinct from the wrapped reflinks the rest of the renderer
 *     set produces.
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

// ===== Loaded shapes (mirror the python dataclasses) =====

type CellValue = unknown;

interface DqlResultLoaded {
	readonly collection_id: string;
	readonly query: string;
	readonly columns: readonly string[];
	readonly rows: readonly (readonly CellValue[])[];
}

interface DqlSchemaLoaded {
	readonly collection_id: string;
	readonly schema: unknown;
}

interface MetadataFieldsLoaded {
	readonly collection_id: string;
	readonly include_sample_values: boolean;
	readonly sample_limit: number;
	readonly fields: readonly unknown[];
	readonly total_runs: number | null;
}

// ===== Shared link pill (the user-requested black outlined chip) =====

const TAG_COPY = {
	dql_result: "dql_result",
	dql_schema: "dql_schema",
	metadata_fields: "metadata_fields",
} as const;

type EntityTag = keyof typeof TAG_COPY;

function LinkPill(props: {
	readonly tag: EntityTag;
	readonly shortId: string;
	readonly inline?: boolean;
}) {
	const style = props.inline ? inlinePillStyle : blockPillStyle;
	return (
		<span style={style}>
			<span style={pillTagStyle}>{TAG_COPY[props.tag]}</span>
			<span style={pillSepStyle}>·</span>
			<code style={pillShortIdStyle}>{props.shortId}</code>
		</span>
	);
}

// ===== DqlResult =====

interface DqlResultPanelState {
	readonly load: OpResultStub<DqlResultLoaded> | null;
}
const INITIAL_DQL_RESULT_STATE: DqlResultPanelState = { load: null };

const MAX_ROWS_IN_PANEL = 200;
const MAX_CELL_CHARS = 80;

export const DqlResultFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<DqlResultPanelState>(
			ownStorage as FxtrSessionAtom<DqlResultPanelState | null>,
			INITIAL_DQL_RESULT_STATE,
		);
		return makeLoadResultAtom<DqlResultLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	return (
		<div style={panelBodyStyle}>
			<PanelHeader tag="dql_result" shortId={stub.shortId} />
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading DQL result…</span>
			) : (
				<DqlResultBody value={result.value} />
			)}
		</div>
	);
};

function DqlResultBody({ value }: { readonly value: DqlResultLoaded }) {
	const rowCount = value.rows.length;
	const colCount = value.columns.length;
	const shownRows = value.rows.slice(0, MAX_ROWS_IN_PANEL);
	const truncated = rowCount > MAX_ROWS_IN_PANEL;

	return (
		<>
			<div style={metaLineStyle}>
				collection <code style={inlineCodeStyle}>{value.collection_id}</code>
			</div>
			<div style={metaLineStyle}>
				{rowCount} row{rowCount === 1 ? "" : "s"} × {colCount} col
				{colCount === 1 ? "" : "s"}
				{truncated ? ` (showing first ${MAX_ROWS_IN_PANEL})` : ""}
			</div>
			<details style={querySectionStyle}>
				<summary style={querySummaryStyle}>query</summary>
				<pre style={queryPreStyle}>{value.query.trim()}</pre>
			</details>
			{colCount === 0 ? (
				<div style={mutedStyle}>(no columns)</div>
			) : (
				<div style={tableWrapperStyle}>
					<table style={tableStyle}>
						<thead>
							<tr>
								{value.columns.map((c) => (
									<th key={c} style={thStyle}>
										{c}
									</th>
								))}
							</tr>
						</thead>
						<tbody>
							{shownRows.map((row, i) => (
								<tr key={i} style={i % 2 === 1 ? rowAltStyle : undefined}>
									{value.columns.map((_, j) => (
										<td key={j} style={tdStyle}>
											{renderCell(row[j])}
										</td>
									))}
								</tr>
							))}
						</tbody>
					</table>
				</div>
			)}
		</>
	);
}

function renderCell(v: CellValue) {
	if (v === null || v === undefined) return <span style={nullCellStyle}>NULL</span>;
	if (typeof v === "object") {
		const s = JSON.stringify(v);
		return <code style={objectCellStyle}>{truncate(s, MAX_CELL_CHARS)}</code>;
	}
	const s = String(v);
	return <span>{truncate(s, MAX_CELL_CHARS)}</span>;
}

function truncate(s: string, n: number) {
	return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export const DqlResultHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DqlResultLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DqlResultLoaded>(sharedAtom, stub),
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
				<span style={mutedStyle}>loading DQL result…</span>
			</div>
		);
	}
	const v = result.value;
	const querySnippet = truncate(v.query.trim().replace(/\s+/g, " "), 80);
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>
				DqlResult — {v.rows.length} × {v.columns.length}
			</div>
			<div style={hoverSubtitleStyle}>{querySnippet || "(empty query)"}</div>
		</div>
	);
};

export const DqlResultBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub }) => (
	<LinkPill tag="dql_result" shortId={stub.shortId} />
);
export const DqlResultInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub }) => (
	<LinkPill tag="dql_result" shortId={stub.shortId} inline />
);

// ===== DqlSchema =====

interface DqlSchemaPanelState {
	readonly load: OpResultStub<DqlSchemaLoaded> | null;
}
const INITIAL_DQL_SCHEMA_STATE: DqlSchemaPanelState = { load: null };

export const DqlSchemaFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<DqlSchemaPanelState>(
			ownStorage as FxtrSessionAtom<DqlSchemaPanelState | null>,
			INITIAL_DQL_SCHEMA_STATE,
		);
		return makeLoadResultAtom<DqlSchemaLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	return (
		<div style={panelBodyStyle}>
			<PanelHeader tag="dql_schema" shortId={stub.shortId} />
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading DQL schema…</span>
			) : (
				<DqlSchemaBody value={result.value} />
			)}
		</div>
	);
};

function DqlSchemaBody({ value }: { readonly value: DqlSchemaLoaded }) {
	return (
		<>
			<div style={metaLineStyle}>
				collection <code style={inlineCodeStyle}>{value.collection_id}</code>
			</div>
			<pre style={jsonPreStyle}>{JSON.stringify(value.schema, null, 2)}</pre>
		</>
	);
}

export const DqlSchemaHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<DqlSchemaLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<DqlSchemaLoaded>(sharedAtom, stub),
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
				<span style={mutedStyle}>loading DQL schema…</span>
			</div>
		);
	}
	const tableCount = countSchemaTables(result.value.schema);
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>DqlSchema</div>
			<div style={hoverSubtitleStyle}>
				collection {truncate(result.value.collection_id, 36)}
				{tableCount !== null ? ` · ${tableCount} table${tableCount === 1 ? "" : "s"}` : ""}
			</div>
		</div>
	);
};

function countSchemaTables(schema: unknown): number | null {
	// Best-effort summary: the upstream shape is "whatever Docent returns"
	// so we just peek at a couple of common keys.
	if (!schema || typeof schema !== "object") return null;
	const s = schema as Record<string, unknown>;
	if (Array.isArray(s.tables)) return s.tables.length;
	if (s.tables && typeof s.tables === "object") return Object.keys(s.tables).length;
	return null;
}

export const DqlSchemaBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub }) => (
	<LinkPill tag="dql_schema" shortId={stub.shortId} />
);
export const DqlSchemaInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub }) => (
	<LinkPill tag="dql_schema" shortId={stub.shortId} inline />
);

// ===== MetadataFields =====

interface MetadataFieldsPanelState {
	readonly load: OpResultStub<MetadataFieldsLoaded> | null;
}
const INITIAL_METADATA_FIELDS_STATE: MetadataFieldsPanelState = { load: null };

export const MetadataFieldsFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<MetadataFieldsPanelState>(
			ownStorage as FxtrSessionAtom<MetadataFieldsPanelState | null>,
			INITIAL_METADATA_FIELDS_STATE,
		);
		return makeLoadResultAtom<MetadataFieldsLoaded>(subAtom(panelAtom, "load"), stub);
	}, [ownStorage, stub]);
	const result = useAtomValue(resultAtom);

	return (
		<div style={panelBodyStyle}>
			<PanelHeader tag="metadata_fields" shortId={stub.shortId} />
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading metadata fields…</span>
			) : (
				<MetadataFieldsBody value={result.value} />
			)}
		</div>
	);
};

function MetadataFieldsBody({ value }: { readonly value: MetadataFieldsLoaded }) {
	// Defensive: older host versions stored the wrapping {fields, total_runs}
	// dict under `value.fields` instead of unwrapping. Tolerate both shapes
	// in the renderer so a stale persisted entity still displays.
	const { fieldsArr, totalRuns } = normalizeFields(value);
	return (
		<>
			<div style={metaLineStyle}>
				collection <code style={inlineCodeStyle}>{value.collection_id}</code>
			</div>
			<div style={metaLineStyle}>
				{fieldsArr.length} field{fieldsArr.length === 1 ? "" : "s"}
				{totalRuns !== null && ` · ${totalRuns} runs aggregated`}
				{" · samples "}
				{value.include_sample_values ? `included (≤${value.sample_limit})` : "not included"}
			</div>
			<div style={fieldListStyle}>
				{fieldsArr.map((field, i) => (
					<FieldRow key={i} field={field} />
				))}
			</div>
		</>
	);
}

function normalizeFields(value: MetadataFieldsLoaded): {
	fieldsArr: readonly unknown[];
	totalRuns: number | null;
} {
	const declaredTotal = typeof value.total_runs === "number" ? value.total_runs : null;
	if (Array.isArray(value.fields)) {
		return { fieldsArr: value.fields, totalRuns: declaredTotal };
	}
	if (value.fields && typeof value.fields === "object") {
		const wrapper = value.fields as Record<string, unknown>;
		const inner = Array.isArray(wrapper.fields) ? wrapper.fields : [];
		const wrapperTotal =
			typeof wrapper.total_runs === "number" ? (wrapper.total_runs as number) : null;
		return { fieldsArr: inner, totalRuns: declaredTotal ?? wrapperTotal };
	}
	return { fieldsArr: [], totalRuns: declaredTotal };
}

function FieldRow({ field }: { readonly field: unknown }) {
	if (!field || typeof field !== "object") {
		return <div style={fieldRowStyle}>{String(field)}</div>;
	}
	const f = field as Record<string, unknown>;
	const name = typeof f.name === "string" ? f.name : JSON.stringify(f.name);
	const ftype = typeof f.type === "string" ? f.type : f.type ? JSON.stringify(f.type) : undefined;
	const samples = Array.isArray(f.sample_values) ? (f.sample_values as unknown[]) : null;
	const totalUnique =
		typeof f.total_unique_values === "number" ? (f.total_unique_values as number) : null;
	return (
		<div style={fieldRowStyle}>
			<div style={fieldNameRowStyle}>
				<code style={fieldNameStyle}>{name}</code>
				{ftype && <span style={fieldTypeStyle}>{ftype}</span>}
				{totalUnique !== null && (
					<span style={fieldCardinalityStyle}>{totalUnique} unique</span>
				)}
			</div>
			{samples && samples.length > 0 && (
				<div style={sampleRowStyle}>
					{samples.slice(0, 6).map((s, i) => (
						<code key={i} style={sampleChipStyle}>
							{truncate(typeof s === "string" ? s : JSON.stringify(s), 40)}
						</code>
					))}
					{samples.length > 6 && <span style={mutedStyle}>+{samples.length - 6}</span>}
				</div>
			)}
		</div>
	);
}

export const MetadataFieldsHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<MetadataFieldsLoaded> | null>(null);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<MetadataFieldsLoaded>(sharedAtom, stub),
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
				<span style={mutedStyle}>loading metadata fields…</span>
			</div>
		);
	}
	const v = result.value;
	const { fieldsArr, totalRuns } = normalizeFields(v);
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>
				MetadataFields — {fieldsArr.length} field{fieldsArr.length === 1 ? "" : "s"}
				{totalRuns !== null && ` · ${totalRuns} runs`}
			</div>
			<div style={hoverSubtitleStyle}>
				collection {truncate(v.collection_id, 36)}
				{v.include_sample_values ? ` · samples ≤${v.sample_limit}` : ""}
			</div>
		</div>
	);
};

export const MetadataFieldsBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
}) => <LinkPill tag="metadata_fields" shortId={stub.shortId} />;
export const MetadataFieldsInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
}) => <LinkPill tag="metadata_fields" shortId={stub.shortId} inline />;

// ===== Shared layout =====

function PanelHeader(props: { readonly tag: EntityTag; readonly shortId: string }) {
	return (
		<div style={panelHeaderStyle}>
			<span style={panelHeaderTagStyle}>{TAG_COPY[props.tag]}</span>
			<code style={panelHeaderShortIdStyle}>{props.shortId}</code>
		</div>
	);
}

// ===== Styles =====

const PILL_BORDER = "1.5px solid #000";

const blockPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "3px 10px",
	borderRadius: 8,
	border: PILL_BORDER,
	background: "#fff",
	color: "#000",
	fontSize: 12,
	lineHeight: 1.2,
};
const inlinePillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 4,
	padding: "0px 6px",
	borderRadius: 6,
	border: PILL_BORDER,
	background: "#fff",
	color: "#000",
	fontSize: 11,
	lineHeight: 1.2,
	verticalAlign: "baseline",
};
const pillTagStyle: CSSProperties = {
	fontWeight: 600,
	letterSpacing: "0.02em",
};
const pillSepStyle: CSSProperties = { color: "#666" };
const pillShortIdStyle: CSSProperties = { fontFamily: "var(--fxtr-font-mono, monospace)" };

const panelBodyStyle: CSSProperties = {
	padding: 16,
	background: "#fafafa",
	color: "#111",
	height: "100%",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: 10,
};
const panelHeaderStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: 8,
	paddingBottom: 8,
	borderBottom: "1px solid #ddd",
};
const panelHeaderTagStyle: CSSProperties = {
	padding: "3px 10px",
	borderRadius: 8,
	border: PILL_BORDER,
	background: "#fff",
	fontSize: 12,
	fontWeight: 600,
};
const panelHeaderShortIdStyle: CSSProperties = {
	fontFamily: "var(--fxtr-font-mono, monospace)",
	fontSize: 11,
	color: "#555",
};
const metaLineStyle: CSSProperties = { fontSize: 12, color: "#444" };
const inlineCodeStyle: CSSProperties = {
	fontFamily: "var(--fxtr-font-mono, monospace)",
	fontSize: 11,
	background: "#eee",
	padding: "1px 4px",
	borderRadius: 3,
};

const querySectionStyle: CSSProperties = {
	border: "1px solid #ddd",
	borderRadius: 6,
	background: "#fff",
};
const querySummaryStyle: CSSProperties = {
	padding: "6px 10px",
	cursor: "pointer",
	fontSize: 12,
	fontWeight: 600,
	color: "#333",
};
const queryPreStyle: CSSProperties = {
	margin: 0,
	padding: "0 10px 10px",
	whiteSpace: "pre-wrap",
	fontFamily: "var(--fxtr-font-mono, monospace)",
	fontSize: 11,
	color: "#222",
};

const tableWrapperStyle: CSSProperties = {
	overflow: "auto",
	border: "1px solid #ddd",
	borderRadius: 6,
	background: "#fff",
};
const tableStyle: CSSProperties = {
	borderCollapse: "collapse",
	width: "100%",
	fontSize: 11,
};
const thStyle: CSSProperties = {
	textAlign: "left",
	padding: "6px 8px",
	background: "#f0f0f0",
	borderBottom: "1px solid #ddd",
	position: "sticky",
	top: 0,
	fontWeight: 600,
};
const tdStyle: CSSProperties = {
	padding: "4px 8px",
	borderBottom: "1px solid #eee",
	verticalAlign: "top",
	fontFamily: "var(--fxtr-font-mono, monospace)",
};
const rowAltStyle: CSSProperties = { background: "#fafafa" };
const nullCellStyle: CSSProperties = { color: "#999", fontStyle: "italic" };
const objectCellStyle: CSSProperties = { color: "#555" };

const jsonPreStyle: CSSProperties = {
	margin: 0,
	padding: 10,
	background: "#fff",
	border: "1px solid #ddd",
	borderRadius: 6,
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
	fontFamily: "var(--fxtr-font-mono, monospace)",
	fontSize: 11,
	color: "#222",
};

const fieldListStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 6 };
const fieldRowStyle: CSSProperties = {
	border: "1px solid #ddd",
	borderRadius: 6,
	background: "#fff",
	padding: "6px 10px",
	display: "flex",
	flexDirection: "column",
	gap: 4,
};
const fieldNameRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: 8,
	flexWrap: "wrap",
};
const fieldNameStyle: CSSProperties = {
	fontFamily: "var(--fxtr-font-mono, monospace)",
	fontSize: 12,
	color: "#111",
};
const fieldTypeStyle: CSSProperties = {
	fontSize: 10,
	textTransform: "uppercase",
	letterSpacing: "0.04em",
	color: "#555",
	padding: "1px 5px",
	border: "1px solid #ccc",
	borderRadius: 3,
};
const fieldCardinalityStyle: CSSProperties = { fontSize: 10, color: "#666" };
const sampleRowStyle: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 4 };
const sampleChipStyle: CSSProperties = {
	fontFamily: "var(--fxtr-font-mono, monospace)",
	fontSize: 10,
	background: "#f0f0f0",
	color: "#333",
	padding: "1px 5px",
	borderRadius: 3,
};

const hoverCardStyle: CSSProperties = {
	padding: 8,
	background: "#fff",
	border: PILL_BORDER,
	borderRadius: 8,
	color: "#111",
	fontSize: 12,
	minWidth: 200,
	maxWidth: 360,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600 };
const hoverSubtitleStyle: CSSProperties = {
	fontSize: 11,
	color: "#555",
	marginTop: 4,
	wordBreak: "break-word",
};

const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { color: "#777", fontStyle: "italic", fontSize: 12 };

// ===== Dispatch table =====

export default {
	dql_result: {
		[FullPanel.uuid]: DqlResultFullPanel,
		[BlockLink.uuid]: DqlResultBlockLink,
		[InlineLink.uuid]: DqlResultInlineLink,
		[HoverPreview.uuid]: DqlResultHoverPreview,
	},
	dql_schema: {
		[FullPanel.uuid]: DqlSchemaFullPanel,
		[BlockLink.uuid]: DqlSchemaBlockLink,
		[InlineLink.uuid]: DqlSchemaInlineLink,
		[HoverPreview.uuid]: DqlSchemaHoverPreview,
	},
	metadata_fields: {
		[FullPanel.uuid]: MetadataFieldsFullPanel,
		[BlockLink.uuid]: MetadataFieldsBlockLink,
		[InlineLink.uuid]: MetadataFieldsInlineLink,
		[HoverPreview.uuid]: MetadataFieldsHoverPreview,
	},
};
