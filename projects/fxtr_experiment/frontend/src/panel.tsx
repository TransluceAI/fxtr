/**
 * The dashboard FullPanel: workflow header (lint badge + the
 * workflow-level lists) over a filesystem-metaphor layout — the
 * key-path tree on the left, the grid detail pane on the right
 * (`notes/monitoring_ui.md`). The read path is three watch mirrors
 * (`notes/event_system.md`): the always-on workflow watch behind the
 * header + tree, the selected grid's cells watch behind the detail
 * pane, and the selected cell's watch behind the drawer — each an
 * inbox + `subscribe_to` entry in the versioned `ownStorage` slot,
 * kept live by one generic driver each (`watch_driver.ts`), folded by
 * `derive/folds.ts`. Navigation changes the specs; the drivers drop /
 * reallocate entries accordingly (drop = unsubscribe). The header's
 * "resubscribe" drops all three (the manual escape hatch — fresh
 * snapshots + fresh grants).
 */

import type { ServiceRefStub } from "@fxtr-ui/core";
import { useSubAtom } from "@fxtr-ui/react";
import type { DispatchedComponent } from "@fxtr-ui/react-dispatch";
import { atomWithDefault } from "@fxtr-ui/state-util";
import type { FullPanelProps } from "@fxtr-ui/workstation-dispatch";
import { atom, useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, useMemo } from "react";

import { ActionLog, type RunAction, allocateStep } from "./actions.js";
import {
	CELLS_MIRROR_EMPTY,
	CELL_MIRROR_EMPTY,
	GRAPH_MIRROR_EMPTY,
	WORKFLOW_MIRROR_EMPTY,
	foldCell,
	foldCells,
	foldGraph,
	foldWorkflow,
} from "./derive/folds.js";
import { buildGridTree } from "./derive/tree.js";
import { GridDetail } from "./grid_view.js";
import { HeaderView } from "./header_view.js";
import {
	type ActionStep,
	type CellSpec,
	type CellsSpec,
	type GraphSpec,
	type HeaderList,
	PANEL_INITIAL,
	type PanelState,
	type WorkflowSpec,
	isPanelState,
	toggle,
} from "./panel_state.js";
import { PlateView } from "./plate_view.js";
import { TreeView } from "./tree_view.js";
import { makeWatchDriver } from "./watch_driver.js";
import type { ExperimentWorkflowProto } from "./wire/proto.js";

// biome-ignore lint/suspicious/noExplicitAny: FxtrSessionAtom's ownStorage narrowing cast (the standard renderer pattern)
type AnyAtom = any;

/** Grid kinds the detail pane subscribes a cells watch for (checkpoint
 * grids are listable but never attachable — the pane explains instead;
 * implicit prefixes have no grid at all). */
const WATCHABLE_KINDS = new Set(["data", "dataframe", "handle", "task", "subtree"]);

/** Cell kinds with drawer content (`inspect_cell`-able; a subtree cell
 * is a namespace, not a value). */
const INSPECTABLE_KINDS = new Set(["data", "dataframe", "handle", "task"]);

