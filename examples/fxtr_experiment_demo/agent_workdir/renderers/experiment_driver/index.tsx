/**
 * Scenario-picker renderer for the `experiment-demo-helper` SERVICE —
 * the experiment demo's driver panel. One button per scenario method
 * (see `packages/experiment_demo_helper`'s scenario map); each press
 * fires the invoke and appends a result card. Result dicts render with
 * entity refs as clickable `RefLink`s (input notes / task outputs open
 * through the `exp_note` renderer), so an operator can build workflow
 * state with clicks and follow the returned paths.
 *
 * Keyed by the helper's `_service_type_id().id_string()` in this dir's
 * `fxtr_renderer.nix` slice; components receive a `ServiceRefStub`.
 * Imports are restricted to the curated externals list.
 */

import {
	EntityRefStub,
	type FxtrSessionAtom,
	type OpResultStub,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { type DispatchedComponent } from "@fxtr-ui/react-dispatch";
import { atomWithDefault } from "@fxtr-ui/state-util";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
	RefLink,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue, useSetAtom } from "jotai";
import { type CSSProperties, type ReactNode, useMemo } from "react";

// ===================================================================
// Scenario catalog
// ===================================================================

interface ScenarioButton {
	readonly method: string;
	/** What pressing it DOES (action-phrased, never a target state). */
	readonly label: string;
	/** Fuller action description when the label can't carry it. */
	readonly desc?: string;
}

interface ScenarioGroup {
	readonly title: string;
	/** How to walk the interesting arc (operator guidance — separate
	 * from what each button does). */
	readonly guide: string;
	readonly buttons: ReadonlyArray<ScenarioButton>;
}

const GROUPS: ReadonlyArray<ScenarioGroup> = [
	{
		title: "Pipeline",
		guide:
			"Build the three-cell data → task → downstream pipeline, then switch " +
			"input b between its two versions: switching away makes b's task cells " +
			"stale (demands fault on stale by design — clear to recompute), and " +
			"switching back to a version that was already computed resurrects " +
			"freshness without any rerun. The echo task reads ONLY shout's output, " +
			"so switching input b leaves its b-cell unsettled (its own observations " +
			"still match; its support is stale) rather than stale.",
		buttons: [
			{
				method: "scenario_pipeline",
				label: "Build + run pipeline",
				desc: "create the grids, write the three inputs, target + demand the three sink cells",
			},
			{
				method: "overwrite_input",
				label: "Set input b = “fog” version",
				desc: "overwrite inputs[b] with the alternate fog-at-dusk note",
			},
			{
				method: "revert_input",
				label: "Set input b = “gulls” version",
				desc: "overwrite inputs[b] back to the original gulls note",
			},
			{
				method: "clear_and_rerun_pipeline",
				label: "Clear non-fresh cells + rerun",
				desc: "clear every pipeline task cell that isn't fresh, then re-demand the sinks",
			},
			{
				method: "scenario_unsettled",
				label: "Run echo task over shout",
				desc: "target + demand the echoed task (reads only shout's output) — then switch input b and watch its b-cell go unsettled",
			},
		],
	},
	{
		title: "Subtrees + run-time task creation",
		guide:
			"Two namespace shapes for the dashboard: a subtree workspace whose " +
			"child grids span the subtree's own axis (flattened-prefix coords in " +
			"the tables), and a task whose run creates grids — a draft data grid " +
			"plus a nested polish task — inside its own internals (browse the " +
			"planned cell's internals to see them).",
		buttons: [
			{
				method: "scenario_subtree",
				label: "Build + run variants subtree",
				desc: "create the variants subtree (axis v: red/blue) with a per-variant cfg data grid and a styled task grid; target + demand both cells",
			},
			{
				method: "scenario_nested",
				label: "Run task-creating task",
				desc: "target + demand the planned task, whose run creates a draft data grid and a nested polish task inside its internals",
			},
			{
				method: "scenario_rail",
				label: "Build cross-scope axis (plate rail)",
				desc: "a task inside a sweep subtree vmaps the top-level inputs over x, so the x axis-plate spans two parents — in the Plates tab it splits into two boxes tied by a rail",
			},
		],
	},
	{
		title: "Slow checkpointed task + crash/revive",
		guide:
			"Start the slow task and watch it run; crash the runner mid-run — the " +
			"generation is interrupted, and this panel survives (it holds the " +
			"supervisor, not the runner). Revive and re-demand: the same " +
			"generation resumes, with completed steps replayed from their durable " +
			"checkpoints.",
		buttons: [
			{
				method: "scenario_slow",
				label: "Target + demand slow task",
				desc: "starts the demand in the background and returns immediately",
			},
			{
				method: "crash_runner",
				label: "Crash runner",
				desc: "cancel the whole runner task in-process; running work is interrupted",
			},
			{
				method: "revive_runner",
				label: "Revive runner",
				desc: "start a fresh runner over the same durable state (re-binds fns, re-registers the capability)",
			},
			{
				method: "rerun_slow",
				label: "Re-demand slow task",
				desc: "background demand again; an interrupted generation resumes",
			},
		],
	},
	{
		title: "Flaky task",
		guide:
			"The flaky fn fails its first invocation after (re)arming, so the " +
			"first demand records an errored generation; demanding again retries " +
			"and completes.",
		buttons: [
			{
				method: "scenario_flaky",
				label: "Arm failure + run flaky task",
				desc: "re-arm the planted failure, clear the cell, then target + demand it",
			},
			{ method: "retry_flaky", label: "Re-demand flaky task" },
		],
	},
	{
		title: "Handle boundness",
		guide:
			"The “reversed” task is wired to the “reverse” fn, " +
			"which is deliberately not auto-bound. Run it with the handle unbound " +
			"to see the fault; bind the fn and re-demand to complete; release the " +
			"binding to flip the handle back to unbound.",
		buttons: [
			{
				method: "scenario_unbound",
				label: "Reset + run reversed task",
				desc: "release our “reverse” binding if held, clear the cell, then target + demand it",
			},
			{
				method: "bind_extra",
				label: "Bind “reverse” fn",
				desc: "mount the host-side fn and bind it to the handle",
			},
			{ method: "rerun_unbound", label: "Re-demand reversed task" },
			{ method: "release_extra", label: "Release “reverse” binding" },
		],
	},
];

