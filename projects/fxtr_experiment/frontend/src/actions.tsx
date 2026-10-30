/**
 * The mutation log (bottom strip of the panel): one row per user
 * gesture. A gesture is a short chain of capability calls
 * (`ActionStep`s — e.g. "Target + run" = `add_target` then the
 * demand); the first step is allocated when the gesture fires, and
 * each row advances its own chain when the current step fills (an
 * errored step stops the chain and shows the error — e.g. a
 * `NotTargetedError` or a clear's running-cell refusal). Long-running
 * demands are ordinary invoke slots: the row stays pending while the
 * backend executes, and tables refresh independently.
 *
 * Steps carry `ViewSel`s pinned to grant tokens; a token dying
 * mid-chain surfaces as the step's error (re-navigate and redo — the
 * log never heals tokens itself). Results render with entity refs as
 * `RefLink`s (demand outputs, `DeleteReport` referrers as path text).
 */

import type { FxtrSessionAtom, FxtrSessionUpdateCtx, ServiceRefStub } from "@fxtr-ui/core";
import { EntityRefStub } from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { RefLink } from "@fxtr-ui/workstation-dispatch";
import { useAtomValue, useSetAtom } from "jotai";
import type { CSSProperties, ReactNode } from "react";
import { useEffect } from "react";

import type { ActionEntry, ActionStep } from "./panel_state.js";
import type { ExperimentWorkflowProto } from "./wire/proto.js";

/** Fire one gesture: label + the step chain (first step allocated
 * now, the rest advanced by the log row). */
export type RunAction = (label: string, steps: readonly ActionStep[]) => void;

/** Allocate one step's invoke through the TYPED overload (the union
 * keeps this a total switch — no loose method strings). */
export function allocateStep(
	ctx: FxtrSessionUpdateCtx,
	capability: ServiceRefStub<ExperimentWorkflowProto>,
	step: ActionStep,
): ActionEntry["current"] {
	switch (step.kind) {
		case "add_target":
			return ctx.allocateInvoke(capability, "add_target", [step.sel], { adopt: step.adopt });
		case "remove_target":
			return ctx.allocateInvoke(capability, "remove_target", [step.sel]);
		case "demand":
			return ctx.allocateInvoke(capability, "read_cell_entity", [step.sel]);
		case "demand_target":
			return ctx.allocateInvoke(capability, "demand_target", [step.record]);
		case "clear_cells":
			return ctx.allocateInvoke(capability, "clear_cells", [step.sel]);
		case "delete_cell":
			return ctx.allocateInvoke(capability, "delete_cell", [step.sel]);
		case "delete_grid":
			return ctx.allocateInvoke(capability, "delete_grid", [step.token, step.key], {
				recursive: step.recursive,
			});
		case "source_grid":
			return ctx.allocateInvoke(capability, "source_grid", [step.sel, step.key]);
		case "revoke_grid_sourcing":
			return ctx.allocateInvoke(capability, "revoke_grid_sourcing", [step.sel, step.key], {
				force: step.force,
			});
		case "source_cell":
			return ctx.allocateInvoke(capability, "source_cell", [step.sel]);
		case "revoke_cell_sourcing":
			return ctx.allocateInvoke(capability, "revoke_cell_sourcing", [step.sel]);
	}
}

export function ActionLog({
	capability,
	actionsAtom,
}: {
	readonly capability: ServiceRefStub<ExperimentWorkflowProto>;
	readonly actionsAtom: FxtrSessionAtom<readonly ActionEntry[]>;
}): ReactNode {
	const actions = useAtomValue(actionsAtom);
	const setActions = useSetAtom(actionsAtom);
	if (actions.length === 0) return null;
	return (
		<div style={logStyle}>
			<div style={logBarStyle}>
				<span style={logTitleStyle}>actions</span>
				<button type="button" style={buttonStyle} onClick={() => setActions([])}>
					clear log
				</button>
			</div>
			{[...actions].reverse().map((entry) => (
				<ActionRow key={entry.id} entry={entry} capability={capability} actionsAtom={actionsAtom} />
			))}
		</div>
	);
}

