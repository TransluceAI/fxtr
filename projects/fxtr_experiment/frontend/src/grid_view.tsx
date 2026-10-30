/**
 * The grid detail pane (right side): header (identity, verdicts,
 * targets on this grid, the stored config as a `RefLink`) over the
 * value-free cells table, with the selected row's `CellDrawer`
 * (`cell_drawer.tsx` — the labeled content channel) below it.
 *
 * Everything renders from the selected grid's watch mirror
 * (`derive/folds.ts`): the `GridMeta` bundle is the header's grid
 * facts AND the mutation grants (the grid's own grant + the parent
 * token — no attach walks, no token cache, no heal path: a broken
 * subscription resubscribes and fresh grants arrive in the fresh
 * meta), and the `cells` map is the table. A deleted grid renders the
 * explicit tombstone (`subject_gone` — the watch stays live, so a
 * re-created grid pops back in place). Targets/sourcing joins come
 * from the workflow mirror, keyed by coords text.
 */

import type { FxtrSessionAtom } from "@fxtr-ui/core";
import { RefLink } from "@fxtr-ui/workstation-dispatch";
import { type CSSProperties, type ReactNode, useMemo, useState } from "react";

import type { RunAction } from "./actions.js";
import { CellDrawer } from "./cell_drawer.js";
import {
	type CellBadge,
	type CellDemand,
	type CellSort,
	type CellSortKey,
	cellBadges,
	cellDemand,
	deriveCellRows,
	sameSortKey,
} from "./derive/cells.js";
import type { CellsMirror, WorkflowMirror } from "./derive/folds.js";
import type { GridTreeNode } from "./derive/tree.js";
import type { FrameLoads } from "./frame_table.js";
import type { WatchSlots } from "./panel_state.js";
import { viewSel } from "./wire/helpers.js";
import type {
	CellInfo,
	Currency,
	ExecStatus,
	GridMeta,
	GridSummary,
	GroundingVerdict,
	SourcingInfo,
	TargetInfo,
	ViewToken,
} from "./wire/types.js";

/** The drawer plumbing GridDetail threads down to the selected row
 * (the cell watch entry + the tree's internals-reveal state). */
export type DrawerProps = {
	readonly internalsRevealed: boolean;
	readonly onToggleInternals: () => void;
};

