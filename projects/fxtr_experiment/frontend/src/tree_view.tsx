/**
 * The key-path tree (left pane): one row per grid, disclosure
 * triangles, kind glyphs, rollup badges (populated / running / errored
 * / interrupted / stale / unsettled), grounding tint, target dots, and
 * the symbol legend behind the "?" toggle. Internals subtrees (grids
 * under a task grid) are hidden behind a per-task reveal toggle. Pure
 * presentation over `buildGridTree`'s output; selection + toggles
 * write the panel's ui state.
 */

import type { CSSProperties, ReactNode } from "react";

import { statusBadges } from "./derive/badges.js";
import type { GridTreeNode, RollupCounts } from "./derive/tree.js";
import type { GridKind } from "./wire/types.js";

const KIND_GLYPH: Record<GridKind, string> = {
	data: "D",
	dataframe: "F",
	handle: "H",
	task: "T",
	checkpoint: "C",
	subtree: "▸",
};

/** What "populated" counts, per kind (rollups also include
 * descendants' counts, so subtree rows aggregate whatever is below). */
const POPULATED_NOUN: Record<GridKind, string> = {
	data: "cells",
	dataframe: "cells",
	handle: "cells",
	task: "generations",
	checkpoint: "entries",
	subtree: "cells (in descendants)",
};

export function TreeView({
	roots,
	selectedPath,
	collapsed,
	revealedInternals,
	legendOpen,
	onSelect,
	onToggleCollapse,
	onToggleInternals,
	onToggleLegend,
}: {
	readonly roots: readonly GridTreeNode[];
	readonly selectedPath: string | null;
	readonly collapsed: readonly string[];
	readonly revealedInternals: readonly string[];
	readonly legendOpen: boolean;
	readonly onSelect: (path: string) => void;
	readonly onToggleCollapse: (path: string) => void;
	readonly onToggleInternals: (taskPath: string) => void;
	readonly onToggleLegend: () => void;
}): ReactNode {
	return (
		<div>
			<div style={legendBarStyle}>
				<button
					type="button"
					style={legendToggleStyle}
					title="show/hide the symbol legend"
					onClick={onToggleLegend}
				>
					{legendOpen ? "hide key" : "?"}
				</button>
			</div>
			{legendOpen && <Legend />}
			{roots.length === 0 ? (
				<div style={mutedStyle}>no grids yet — drive the demo to create some</div>
			) : (
				roots.map((n) => (
					<TreeNodeRow
						key={n.path}
						node={n}
						depth={0}
						selectedPath={selectedPath}
						collapsed={collapsed}
						revealedInternals={revealedInternals}
						onSelect={onSelect}
						onToggleCollapse={onToggleCollapse}
						onToggleInternals={onToggleInternals}
					/>
				))
			)}
		</div>
	);
}

function Legend(): ReactNode {
	return (
		<div style={legendStyle}>
			<div style={legendRowStyle}>
				<b>kinds</b> D data · F dataframe · H handle · T task · C checkpoint · ▸ subtree
			</div>
			<div style={legendRowStyle}>
				<b>counts</b> bare n = populated (T: generations · D/F/H: cells · C: entries) ·{" "}
				<span style={{ color: "#2266cc" }}>▶ running</span> ·{" "}
				<span style={{ color: "#c04040" }}>✕ errored</span> ·{" "}
				<span style={{ color: "#b06010" }}>‼ interrupted</span> ·{" "}
				<span style={{ color: "#907000" }}>~ stale · ? unsettled</span>
			</div>
			<div style={legendRowStyle}>
				<b>targets</b> <span style={{ color: "#3a8a4a" }}>●n targeted + populated</span> ·{" "}
				<span style={{ color: "#3a8a4a" }}>◌n targeted, unpopulated</span> ·{" "}
				<span style={{ color: "#c04040" }}>red = dangling target record</span>
			</div>
			<div style={legendRowStyle}>
				<b>row tint</b> <span style={{ color: "#c09a3a" }}>unanchored</span> ·{" "}
				<span style={{ color: "#c04040" }}>orphaned</span> (grounding; counts include descendants)
			</div>
		</div>
	);
}