// ===================================================================
// Panel state
// ===================================================================

const PANEL_KIND = "experiment_driver/v1";

interface RunEntry {
	readonly id: number;
	readonly method: string;
	readonly stub: OpResultStub<unknown>;
}

interface PanelState {
	readonly kind: typeof PANEL_KIND;
	readonly runs: ReadonlyArray<RunEntry>;
}

const PANEL_INITIAL: PanelState = { kind: PANEL_KIND, runs: [] };

function isPanel(v: PanelState): boolean {
	return v != null && typeof v === "object" && v.kind === PANEL_KIND;
}

// ===================================================================
// Result rendering
// ===================================================================

/** Ref → block RefLink; dict/array → one row per entry; scalar → text. */
function renderValue(value: unknown): ReactNode {
	if (value instanceof EntityRefStub || value instanceof ServiceRefStub) {
		return <RefLink stub={value} as="block" />;
	}
	if (Array.isArray(value)) {
		return (
			<div style={dictStyle}>
				{value.map((v, i) => (
					<div key={i} style={dictRowStyle}>
						{renderValue(v)}
					</div>
				))}
			</div>
		);
	}
	if (value !== null && typeof value === "object") {
		return (
			<div style={dictStyle}>
				{Object.entries(value as Record<string, unknown>).map(([k, v]) => (
					<div key={k} style={dictRowStyle}>
						<span style={dictKeyStyle}>{k}</span>
						{renderValue(v)}
					</div>
				))}
			</div>
		);
	}
	return (
		<pre style={scalarStyle}>
			{typeof value === "string" ? value : JSON.stringify(value, null, 2)}
		</pre>
	);
}

function RunCard({ entry }: { readonly entry: RunEntry }) {
	const r = useResult(entry.stub);
	return (
		<div style={cardStyle}>
			<div style={cardLabelStyle}>
				#{entry.id} {entry.method} · {r.status}
			</div>
			{r.status === "filled" ? (
				renderValue(r.value)
			) : r.status === "errored" ? (
				<div style={errorStyle}>{r.message}</div>
			) : (
				<div style={mutedStyle}>running…</div>
			)}
		</div>
	);
}

// ===================================================================
// FullPanel
// ===================================================================