export function GridDetail({
	node,
	workflow,
	cellsEntry,
	cellEntry,
	selectedCell,
	onSelectCell,
	drawer,
	runAction,
	retryCells,
	retryCell,
	frameLoads,
}: {
	readonly node: GridTreeNode;
	readonly workflow: WorkflowMirror;
	readonly cellsEntry: WatchSlots["cells"];
	readonly cellEntry: WatchSlots["cell"];
	readonly selectedCell: string | null;
	readonly onSelectCell: (coordsText: string | null) => void;
	readonly drawer: DrawerProps;
	readonly runAction: RunAction;
	/** Drop the parked grid/cell watch entry (the driver resubscribes). */
	readonly retryCells: () => void;
	readonly retryCell: () => void;
	/** The drawer's frame-entity load slots (`frame_table.tsx`). */
	readonly frameLoads: FxtrSessionAtom<FrameLoads>;
}): ReactNode {
	const gridTargets = useMemo(
		() => workflow.targets.filter((t) => t.grid_path === node.path),
		[workflow.targets, node.path],
	);
	// Coords text → the record (the drawer's "Run" demands by record;
	// the table only needs membership).
	const targetsByCoords = useMemo(
		() => new Map(gridTargets.map((t) => [t.coords, t])),
		[gridTargets],
	);
	const gridSourcing = useMemo(
		() => workflow.sourcing.filter((s) => s.grid_path === node.path),
		[workflow.sourcing, node.path],
	);

	// Checkpoint grids are listable but never watchable/attachable
	// (step-history introspection is a separate affordance), and
	// implicit prefixes have no grid at all: the spec derivation in
	// `panel.tsx` never subscribes for either — render the explanation.
	if (node.summary === null || node.summary.kind === "checkpoint") {
		return (
			<div>
				<GridHeader
					path={node.path}
					nodeKey={node.key}
					summary={node.summary}
					internalsOf={node.internalsOf}
					gridTargets={gridTargets}
					meta={null}
					hasDescendants={node.children.length > 0}
					runAction={runAction}
				/>
				{node.summary === null ? (
					<div style={mutedStyle}>an implicit subtree prefix (no grid of its own)</div>
				) : (
					<div style={mutedStyle}>
						checkpoint grids hold recorded step entries and are not attachable — the owning task
						cell's drawer shows them (the generation report's checkpoint entries)
					</div>
				)}
			</div>
		);
	}

	// The entry is spec-matched before use: navigation writes the ui
	// selection first, and the driver replaces the entry a beat later —
	// a mismatched entry is the OLD grid's, not this one's.
	const entry = cellsEntry !== null && cellsEntry.spec.path === node.path ? cellsEntry : null;
	const header = (mirror: CellsMirror | null) => (
		<GridHeader
			path={node.path}
			nodeKey={node.key}
			summary={node.summary}
			internalsOf={node.internalsOf}
			gridTargets={gridTargets}
			meta={mirror?.meta ?? null}
			hasDescendants={node.children.length > 0}
			runAction={runAction}
		/>
	);
	if (entry !== null && entry.phase === "errored") {
		return (
			<div>
				{header(null)}
				<div style={errorStyle}>
					grid watch failed: {entry.message}{" "}
					<button type="button" style={buttonStyle} onClick={retryCells}>
						retry
					</button>
				</div>
			</div>
		);
	}
	if (entry === null || !entry.mirror.snapshotted) {
		return (
			<div>
				{header(null)}
				<div style={mutedStyle}>subscribing…</div>
			</div>
		);
	}
	if (entry.mirror.gone) {
		return (
			<div>
				{header(null)}
				<div style={tombstoneStyle}>
					this grid no longer exists — the watch stays live, so a re-creation at the same path will
					appear here
				</div>
			</div>
		);
	}
	return (
		<div>
			{header(entry.mirror)}
			<CellsTable
				node={node}
				mirror={entry.mirror}
				cellEntry={cellEntry}
				targetsByCoords={targetsByCoords}
				gridSourcing={gridSourcing}
				selectedCell={selectedCell}
				onSelectCell={onSelectCell}
				drawer={drawer}
				runAction={runAction}
				retryCell={retryCell}
				frameLoads={frameLoads}
			/>
		</div>
	);
}

function GridHeader({
	path,
	nodeKey,
	summary,
	internalsOf,
	gridTargets,
	meta,
	hasDescendants,
	runAction,
}: {
	readonly path: string;
	readonly nodeKey: string;
	readonly summary: GridSummary | null;
	readonly internalsOf: string | null;
	readonly gridTargets: readonly TargetInfo[];
	/** The watch mirror's grid bundle (null while unloaded / for
	 * unwatchable kinds); carries the grants the actions address. */
	readonly meta: GridMeta | null;
	/** The grid has child grids (drives recursive delete). */
	readonly hasDescendants: boolean;
	readonly runAction: RunAction;
}): ReactNode {
	// Grid facts prefer the meta (the grid's own watch — fresher) and
	// fall back to the summary row (always present for real grids).
	const grounding = meta?.grounding ?? summary?.grounding ?? null;
	const groundingReason = meta?.grounding_reason ?? summary?.grounding_reason ?? null;
	const sourcing = meta !== null ? meta.sourcing : (summary?.sourcing ?? null);
	return (
		<div style={headerStyle}>
			<div style={headerRowStyle}>
				<code style={pathStyle}>{path}</code>
				{summary && <span style={kindTagStyle}>{summary.kind}</span>}
				{summary && summary.axes.length > 0 && (
					<span style={axesStyle}>
						[{summary.axes.map((a) => `${a.name}:${a.kind}`).join(", ")}]
					</span>
				)}
				{internalsOf !== null && (
					<span style={kindTagStyle} title={`internals of the task grid ${internalsOf}`}>
						internals
					</span>
				)}
			</div>
			{summary && (
				<div style={headerRowStyle}>
					{/* Only surface grounding / sourcing when abnormal — the
					    healthy defaults (grounded, attached) are noise. */}
					{(grounding === "unanchored" || grounding === "orphaned") && (
						<Verdict
							label="grounding"
							value={grounding}
							reason={groundingReason}
							color={groundingColor(grounding)}
						/>
					)}
					{(sourcing === "dangling_missing" || sourcing === "dangling_mismatched") && (
						<span style={{ ...mutedInlineStyle, color: "#c04040" }}>sourcing: {sourcing}</span>
					)}
					{gridTargets.length > 0 && (
						<span style={mutedInlineStyle}>
							targets: {gridTargets.map((t) => t.coords).join(" ")}
						</span>
					)}
					<span style={mutedInlineStyle}>config:</span>
					<RefLink stub={meta?.config_id ?? summary.config_id} as="inline" />
				</div>
			)}
			{summary && internalsOf === null && meta !== null && meta.parent_token !== null && (
				<GridActions
					nodeKey={nodeKey}
					path={path}
					sourced={sourcing !== null}
					parentToken={meta.parent_token}
					hasDescendants={hasDescendants}
					isTask={summary.kind === "task"}
					runAction={runAction}
				/>
			)}
		</div>
	);
}