function ActionRow({
	entry,
	capability,
	actionsAtom,
}: {
	readonly entry: ActionEntry;
	readonly capability: ServiceRefStub<ExperimentWorkflowProto>;
	readonly actionsAtom: FxtrSessionAtom<readonly ActionEntry[]>;
}): ReactNode {
	const res = useResult(entry.current);
	const setActions = useSetAtom(actionsAtom);

	// Advance the chain when the current step fills. The identity
	// guard (same entry id AND same current stub) makes a re-run of
	// the effect a no-op instead of a double-allocate.
	useEffect(() => {
		if (res.status !== "filled" || entry.pending.length === 0) return;
		setActions((prev, ctx) =>
			prev.map((e) =>
				e.id === entry.id && e.current === entry.current && e.pending.length > 0
					? {
							...e,
							current: allocateStep(ctx, capability, e.pending[0] as ActionStep),
							pending: e.pending.slice(1),
						}
					: e,
			),
		);
	}, [res.status, entry, capability, setActions]);

	const stepsLeft = entry.pending.length;
	return (
		<div style={rowStyle}>
			<span style={labelStyle}>{entry.label}</span>
			{res.status === "pending" ? (
				<span style={mutedStyle}>running{stepsLeft > 0 ? ` (+${stepsLeft} more)` : ""}…</span>
			) : res.status === "errored" ? (
				<span style={errorStyle} title={res.message}>
					failed: {errorGist(res.message)}
				</span>
			) : stepsLeft > 0 ? (
				<span style={mutedStyle}>step done, continuing…</span>
			) : (
				<span style={resultStyle}>
					done <ResultValueView value={res.value} />
				</span>
			)}
		</div>
	);
}

/** The informative line of a wire error: transit messages are often
 * whole tracebacks, whose LAST non-empty line names the exception —
 * the full text stays on the row's tooltip. */
function errorGist(message: string): string {
	const lines = message
		.split("\n")
		.map((l) => l.trim())
		.filter(Boolean);
	return (lines[lines.length - 1] ?? "").slice(0, 200);
}

/** Compact result rendering: refs as links, dicts one entry per ref,
 * `DeleteReport`s by their referrer paths, scalars as text. */
function ResultValueView({ value }: { readonly value: unknown }): ReactNode {
	if (value === null || value === undefined) return null;
	if (value instanceof EntityRefStub) {
		return <RefLink stub={value} as="inline" />;
	}
	if (typeof value === "boolean") {
		return <span style={mutedStyle}>{value ? "(record existed)" : "(nothing there)"}</span>;
	}
	if (typeof value === "object") {
		const record = value as Record<string, unknown>;
		if (Array.isArray(record.broken_referrers)) {
			const broken = record.broken_referrers as readonly string[];
			return broken.length === 0 ? (
				<span style={mutedStyle}>(no rules referenced it)</span>
			) : (
				<span style={errorStyle}>rules referencing the deleted grid: {broken.join(", ")}</span>
			);
		}
		const entries = Object.entries(record);
		return (
			<span style={resultsInlineStyle}>
				{entries.map(([k, v]) => (
					<span key={k} style={resultsInlineStyle}>
						{k !== "" && <span style={mutedStyle}>{k}:</span>}
						{v instanceof EntityRefStub ? (
							<RefLink stub={v} as="inline" />
						) : (
							<span style={mutedStyle}>{String(v)}</span>
						)}
					</span>
				))}
			</span>
		);
	}
	return <span style={mutedStyle}>{String(value)}</span>;
}

const logStyle: CSSProperties = {
	borderTop: "1px solid var(--fxtr-border, #ccd5df)",
	marginTop: 8,
	paddingTop: 4,
	maxHeight: 160,
	overflow: "auto",
	fontSize: 12,
};
const logBarStyle: CSSProperties = {
	display: "flex",
	justifyContent: "space-between",
	alignItems: "baseline",
};
const logTitleStyle: CSSProperties = { fontWeight: 600, fontSize: 11, opacity: 0.75 };
const rowStyle: CSSProperties = {
	display: "flex",
	gap: 10,
	alignItems: "baseline",
	padding: "1px 0",
	flexWrap: "wrap",
};
const labelStyle: CSSProperties = { fontWeight: 500, fontSize: 11.5, whiteSpace: "nowrap" };
const mutedStyle: CSSProperties = { opacity: 0.65, fontSize: 11 };
const errorStyle: CSSProperties = { color: "#c04040", fontSize: 11 };
const resultStyle: CSSProperties = {
	fontSize: 11,
	display: "inline-flex",
	gap: 6,
	alignItems: "baseline",
	flexWrap: "wrap",
};
const resultsInlineStyle: CSSProperties = {
	display: "inline-flex",
	gap: 4,
	alignItems: "baseline",
};
const buttonStyle: CSSProperties = {
	padding: "1px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	background: "var(--fxtr-bg-raised, #f4f7fa)",
	cursor: "pointer",
	fontSize: 11,
};
