/**
 * Frame rendering: a dataframe cell's value is the canonical **frame
 * entity** (`fxtr_experiment/frame_encoding.py` — self-describing:
 * tagged schema + rows in canonical order), so rendering one needs no
 * new wire surface at all — load the entity the report already
 * references (`ctx.allocateLoad`) and decode its value client-side.
 *
 * Pieces:
 * - `decodeFrameValue` / `formatFrameCell` — the pure decode of the
 *   loaded entity value (tagged `[name, kind]` / `[name, kind, true]`
 *   columns; row arrays; bytes arrive as `Uint8Array` via the escape
 *   codec) and the display formatting. Malformed shapes return `null`
 *   (callers fall back to the ref link — never a crash).
 * - `FrameTableView` — the table (schema header, first
 *   `PREVIEW_ROWS` rows with a show-all expander, row count).
 *   Truncation is purely a display choice; the whole entity is already
 *   client-side (accepted v0 trade — the big-frame story is deferred
 *   with chunked cells).
 * - `DrawerFrameTable` — the cell-drawer embed: keyed load slots in
 *   the panel state (`PanelState.frameLoads`, capped — see
 *   `pruneFrameLoads`).
 * - `FrameFullPanel` / `FrameHoverPreview` — dispatch-entry renderers
 *   for the frame entity type (`index.tsx` exports them under the
 *   `frame_v0` subtype key; they go live when the deferred nix wiring
 *   registers the module for the type id — until then the drawer embed
 *   is the live path, and bare frame refs fall back to the generic
 *   entity panel, which still shows the raw value).
 *
 * Display caveats (platform-wide, not frame-specific): NaN floats
 * arrive as `null` through the JSON transport, and int64 values beyond
 * 2^53 lose precision — both display-only; the durable truth stays the
 * canonical entity.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub, ResultStatus } from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import type { DispatchedComponent } from "@fxtr-ui/react-dispatch";
import { atomWithDefault } from "@fxtr-ui/state-util";
import type { FullPanelProps, HoverPreviewProps } from "@fxtr-ui/workstation-dispatch";
import { RefLink } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, type ReactNode, useEffect, useMemo, useState } from "react";

// -- the pure decode ---------------------------------------------------------

export interface FrameColumn {
	readonly name: string;
	readonly kind: string;
	readonly nullable: boolean;
}

export interface FrameModel {
	readonly columns: readonly FrameColumn[];
	readonly rows: readonly (readonly unknown[])[];
}

function decodeColumn(v: unknown): FrameColumn | null {
	if (!Array.isArray(v) || v.length < 2 || v.length > 3) return null;
	const [name, kind, nullable] = v as readonly unknown[];
	if (typeof name !== "string" || typeof kind !== "string") return null;
	if (v.length === 3 && nullable !== true) return null;
	return { name, kind, nullable: v.length === 3 };
}

/**
 * Decode a loaded frame entity's value into a table model.
 *
 * Expects the stored form (tolerating extra keys like `$fxtr.type`):
 * `schema` = tagged column list, `rows` = arrays in schema column
 * order. Returns `null` on anything else — the caller falls back to
 * the ref link, so a malformed value degrades, never crashes.
 */
export function decodeFrameValue(value: unknown): FrameModel | null {
	if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
	const { schema, rows } = value as { readonly schema?: unknown; readonly rows?: unknown };
	if (!Array.isArray(schema) || !Array.isArray(rows)) return null;
	const columns: FrameColumn[] = [];
	for (const c of schema) {
		const col = decodeColumn(c);
		if (col === null) return null;
		columns.push(col);
	}
	if (columns.length === 0) return null; // zero-column schemas are rejected at config creation
	for (const r of rows) {
		if (!Array.isArray(r) || r.length !== columns.length) return null;
	}
	return { columns, rows: rows as readonly (readonly unknown[])[] };
}

/** One cell value as display text (lossy on purpose — it's a table
 * cell, and the entity itself is the truth). */