/** Grid-level intent + deletion, addressed through the PARENT subtree
 * token from the watch meta (`source_grid` / `revoke_grid_sourcing` /
 * `delete_grid` take the enclosing subtree + the child key; a root
 * grid has no parent token, so nothing shows there — nor for
 * internals, which are read-only from the top). Revoke and delete
 * expand to an inline confirm: revoke's force variant sweeps the
 * grid's cell records + targets (the cascade), and delete is
 * rmdir-style (refuses non-empty; the report of broken rule referrers
 * lands in the action log). */
function GridActions({
	nodeKey,
	path,
	sourced,
	parentToken,
	hasDescendants,
	isTask,
	runAction,
}: {
	readonly nodeKey: string;
	readonly path: string;
	readonly sourced: boolean;
	readonly parentToken: ViewToken;
	/** The grid has child grids (a task's internals, or a subtree's
	 * children) — delete must be recursive. */
	readonly hasDescendants: boolean;
	readonly isTask: boolean;
	readonly runAction: RunAction;
}): ReactNode {
	const [expanded, setExpanded] = useState<"revoke" | "delete" | null>(null);
	// The parent grant is unsliced; the subtree ViewSel binds nothing
	// (grid addressing is by key under the parent).
	const parentSel = viewSel(parentToken);
	const fire = (label: string, steps: Parameters<RunAction>[1]) => {
		setExpanded(null);
		runAction(label, steps);
	};
	return (
		<div>
			<div style={headerRowStyle}>
				{sourced ? (
					<button
						type="button"
						style={smallButtonStyle}
						onClick={() => setExpanded(expanded === "revoke" ? null : "revoke")}
					>
						Revoke grid sourcing…
					</button>
				) : (
					<button
						type="button"
						style={smallButtonStyle}
						title="declare this grid user-sourced at its current stored config (adoption)"
						onClick={() =>
							fire(`Source grid ${path}`, [{ kind: "source_grid", sel: parentSel, key: nodeKey }])
						}
					>
						Source grid (adopt config)
					</button>
				)}
				<button
					type="button"
					style={smallButtonStyle}
					onClick={() => setExpanded(expanded === "delete" ? null : "delete")}
				>
					Delete grid…
				</button>
			</div>
			{expanded === "revoke" && (
				<div style={confirmBoxStyle}>
					<span style={mutedInlineStyle}>
						remove the grid's sourcing record (values stay; verdicts re-derive)? refuses while
						cell-level intent exists — force also sweeps this grid's cell sourcing records and
						targets
					</span>
					<button
						type="button"
						style={smallButtonStyle}
						onClick={() =>
							fire(`Revoke grid sourcing ${path}`, [
								{ kind: "revoke_grid_sourcing", sel: parentSel, key: nodeKey, force: false },
							])
						}
					>
						revoke
					</button>
					<button
						type="button"
						style={smallButtonStyle}
						onClick={() =>
							fire(`Revoke grid sourcing (force) ${path}`, [
								{ kind: "revoke_grid_sourcing", sel: parentSel, key: nodeKey, force: true },
							])
						}
					>
						revoke + sweep cell intent (force)
					</button>
					<button type="button" style={smallButtonStyle} onClick={() => setExpanded(null)}>
						cancel
					</button>
				</div>
			)}
			{expanded === "delete" && (
				<div style={confirmBoxStyle}>
					<span style={mutedInlineStyle}>
						{hasDescendants
							? isTask
								? "delete this task and all of its internal subgrids"
								: "delete this grid and all of its descendants"
							: "delete this grid and its contents"}{" "}
						(observers of its cells go stale, and intent records pointing here go dangling)?
					</span>
					<button
						type="button"
						style={smallButtonStyle}
						onClick={() =>
							fire(`Delete grid ${path}`, [
								{
									kind: "delete_grid",
									token: parentToken,
									key: nodeKey,
									recursive: hasDescendants,
								},
							])
						}
					>
						{hasDescendants ? "delete recursively" : "delete grid"}
					</button>
					<button type="button" style={smallButtonStyle} onClick={() => setExpanded(null)}>
						cancel
					</button>
				</div>
			)}
		</div>
	);
}

