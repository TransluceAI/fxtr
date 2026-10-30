/**
 * Pure derivation: which mutation affordances one cell offers, from
 * its kind/mode/status/intent state (`notes/monitoring_ui.md`'s
 * demand-affordance doctrine). The UI renders exactly this list, so
 * the doctrine is unit-testable without React:
 *
 * - Nothing on internals grids (read-only from the top — and the
 *   capability refuses internals intent anyway), subtree/checkpoint
 *   cells, or a running task cell. The gate is internals-ness, NOT
 *   the grant's mode: task grants are read-shaped by design, so mode
 *   cannot distinguish a top-level task grid (actionable) from one
 *   inside internals. Data/handle mutations additionally require a
 *   write grant (which every non-internals walk grant has).
 * - **Never auto-target silently**: untargeted task cells offer
 *   "Target + run" / "Target" (with `needsAdopt` when the grid has no
 *   user sourcing record — targeting requires grid sourcing, and
 *   adoption is an explicit prompt); "Run" appears only on targeted
 *   cells (the by-record `demand_target`, round-tripped verbatim).
 * - Errored cells offer "Retry" (re-demand — the demand policy
 *   retries an errored-fresh generation) and "Clear + rerun"; stale /
 *   unsettled cells offer "Clear + rerun" only (a plain demand faults
 *   on non-fresh support by design — the diagnosis names the fix).
 * - data/dataframe/handle cells: target toggle, delete (confirmed),
 *   and the cell-sourcing record's add/revoke.
 */

import type { Currency, ExecStatus, GridKind, SliceMode } from "../wire/types.js";

export type CellActionId =
	| "run"
	| "retry"
	| "clear_rerun"
	| "clear"
	| "target_run"
	| "target"
	| "untarget"
	| "delete_cell"
	| "source_cell"
	| "revoke_cell_sourcing";

export type CellAction = {
	readonly id: CellActionId;
	readonly label: string;
	/** Targeting into an unsourced grid: prompt for explicit adoption
	 * (an inline confirm) instead of calling with `adopt` silently. */
	readonly needsAdopt?: boolean;
	/** Destructive: render behind an inline confirm step. */
	readonly confirm?: boolean;
};

export type CellActionCtx = {
	readonly kind: GridKind;
	readonly mode: SliceMode;
	/** The grid lives inside a task's internals subtree. */
	readonly internals: boolean;
	readonly status: ExecStatus | null;
	readonly currency: Currency | null;
	readonly targeted: boolean;
	/** The grid has a user sourcing record (`GridSummary.sourcing` is
	 * non-null — dangling still counts as declared intent). */
	readonly gridSourced: boolean;
	/** This cell has a cell-level sourcing record. */
	readonly hasCellSourcing: boolean;
	/** The cell holds something (a value / any generation state). */
	readonly populated: boolean;
};

export function deriveCellActions(ctx: CellActionCtx): readonly CellAction[] {
	if (ctx.internals) return [];
	const adopt = ctx.gridSourced ? {} : { needsAdopt: true };
	if (ctx.kind === "task") {
		if (ctx.status === "running") return [];
		const out: CellAction[] = [];
		if (ctx.targeted) {
			const nonFresh = ctx.currency === "stale" || ctx.currency === "unsettled";
			if (ctx.status === "errored") {
				out.push({ id: "retry", label: "Retry" });
				out.push({ id: "clear_rerun", label: "Clear + rerun" });
			} else if (nonFresh) {
				out.push({ id: "clear_rerun", label: "Clear + rerun" });
			} else {
				out.push({ id: "run", label: "Run" });
			}
			if (ctx.populated) out.push({ id: "clear", label: "Clear", confirm: true });
			out.push({ id: "untarget", label: "Untarget" });
		} else {
			out.push({ id: "target_run", label: "Target + run", ...adopt });
			out.push({ id: "target", label: "Target", ...adopt });
			if (ctx.populated) out.push({ id: "clear", label: "Clear", confirm: true });
		}
		return out;
	}
	if (ctx.kind === "data" || ctx.kind === "dataframe" || ctx.kind === "handle") {
		if (ctx.mode !== "write") return [];
		const out: CellAction[] = [];
		out.push(
			ctx.targeted
				? { id: "untarget", label: "Untarget" }
				: { id: "target", label: "Target", ...adopt },
		);
		if (ctx.populated) {
			out.push(
				ctx.hasCellSourcing
					? { id: "revoke_cell_sourcing", label: "Revoke cell sourcing" }
					: { id: "source_cell", label: "Source cell (adopt value)" },
			);
			out.push({ id: "delete_cell", label: "Delete cell", confirm: true });
		}
		return out;
	}
	return [];
}