export function formatFrameCell(v: unknown): string {
	if (v === null || v === undefined) return "∅";
	if (v instanceof Uint8Array) {
		const head = Array.from(v.slice(0, 8), (b) => b.toString(16).padStart(2, "0")).join("");
		return `0x${head}${v.length > 8 ? "…" : ""} (${v.length}B)`;
	}
	if (typeof v === "string") return v;
	return String(v);
}

// -- the table ---------------------------------------------------------------

export const PREVIEW_ROWS = 20;

export function FrameTableView({ model }: { readonly model: FrameModel }): ReactNode {
	const [showAll, setShowAll] = useState(false);
	const rows = showAll ? model.rows : model.rows.slice(0, PREVIEW_ROWS);
	return (
		<div style={tableBoxStyle}>
			<table style={tableStyle}>
				<thead>
					<tr>
						{model.columns.map((c) => (
							<th key={c.name} style={thStyle} title={`${c.kind}${c.nullable ? ", nullable" : ""}`}>
								{c.name}
								<span style={kindStyle}>
									{c.kind}
									{c.nullable ? "?" : ""}
								</span>
							</th>
						))}
					</tr>
				</thead>
				<tbody>
					{rows.map((r, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: canonical order is stable for one loaded value
						<tr key={i}>
							{r.map((v, j) => (
								// biome-ignore lint/suspicious/noArrayIndexKey: cells are positional by schema
								<td key={j} style={tdStyle}>
									{formatFrameCell(v)}
								</td>
							))}
						</tr>
					))}
				</tbody>
			</table>
			<div style={footerStyle}>
				{model.rows.length} row{model.rows.length === 1 ? "" : "s"}
				{model.rows.length > PREVIEW_ROWS && (
					<button type="button" style={expandStyle} onClick={() => setShowAll((s) => !s)}>
						{showAll ? `show first ${PREVIEW_ROWS}` : `show all ${model.rows.length}`}
					</button>
				)}
			</div>
		</div>
	);
}

/** Gate a load result into the table (decode-failure → a muted note;
 * the ref link beside the embed keeps the content reachable). */
function LoadedFrame({ op }: { readonly op: OpResultStub<unknown> }): ReactNode {
	const status: ResultStatus<unknown> = useResult(op);
	if (status.status === "pending") return <span style={mutedStyle}>loading rows…</span>;
	if (status.status === "errored")
		return <span style={errorStyle}>frame load failed: {status.message}</span>;
	const model = decodeFrameValue(status.value);
	if (model === null)
		return <span style={mutedStyle}>unrecognized frame form (open the ref for the raw value)</span>;
	return <FrameTableView model={model} />;
}

// -- the drawer embed ---------------------------------------------------------

/** The drawer's load slots: entity shortId → `OpResultStub`, capped so
 * browsing many frame cells doesn't grow the panel state unboundedly
 * (loads are interned session-side, so re-allocating a dropped ref is
 * cheap). */
export type FrameLoads = Readonly<Record<string, unknown>>;

const FRAME_LOADS_CAP = 8;

function pruneFrameLoads(loads: FrameLoads): FrameLoads {
	const keys = Object.keys(loads);
	if (keys.length < FRAME_LOADS_CAP) return loads;
	const keep = keys.slice(keys.length - (FRAME_LOADS_CAP - 1));
	return Object.fromEntries(keep.map((k) => [k, loads[k]]));
}

export function DrawerFrameTable({
	stub,
	loadsAtom,
}: {
	readonly stub: EntityRefStub;
	readonly loadsAtom: FxtrSessionAtom<FrameLoads>;
}): ReactNode {
	const loads = useAtomValue(loadsAtom);
	const setLoads = useSetAtom(loadsAtom);
	const key = stub.shortId;
	useEffect(() => {
		setLoads((prev, ctx) =>
			prev[key] !== undefined
				? prev
				: { ...pruneFrameLoads(prev), [key]: ctx.allocateLoad<unknown>(stub) },
		);
	}, [key, stub, setLoads]);
	const op = loads[key] as OpResultStub<unknown> | undefined;
	if (op === undefined) return <span style={mutedStyle}>loading rows…</span>;
	return <LoadedFrame op={op} />;
}