function Verdict({
	label,
	value,
	reason,
	color,
}: {
	readonly label: string;
	readonly value: string | null;
	readonly reason: string | null;
	readonly color: string;
}): ReactNode {
	return (
		<span style={{ ...mutedInlineStyle, color: value === null ? undefined : color }}>
			{label}: {value ?? "—"}
			{reason ? ` (${reason})` : ""}
		</span>
	);
}

function CellsTable({
	node,
	mirror,
	cellEntry,
	targetsByCoords,
	gridSourcing,
	selectedCell,
	onSelectCell,
	drawer,
	runAction,
	retryCell,
	frameLoads,
}: {
	readonly node: GridTreeNode;
	readonly mirror: CellsMirror;
	readonly cellEntry: WatchSlots["cell"];
	readonly targetsByCoords: ReadonlyMap<string, TargetInfo>;
	readonly gridSourcing: readonly SourcingInfo[];
	readonly selectedCell: string | null;
	readonly onSelectCell: (coordsText: string | null) => void;
	readonly drawer: DrawerProps;
	readonly runAction: RunAction;
	readonly retryCell: () => void;
	readonly frameLoads: FxtrSessionAtom<FrameLoads>;
}): ReactNode {
	const [filter, setFilter] = useState("");
	const [sort, setSort] = useState<CellSort | null>(null);

	const cells = useMemo(() => Object.values(mirror.cells), [mirror.cells]);
	const targetCoords = useMemo(() => [...targetsByCoords.keys()], [targetsByCoords]);
	const rows = useMemo(
		() => deriveCellRows(cells, targetCoords, filter, sort),
		[cells, targetCoords, filter, sort],
	);
	// The meta is non-null once the snapshot folded (the engine's first
	// batch always carries it); guard anyway.
	const meta = mirror.meta;
	if (meta === null) return <div style={mutedStyle}>subscribing…</div>;

	const clickSort = (key: CellSortKey) => {
		setSort((prev) =>
			prev !== null && sameSortKey(prev.key, key)
				? prev.dir === 1
					? { key, dir: -1 }
					: null
				: { key, dir: 1 },
		);
	};

	const selected = rows.find((r) => r.coordsText === selectedCell) ?? null;
	// Total showable rows (populated + phantom targets), for the count and
	// the empty state — phantoms mean a grid with only unrun targets is
	// not "empty".
	const totalRows = useMemo(
		() => deriveCellRows(cells, targetCoords, "", null).length,
		[cells, targetCoords],
	);
	return (
		<div>
			<div style={tableBarStyle}>
				<input
					style={filterStyle}
					placeholder="filter cells…"
					value={filter}
					onChange={(e) => setFilter(e.target.value)}
				/>
				<span style={mutedInlineStyle}>
					{rows.length} / {totalRows} cells
				</span>
				{mirror.recomputing && <span style={mutedInlineStyle}>updating…</span>}
			</div>
			{totalRows === 0 ? (
				<div style={mutedStyle}>no cells or targets</div>
			) : (
				<table style={tableStyle}>
					<thead>
						<tr>
							{meta.grant.axes.map((a, i) => (
								<th
									key={a.name}
									style={thStyle}
									onClick={() => clickSort({ kind: "coord", i })}
									onKeyDown={(e) => {
										if (e.key === "Enter") clickSort({ kind: "coord", i });
									}}
									title="sort"
								>
									{a.name}
									<SortMark
										active={sort !== null && sort.key.kind === "coord" && sort.key.i === i}
										dir={sort?.dir}
									/>
								</th>
							))}
							<th
								style={thStyle}
								onClick={() => clickSort({ kind: "status" })}
								onKeyDown={(e) => {
									if (e.key === "Enter") clickSort({ kind: "status" });
								}}
								title="sort"
							>
								status
								<SortMark active={sort !== null && sort.key.kind === "status"} dir={sort?.dir} />
							</th>
							<th style={thStyle} title="targeted (green) / wanted by a reader (grey)">
								want
							</th>
							<th style={thStyle}>reason</th>
						</tr>
					</thead>
					<tbody>
						{rows.map(({ cell, coordsText, populated }) => (
							<tr
								key={coordsText}
								style={{
									...trStyle,
									// Phantom (targeted, not-yet-run) rows read as faint —
									// the hollow green dot marks them; the row is value-free.
									...(populated ? {} : phantomRowStyle),
									...(coordsText === selectedCell ? trSelectedStyle : {}),
								}}
								onClick={() => onSelectCell(coordsText === selectedCell ? null : coordsText)}
								onKeyDown={(e) => {
									if (e.key === "Enter") {
										onSelectCell(coordsText === selectedCell ? null : coordsText);
									}
								}}
							>
								{meta.grant.axes.map((a, i) => (
									<td key={a.name} style={tdStyle}>
										<code style={coordStyle}>{String(cell.coords[i] ?? "")}</code>
									</td>
								))}
								<td style={tdStyle}>
									{populated ? (
										<StatusCell cell={cell} />
									) : (
										<span style={mutedInlineStyle}>not run</span>
									)}
								</td>
								<td style={tdStyle}>
									<DemandMark
										demand={cellDemand(cell, targetsByCoords.has(coordsText), populated)}
									/>
								</td>
								<td style={{ ...tdStyle, ...reasonStyle }} title={cell.reason ?? undefined}>
									{cell.reason ?? ""}
								</td>
							</tr>
						))}
					</tbody>
				</table>
			)}
			{selected !== null && node.summary !== null && (
				<CellDrawer
					meta={meta}
					kind={node.summary.kind}
					internals={node.internalsOf !== null}
					path={node.path}
					cell={selected.cell}
					coordsText={selected.coordsText}
					targeted={targetsByCoords.has(selected.coordsText)}
					targetRecord={targetsByCoords.get(selected.coordsText) ?? null}
					gridSourced={mirror.meta !== null && mirror.meta.sourcing !== null}
					sourcing={gridSourcing}
					cellEntry={cellEntry}
					hasInternals={node.children.some((c) => c.internalsOf === node.path)}
					internalsRevealed={drawer.internalsRevealed}
					onBrowseInternals={drawer.onToggleInternals}
					onClose={() => onSelectCell(null)}
					retryCell={retryCell}
					runAction={runAction}
					frameLoads={frameLoads}
				/>
			)}
		</div>
	);
}