export const ExperimentDashboardPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const capability = stub as ServiceRefStub<ExperimentWorkflowProto>;
	const panelAtom = useMemo(
		() => atomWithDefault<PanelState>(ownStorage as AnyAtom, PANEL_INITIAL, isPanelState),
		[ownStorage],
	);
	const watchesAtom = useSubAtom(panelAtom, "watches");
	const workflowEntryAtom = useSubAtom(watchesAtom, "workflow");
	const cellsEntryAtom = useSubAtom(watchesAtom, "cells");
	const cellEntryAtom = useSubAtom(watchesAtom, "cell");
	const graphEntryAtom = useSubAtom(watchesAtom, "graph");
	const actionsAtom = useSubAtom(panelAtom, "actions");
	const frameLoadsAtom = useSubAtom(panelAtom, "frameLoads");
	const uiAtom = useSubAtom(panelAtom, "ui");

	// The watch specs: what the current navigation wants watched. The
	// grid/cell specs derive from the workflow mirror too (the grid's
	// KIND gates whether a watch makes sense), so a deleted grid's spec
	// collapses to null and the driver drops the watch.
	const workflowSpecAtom = useMemo(() => atom((): WorkflowSpec | null => true), []);
	const cellsSpecAtom = useMemo(
		() =>
			atom((get): CellsSpec | null => {
				const path = get(uiAtom).selectedPath;
				if (path === null) return null;
				const row = get(workflowEntryAtom)?.mirror.rows[path];
				return row !== undefined && WATCHABLE_KINDS.has(row.kind) ? { path } : null;
			}),
		[uiAtom, workflowEntryAtom],
	);
	const cellSpecAtom = useMemo(
		() =>
			atom((get): CellSpec | null => {
				const ui = get(uiAtom);
				if (ui.selectedPath === null || ui.selectedCell === null) return null;
				const row = get(workflowEntryAtom)?.mirror.rows[ui.selectedPath];
				return row !== undefined && INSPECTABLE_KINDS.has(row.kind)
					? { path: ui.selectedPath, coords: ui.selectedCell }
					: null;
			}),
		[uiAtom, workflowEntryAtom],
	);
	// The graph watch is live only while the plate tab is visible (its
	// spec collapses to null off-tab, so the driver drops the subscription).
	const graphSpecAtom = useMemo(
		() => atom((get): GraphSpec | null => (get(uiAtom).tab === "plates" ? true : null)),
		[uiAtom],
	);

	// Mounting the driver atoms keeps the three subscriptions live; the
	// values are void (the effects write the watch entries).
	useAtomValue(
		useMemo(
			() =>
				makeWatchDriver({
					entryAtom: workflowEntryAtom,
					specAtom: workflowSpecAtom,
					capability,
					method: "watch_workflow",
					specKwargs: () => ({}),
					sameSpec: () => true,
					emptyMirror: WORKFLOW_MIRROR_EMPTY,
					fold: foldWorkflow,
				}),
			[workflowEntryAtom, workflowSpecAtom, capability],
		),
	);
	useAtomValue(
		useMemo(
			() =>
				makeWatchDriver({
					entryAtom: cellsEntryAtom,
					specAtom: cellsSpecAtom,
					capability,
					method: "watch_cells",
					specKwargs: (spec) => ({ path: spec.path }),
					sameSpec: (a, b) => a.path === b.path,
					emptyMirror: CELLS_MIRROR_EMPTY,
					fold: foldCells,
				}),
			[cellsEntryAtom, cellsSpecAtom, capability],
		),
	);
	useAtomValue(
		useMemo(
			() =>
				makeWatchDriver({
					entryAtom: cellEntryAtom,
					specAtom: cellSpecAtom,
					capability,
					method: "watch_cell",
					specKwargs: (spec) => ({ path: spec.path, coords: spec.coords }),
					sameSpec: (a, b) => a.path === b.path && a.coords === b.coords,
					emptyMirror: CELL_MIRROR_EMPTY,
					fold: foldCell,
				}),
			[cellEntryAtom, cellSpecAtom, capability],
		),
	);
	useAtomValue(
		useMemo(
			() =>
				makeWatchDriver({
					entryAtom: graphEntryAtom,
					specAtom: graphSpecAtom,
					capability,
					method: "watch_graph",
					specKwargs: () => ({}),
					sameSpec: () => true,
					emptyMirror: GRAPH_MIRROR_EMPTY,
					fold: foldGraph,
				}),
			[graphEntryAtom, graphSpecAtom, capability],
		),
	);

	const workflowEntry = useAtomValue(workflowEntryAtom);
	const cellsEntry = useAtomValue(cellsEntryAtom);
	const cellEntry = useAtomValue(cellEntryAtom);
	const graphEntry = useAtomValue(graphEntryAtom);
	const workflow = workflowEntry?.mirror ?? WORKFLOW_MIRROR_EMPTY;
	const graph = graphEntry?.mirror ?? GRAPH_MIRROR_EMPTY;

	const ui = useAtomValue(uiAtom);
	const setUi = useSetAtom(uiAtom);
	const setWatches = useSetAtom(watchesAtom);
	const setWorkflowEntry = useSetAtom(workflowEntryAtom);
	const setCellsEntry = useSetAtom(cellsEntryAtom);
	const setCellEntry = useSetAtom(cellEntryAtom);
	const setActions = useSetAtom(actionsAtom);

	// One mutation gesture: allocate the first step now; the log's rows
	// advance the rest (`actions.tsx`). The log keeps the last 8.
	const runAction: RunAction = (label, steps) => {
		if (steps.length === 0) return;
		setActions((prev, ctx) => [
			...prev.slice(-7),
			{
				id: prev.length === 0 ? 1 : (prev[prev.length - 1]?.id ?? 0) + 1,
				label,
				current: allocateStep(ctx, capability, steps[0] as ActionStep),
				pending: steps.slice(1),
			},
		]);
	};

	// The manual escape hatch: drop all three entries — the drivers
	// resubscribe with fresh inboxes for fresh snapshots (and fresh
	// grants in the cells meta).
	const resubscribe = () => setWatches({ workflow: null, cells: null, cell: null, graph: null });

	const tree = useMemo(
		() => (workflow.snapshotted ? buildGridTree(Object.values(workflow.rows)) : null),
		[workflow.snapshotted, workflow.rows],
	);
	const selected =
		tree !== null && ui.selectedPath !== null ? (tree.byPath.get(ui.selectedPath) ?? null) : null;

	// The cell selection navigation lands on: a grid with no axes has
	// exactly one possible cell (the empty coordinate), so select it
	// outright when the grid can show a drawer; everywhere else start
	// unselected.
	const initialCell = (path: string): string | null => {
		const row = workflow.rows[path];
		return row !== undefined && INSPECTABLE_KINDS.has(row.kind) && row.axes.length === 0
			? "[]"
			: null;
	};

	return (
		<div style={panelStyle}>
			<HeaderView
				workflow={workflow}
				openList={ui.openList}
				onToggleList={(list: HeaderList) =>
					setUi((u) => ({ ...u, openList: u.openList === list ? null : list }))
				}
				onResubscribe={resubscribe}
				runAction={runAction}
				live={{
					phase:
						workflowEntry === null
							? "connecting"
							: workflowEntry.phase === "errored"
								? "errored"
								: workflowEntry.phase === "running"
									? "live"
									: "connecting",
					message: workflowEntry?.phase === "errored" ? workflowEntry.message : null,
					recomputing: workflow.recomputing,
					// Retry drops the parked entry; the driver resubscribes
					// with a fresh inbox on its next run.
					onRetry: () => setWorkflowEntry(null),
				}}
			/>
			<div style={tabBarStyle}>
				{(["tree", "plates"] as const).map((t) => (
					<button
						type="button"
						key={t}
						onClick={() => setUi((u) => ({ ...u, tab: t }))}
						style={ui.tab === t ? tabActiveStyle : tabStyle}
					>
						{t === "tree" ? "Tree" : "Plates"}
					</button>
				))}
			</div>
			{ui.tab === "plates" ? (
				<div style={bodyStyle}>
					<PlateView
						workflow={workflow}
						graph={graph}
						onSelect={(path) =>
							setUi((u) => ({
								...u,
								tab: "tree",
								selectedPath: path,
								selectedCell: initialCell(path),
							}))
						}
					/>
				</div>
			) : (
				<div style={bodyStyle}>
					<div style={treePaneStyle}>
						{workflowEntry?.phase === "errored" ? (
							<div style={errorStyle}>workflow watch failed: {workflowEntry.message}</div>
						) : tree === null ? (
							<div style={mutedStyle}>loading…</div>
						) : (
							<TreeView
								roots={tree.roots}
								selectedPath={ui.selectedPath}
								collapsed={ui.collapsed}
								revealedInternals={ui.revealedInternals}
								legendOpen={ui.legendOpen}
								onToggleLegend={() => setUi((u) => ({ ...u, legendOpen: !u.legendOpen }))}
								onSelect={(path) =>
									setUi((u) => ({ ...u, selectedPath: path, selectedCell: initialCell(path) }))
								}
								onToggleCollapse={(path) =>
									setUi((u) => ({ ...u, collapsed: toggle(u.collapsed, path) }))
								}
								onToggleInternals={(taskPath) =>
									setUi((u) => ({
										...u,
										revealedInternals: toggle(u.revealedInternals, taskPath),
									}))
								}
							/>
						)}
					</div>
					<div style={detailPaneStyle}>
						{selected === null ? (
							<div style={mutedStyle}>select a grid in the tree</div>
						) : (
							<GridDetail
								key={selected.path}
								node={selected}
								workflow={workflow}
								cellsEntry={cellsEntry}
								cellEntry={cellEntry}
								selectedCell={ui.selectedCell}
								onSelectCell={(coordsText) => setUi((u) => ({ ...u, selectedCell: coordsText }))}
								runAction={runAction}
								retryCells={() => setCellsEntry(null)}
								retryCell={() => setCellEntry(null)}
								frameLoads={frameLoadsAtom}
								drawer={{
									internalsRevealed: ui.revealedInternals.includes(selected.path),
									// The drawer's "browse internals": reveal this task's
									// internals rows in the tree (and make sure the node
									// is expanded so they are actually visible).
									onToggleInternals: () =>
										setUi((u) => ({
											...u,
											revealedInternals: toggle(u.revealedInternals, selected.path),
											collapsed: u.collapsed.filter((p) => p !== selected.path),
										})),
								}}
							/>
						)}
					</div>
				</div>
			)}
			<ActionLog capability={capability} actionsAtom={actionsAtom} />
		</div>
	);
};