// -- dispatch-entry renderers (live once the nix wiring registers them) --------

interface FrameSlotStorage {
	readonly load: OpResultStub<unknown> | null;
}

const FRAME_SLOT_INITIAL: FrameSlotStorage = { load: null };

/** The single-slot load pattern (the generic entity fallback's shape)
 * over the dispatched `ownStorage`. */
function useOwnLoad(stub: EntityRefStub, ownStorage: unknown): OpResultStub<unknown> | null {
	const typed = useMemo(
		() =>
			atomWithDefault<FrameSlotStorage>(
				ownStorage as FxtrSessionAtom<FrameSlotStorage | null>,
				FRAME_SLOT_INITIAL,
			),
		[ownStorage],
	);
	const state = useAtomValue(typed);
	const setState = useSetAtom(typed);
	useEffect(() => {
		setState((prev, ctx) =>
			prev.load !== null ? prev : { load: ctx.allocateLoad<unknown>(stub) },
		);
	}, [stub, setState]);
	return state.load;
}

export const FrameFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const load = useOwnLoad(stub, ownStorage);
	return (
		<div style={panelStyle}>
			<div style={panelHeaderStyle}>
				<span style={panelTitleStyle}>frame</span>
				<RefLink stub={stub} as="inline" />
			</div>
			{load !== null ? <LoadedFrame op={load} /> : <span style={mutedStyle}>loading rows…</span>}
		</div>
	);
};

/** Hover previews carry no storage slot (`HoverPreviewProps` is
 * `NoProps`), so no load happens here — a static card; the FullPanel
 * is where the table lives. */
export const FrameHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => (
	<div style={hoverStyle}>
		<span style={{ fontFamily: "var(--fxtr-mono, ui-monospace, monospace)" }}>{stub.shortId}</span>
		<div style={mutedStyle}>a frame (row multiset) — open for the table</div>
	</div>
);

// -- styles --------------------------------------------------------------------

const tableBoxStyle: CSSProperties = {
	overflowX: "auto",
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	maxWidth: "100%",
};
const tableStyle: CSSProperties = {
	borderCollapse: "collapse",
	fontSize: 12,
	fontFamily: "var(--fxtr-mono, ui-monospace, monospace)",
	width: "100%",
};
const thStyle: CSSProperties = {
	textAlign: "left",
	padding: "4px 10px",
	borderBottom: "1px solid var(--fxtr-border, #ccd5df)",
	whiteSpace: "nowrap",
};
const kindStyle: CSSProperties = {
	marginLeft: 6,
	fontWeight: 400,
	opacity: 0.55,
	fontSize: 10,
};
const tdStyle: CSSProperties = {
	padding: "2px 10px",
	borderBottom: "1px solid var(--fxtr-border-subtle, #e6ebf0)",
	whiteSpace: "nowrap",
	maxWidth: 320,
	overflow: "hidden",
	textOverflow: "ellipsis",
};
const footerStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: 10,
	padding: "3px 10px",
	fontSize: 11,
	opacity: 0.7,
};
const expandStyle: CSSProperties = {
	fontSize: 11,
	padding: "1px 8px",
	cursor: "pointer",
};
const mutedStyle: CSSProperties = { fontStyle: "italic", opacity: 0.6, fontSize: 12 };
const errorStyle: CSSProperties = { color: "var(--fxtr-error, #b3261e)", fontSize: 12 };
const panelStyle: CSSProperties = {
	padding: 12,
	display: "flex",
	flexDirection: "column",
	gap: 8,
	overflow: "auto",
	height: "100%",
};
const panelHeaderStyle: CSSProperties = { display: "flex", alignItems: "center", gap: 8 };
const panelTitleStyle: CSSProperties = { fontWeight: 600, fontSize: 13 };
const hoverStyle: CSSProperties = {
	padding: 8,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	fontSize: 12,
	maxWidth: 320,
};