function SortMark({
	active,
	dir,
}: {
	readonly active: boolean;
	readonly dir: 1 | -1 | undefined;
}): ReactNode {
	if (!active) return null;
	return <span style={{ opacity: 0.6 }}>{dir === -1 ? " ↓" : " ↑"}</span>;
}

/** The folded status column: exec status + abnormal currency/grounding
 * only (`cellBadges`); "—" when there's nothing notable to show. */
function StatusCell({ cell }: { readonly cell: CellInfo }): ReactNode {
	const badges = cellBadges(cell);
	if (badges.length === 0) return <span style={{ opacity: 0.5 }}>—</span>;
	return (
		<span style={statusCellStyle}>
			{badges.map((b) => (
				<span key={b.kind} style={{ color: badgeColor(b) }}>
					{b.text}
				</span>
			))}
		</span>
	);
}

function badgeColor(badge: CellBadge): string | undefined {
	switch (badge.kind) {
		case "status":
			return statusColor(badge.text as ExecStatus);
		case "currency":
			return currencyColor(badge.text as Currency);
		case "grounding":
			return groundingColor(badge.text as GroundingVerdict);
	}
}

/** The combined want/target dot (color = targeted vs wanted; fill =
 * populated vs not — the tree pane's language): targeted+run = filled
 * green, targeted+not-run = hollow green, wanted-by-a-reader = filled
 * grey, nothing otherwise. */
function DemandMark({ demand }: { readonly demand: CellDemand }): ReactNode {
	switch (demand) {
		case "targeted-run":
			return (
				<span style={{ color: "#3a8a4a" }} title="targeted (populated)">
					●
				</span>
			);
		case "targeted-pending":
			return (
				<span style={{ color: "#3a8a4a" }} title="targeted, not yet run">
					◌
				</span>
			);
		case "wanted":
			return (
				<span style={{ opacity: 0.55 }} title="wanted by a reader (not directly targeted)">
					●
				</span>
			);
		default:
			return null;
	}
}