const panelStyle: CSSProperties = {
	padding: 12,
	height: "100%",
	display: "flex",
	flexDirection: "column",
	fontSize: 13,
	color: "var(--fxtr-fg, #1a2b3c)",
};
const bodyStyle: CSSProperties = { display: "flex", flex: 1, minHeight: 0 };
const tabBarStyle: CSSProperties = {
	display: "flex",
	gap: 4,
	padding: "6px 0 4px",
	borderBottom: "1px solid var(--fxtr-border, #ccd5df)",
};
const tabStyle: CSSProperties = {
	padding: "3px 12px",
	fontSize: 12,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	background: "transparent",
	color: "var(--fxtr-fg, #1a2b3c)",
	cursor: "pointer",
};
const tabActiveStyle: CSSProperties = {
	...tabStyle,
	background: "var(--fxtr-accent, #2563eb)",
	color: "#ffffff",
	borderColor: "var(--fxtr-accent, #2563eb)",
};
const treePaneStyle: CSSProperties = {
	width: 320,
	minWidth: 220,
	overflow: "auto",
	paddingTop: 8,
	borderRight: "1px solid var(--fxtr-border, #ccd5df)",
};
const detailPaneStyle: CSSProperties = { flex: 1, overflow: "auto", padding: "8px 0 8px 14px" };
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12, marginTop: 8 };
const errorStyle: CSSProperties = { color: "#c04040", fontSize: 12, padding: 8 };