export const ExperimentDriverFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const panelAtom = useMemo(
		() =>
			atomWithDefault<PanelState>(
				ownStorage as FxtrSessionAtom<PanelState | null>,
				PANEL_INITIAL,
				isPanel,
			),
		[ownStorage],
	);
	const panel = useAtomValue(panelAtom);
	const setPanel = useSetAtom(panelAtom);

	const fire = (method: string) => {
		setPanel((p, ctx) => ({
			...p,
			runs: [
				...p.runs,
				{
					id: p.runs.length === 0 ? 1 : p.runs[p.runs.length - 1].id + 1,
					method,
					stub: ctx.allocateInvoke(stub, method, []),
				},
			],
		}));
	};

	return (
		<div style={panelStyle}>
			<h2 style={titleStyle}>experiment demo driver</h2>
			<p style={blurbStyle}>
				Each button drives the <code>experiment-workflow</code> capability the same way any
				client would — press one, then follow the returned paths (the dashboard panel joins in
				a later chunk; until then, inspect state via the returned refs and the shell).
			</p>
			{GROUPS.map((g) => (
				<div key={g.title} style={groupStyle}>
					<div style={groupTitleStyle}>{g.title}</div>
					<div style={groupBlurbStyle}>{g.guide}</div>
					<div style={buttonListStyle}>
						{g.buttons.map((b) => (
							<div key={b.method} style={buttonRowStyle}>
								<button
									type="button"
									style={buttonStyle}
									onClick={() => fire(b.method)}
								>
									{b.label}
								</button>
								{b.desc && <span style={buttonDescStyle}>{b.desc}</span>}
							</div>
						))}
					</div>
				</div>
			))}
			<div style={runsHeaderStyle}>
				<span>runs</span>
				<button
					type="button"
					style={buttonStyle}
					onClick={() => setPanel((p) => ({ ...p, runs: [] }))}
				>
					clear
				</button>
			</div>
			{panel.runs.length === 0 ? (
				<div style={mutedStyle}>nothing run yet</div>
			) : (
				[...panel.runs].reverse().map((entry) => <RunCard key={entry.id} entry={entry} />)
			)}
		</div>
	);
};

// ----- link keys: a labeled pill -----

function DriverPill() {
	return (
		<span style={pillStyle}>
			<span style={pillTagStyle}>driver</span>
			<span>experiment demo</span>
		</span>
	);
}

export const ExperimentDriverBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = () => (
	<DriverPill />
);
export const ExperimentDriverInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> = () => (
	<DriverPill />
);
export const ExperimentDriverHoverPreview: DispatchedComponent<
	ServiceRefStub,
	HoverPreviewProps
> = () => (
	<div style={hoverCardStyle}>
		the experiment demo's scenario driver — open it in a panel and press buttons
	</div>
);

// ----- styles -----

const panelStyle: CSSProperties = {
	padding: 16,
	height: "100%",
	overflow: "auto",
	color: "var(--fxtr-fg, #1a2b3c)",
	fontSize: 13,
};
const titleStyle: CSSProperties = { margin: 0, fontSize: 18 };
const blurbStyle: CSSProperties = { fontSize: 12, opacity: 0.8 };
const groupStyle: CSSProperties = {
	marginTop: 12,
	padding: 10,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 8,
};
const groupTitleStyle: CSSProperties = { fontWeight: 600 };
const groupBlurbStyle: CSSProperties = { fontSize: 11, opacity: 0.75, marginTop: 2 };
const buttonListStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: 6,
	marginTop: 8,
};
const buttonRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "baseline",
	gap: 10,
};
const buttonDescStyle: CSSProperties = { fontSize: 11, opacity: 0.7 };
const buttonStyle: CSSProperties = {
	padding: "4px 10px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	background: "var(--fxtr-bg-raised, #f4f7fa)",
	cursor: "pointer",
	fontSize: 12,
};
const runsHeaderStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	justifyContent: "space-between",
	marginTop: 16,
	fontWeight: 600,
};
const cardStyle: CSSProperties = {
	marginTop: 8,
	padding: 10,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 8,
};
const cardLabelStyle: CSSProperties = { fontSize: 11, fontWeight: 600, marginBottom: 6 };
const dictStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 4 };
const dictRowStyle: CSSProperties = { display: "flex", gap: 8, alignItems: "baseline" };
const dictKeyStyle: CSSProperties = { fontSize: 11, opacity: 0.7, minWidth: 90 };
const scalarStyle: CSSProperties = { margin: 0, fontSize: 12, whiteSpace: "pre-wrap" };
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12, whiteSpace: "pre-wrap" };
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12 };
const pillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	fontSize: 12,
};
const pillTagStyle: CSSProperties = { fontWeight: 600 };
const hoverCardStyle: CSSProperties = {
	padding: 8,
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	fontSize: 12,
	maxWidth: 240,
};

export default {
	experiment_driver: {
		[FullPanel.uuid]: ExperimentDriverFullPanel,
		[BlockLink.uuid]: ExperimentDriverBlockLink,
		[InlineLink.uuid]: ExperimentDriverInlineLink,
		[HoverPreview.uuid]: ExperimentDriverHoverPreview,
	},
};