function statusColor(status: ExecStatus | null): string | undefined {
	switch (status) {
		case "running":
			return "#2266cc";
		case "errored":
			return "#c04040";
		case "interrupted":
			return "#b06010";
		case "complete":
			return "#3a8a4a";
		default:
			return undefined;
	}
}

function currencyColor(currency: Currency | null): string | undefined {
	switch (currency) {
		case "fresh":
			return "#3a8a4a";
		case "stale":
		case "unsettled":
			return "#907000";
		default:
			return undefined;
	}
}

function groundingColor(verdict: GroundingVerdict | null): string {
	switch (verdict) {
		case "orphaned":
			return "#c04040";
		case "unanchored":
			return "#c09a3a";
		default:
			return "#3a8a4a";
	}
}

const headerStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: 4,
	paddingBottom: 8,
	borderBottom: "1px solid var(--fxtr-border, #ccd5df)",
	marginBottom: 8,
};
const headerRowStyle: CSSProperties = {
	display: "flex",
	gap: 10,
	alignItems: "baseline",
	flexWrap: "wrap",
};
const pathStyle: CSSProperties = { fontSize: 12, opacity: 0.75 };
const kindTagStyle: CSSProperties = {
	fontSize: 10,
	padding: "1px 6px",
	borderRadius: 3,
	background: "var(--fxtr-bg-raised, #eef2f6)",
	opacity: 0.8,
};
const axesStyle: CSSProperties = { fontSize: 11, opacity: 0.55 };
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12, marginTop: 8 };
const mutedInlineStyle: CSSProperties = { opacity: 0.65, fontSize: 11 };
const errorStyle: CSSProperties = { color: "#c04040", fontSize: 12, marginTop: 8 };
const tombstoneStyle: CSSProperties = {
	marginTop: 8,
	padding: "6px 10px",
	border: "1px dashed var(--fxtr-border, #aab8c6)",
	borderRadius: 6,
	fontSize: 12,
	fontStyle: "italic",
	opacity: 0.75,
};
const buttonStyle: CSSProperties = {
	marginLeft: 6,
	padding: "1px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	background: "var(--fxtr-bg-raised, #f4f7fa)",
	cursor: "pointer",
	fontSize: 11,
};
const smallButtonStyle: CSSProperties = {
	padding: "1px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	background: "var(--fxtr-bg-raised, #f4f7fa)",
	cursor: "pointer",
	fontSize: 11,
};
const confirmBoxStyle: CSSProperties = {
	display: "flex",
	gap: 8,
	alignItems: "baseline",
	flexWrap: "wrap",
	marginTop: 4,
	padding: "4px 8px",
	border: "1px solid var(--fxtr-border, #c9a86a)",
	borderRadius: 6,
	fontSize: 11,
};
const tableBarStyle: CSSProperties = {
	display: "flex",
	gap: 8,
	alignItems: "baseline",
	margin: "4px 0 6px 0",
};
const filterStyle: CSSProperties = {
	fontSize: 12,
	padding: "2px 6px",
	borderRadius: 4,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	background: "none",
	color: "inherit",
	width: 180,
};
const tableStyle: CSSProperties = { borderSpacing: 0, fontSize: 12, width: "100%" };
const thStyle: CSSProperties = {
	textAlign: "left",
	fontWeight: 600,
	fontSize: 11,
	opacity: 0.7,
	padding: "2px 10px 4px 4px",
	borderBottom: "1px solid var(--fxtr-border, #ccd5df)",
	cursor: "pointer",
	whiteSpace: "nowrap",
};
const trStyle: CSSProperties = { cursor: "pointer" };
const trSelectedStyle: CSSProperties = { background: "var(--fxtr-bg-selected, #dbe7f5)" };
const tdStyle: CSSProperties = {
	padding: "2px 10px 2px 4px",
	borderBottom: "1px solid var(--fxtr-border-faint, #e8edf2)",
	whiteSpace: "nowrap",
};
const coordStyle: CSSProperties = { fontSize: 11 };
const statusCellStyle: CSSProperties = { display: "inline-flex", gap: 6, flexWrap: "wrap" };
const phantomRowStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic" };
const reasonStyle: CSSProperties = {
	maxWidth: 320,
	overflow: "hidden",
	textOverflow: "ellipsis",
	opacity: 0.7,
	whiteSpace: "nowrap",
};