function TreeNodeRow({
	node,
	depth,
	selectedPath,
	collapsed,
	revealedInternals,
	onSelect,
	onToggleCollapse,
	onToggleInternals,
}: {
	readonly node: GridTreeNode;
	readonly depth: number;
	readonly selectedPath: string | null;
	readonly collapsed: readonly string[];
	readonly revealedInternals: readonly string[];
	readonly onSelect: (path: string) => void;
	readonly onToggleCollapse: (path: string) => void;
	readonly onToggleInternals: (taskPath: string) => void;
}): ReactNode {
	// Children that are this task grid's internals are hidden unless
	// revealed (a non-task node never has internals children of its own
	// — they'd belong to a deeper task).
	const isTask = node.summary?.kind === "task";
	const internalsHidden = isTask && !revealedInternals.includes(node.path);
	const visibleChildren = internalsHidden
		? node.children.filter((c) => c.internalsOf !== node.path)
		: node.children;
	const hasInternals = isTask && node.children.some((c) => c.internalsOf === node.path);
	const isCollapsed = collapsed.includes(node.path);
	const grounding = node.summary?.grounding ?? null;
	const tint =
		grounding === "orphaned" ? "#c04040" : grounding === "unanchored" ? "#c09a3a" : "transparent";

	return (
		<div>
			<div
				style={{
					...rowStyle,
					paddingLeft: 6 + depth * 14,
					borderLeft: `3px solid ${tint}`,
					...(node.path === selectedPath ? selectedStyle : {}),
				}}
				onClick={() => onSelect(node.path)}
				onKeyDown={(e) => {
					if (e.key === "Enter") onSelect(node.path);
				}}
				role="button"
				tabIndex={0}
			>
				{visibleChildren.length > 0 ? (
					<button
						type="button"
						style={twistyStyle}
						onClick={(e) => {
							e.stopPropagation();
							onToggleCollapse(node.path);
						}}
					>
						{isCollapsed ? "▸" : "▾"}
					</button>
				) : (
					<span style={twistySpacerStyle} />
				)}
				<span style={glyphStyle}>{node.summary ? KIND_GLYPH[node.summary.kind] : "·"}</span>
				<span style={keyStyle}>{node.key}</span>
				{node.summary && node.summary.axes.length > 0 && (
					<span style={axesStyle}>[{node.summary.axes.map((a) => a.name).join(", ")}]</span>
				)}
				<Badges agg={node.agg} kind={node.summary?.kind ?? null} />
				{hasInternals && (
					<button
						type="button"
						style={internalsToggleStyle}
						title="show/hide this task's internals grids"
						onClick={(e) => {
							e.stopPropagation();
							onToggleInternals(node.path);
						}}
					>
						{internalsHidden ? "internals…" : "hide internals"}
					</button>
				)}
			</div>
			{!isCollapsed &&
				visibleChildren.map((c) => (
					<TreeNodeRow
						key={c.path}
						node={c}
						depth={depth + 1}
						selectedPath={selectedPath}
						collapsed={collapsed}
						revealedInternals={revealedInternals}
						onSelect={onSelect}
						onToggleCollapse={onToggleCollapse}
						onToggleInternals={onToggleInternals}
					/>
				))}
		</div>
	);
}

function Badges({
	agg,
	kind,
}: {
	readonly agg: RollupCounts;
	readonly kind: GridKind | null;
}): ReactNode {
	const targetsTotal = agg.targets_attached + agg.targets_dangling;
	return (
		<span style={badgesStyle}>
			{agg.populated > 0 && (
				<span
					style={badgeStyle}
					title={`${agg.populated} populated ${kind === null ? "cells (in descendants)" : POPULATED_NOUN[kind]}`}
				>
					{agg.populated}
				</span>
			)}
			{statusBadges(agg).map((b) => (
				<span
					key={b.label}
					style={{ ...badgeStyle, color: b.color }}
					title={`${b.count} ${b.label}`}
				>
					{b.sym}
					{b.count}
				</span>
			))}
			{targetsTotal > 0 && (
				<span
					style={{
						...badgeStyle,
						color: agg.targets_dangling > 0 ? "#c04040" : "#3a8a4a",
					}}
					title={[
						`${agg.targets_attached} attached target${agg.targets_attached === 1 ? "" : "s"}`,
						agg.targets_dangling > 0 ? `${agg.targets_dangling} dangling` : null,
						agg.populated === 0 ? "not yet populated" : null,
					]
						.filter(Boolean)
						.join(" · ")}
				>
					{agg.populated > 0 ? "●" : "◌"}
					{targetsTotal}
				</span>
			)}
		</span>
	);
}

const rowStyle: CSSProperties = {
	display: "flex",
	alignItems: "baseline",
	gap: 5,
	padding: "2px 4px",
	cursor: "pointer",
	borderRadius: 4,
	whiteSpace: "nowrap",
};
const selectedStyle: CSSProperties = { background: "var(--fxtr-bg-selected, #dbe7f5)" };
const twistyStyle: CSSProperties = {
	border: "none",
	background: "none",
	cursor: "pointer",
	padding: 0,
	width: 14,
	fontSize: 10,
};
const twistySpacerStyle: CSSProperties = { display: "inline-block", width: 14 };
const glyphStyle: CSSProperties = {
	fontSize: 10,
	fontWeight: 700,
	opacity: 0.55,
	width: 12,
	textAlign: "center",
};
const keyStyle: CSSProperties = { fontWeight: 500 };
const axesStyle: CSSProperties = { fontSize: 11, opacity: 0.55 };
const badgesStyle: CSSProperties = { display: "inline-flex", gap: 5, marginLeft: 4 };
const badgeStyle: CSSProperties = { fontSize: 11, opacity: 0.9 };
const internalsToggleStyle: CSSProperties = {
	marginLeft: 6,
	fontSize: 10,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 4,
	background: "none",
	cursor: "pointer",
	opacity: 0.8,
};
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", padding: 8, fontSize: 12 };
const legendBarStyle: CSSProperties = {
	display: "flex",
	justifyContent: "flex-end",
	padding: "0 6px 2px 0",
};
const legendToggleStyle: CSSProperties = {
	fontSize: 10,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 8,
	background: "none",
	cursor: "pointer",
	opacity: 0.7,
	padding: "0 6px",
};
const legendStyle: CSSProperties = {
	margin: "0 6px 8px 6px",
	padding: 6,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	fontSize: 10.5,
	display: "flex",
	flexDirection: "column",
	gap: 3,
	opacity: 0.9,
};
const legendRowStyle: CSSProperties = { display: "block" };
