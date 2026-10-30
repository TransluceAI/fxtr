/**
 * Stage-picker renderer for the `behavior-demo-helper` SERVICE — the
 * behavior-research scenario's driver panel (impl:
 * `examples/fxtr_experiment_demo/behavior/behavior_demo_helper_service.py`).
 * Same mechanics as `renderers/experiment_driver/`: one button per
 * driver method, each press fires the invoke and appends a result card
 * with entity refs as clickable `RefLink`s (transcripts, configs,
 * verdicts, and the analysis table open through the
 * `behavior_entities` renderers).
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
// Stage catalog
// ===================================================================

interface StageButton {
	readonly method: string;
	/** What pressing it DOES (action-phrased, never a target state). */
	readonly label: string;
	readonly desc?: string;
}

interface StageGroup {
	readonly title: string;
	/** Operator guidance for walking the arc. */
	readonly guide: string;
	readonly buttons: ReadonlyArray<StageButton>;
}

const GROUPS: ReadonlyArray<StageGroup> = [
	{
		title: "The scenario, in stages",
		guide:
			"A mini behavior-research pipeline: car-sales assistants (subject " +
			"models swept by system prompt) talk to simulated customers; LLM " +
			"judges score each transcript against rubrics; an analysis " +
			"aggregates mean scores per (subject model, judge). Press the " +
			"stages in order. Buttons are safe to re-press (identical writes " +
			"are no-ops), but a cell someone edited to a different value makes " +
			"the button error with the write conflict — replace edited cells " +
			"explicitly instead. Which LLM services the handles name depends " +
			"on the backend's launch flag (--allow-llm-apis / " +
			"--mocked-llm-apis); re-pressing stage 1 after a mode switch " +
			"rewrites the handle keys and fans staleness through the old " +
			"mode's evidence — that's provenance working, not a bug.",
		buttons: [
			{
				method: "stage_structure",
				label: "Stage 1: build structure",
				desc: "open every grid (tasks / services / configs / domain / evidence / analyses) and write the current mode's handle keys",
			},
			{
				method: "stage_inputs",
				label: "Stage 2: write inputs",
				desc: "write the subject-model / persona / judge configs, the rollout config, and the active-domain cells (2 subjects x 2 sims x 2 judges, 1 replica)",
			},
			{
				method: "stage_analysis",
				label: "Stage 3: open analysis",
				desc: "open analyses/sales_report, wired over the judgements' full cross product + the domain cells (targeting and running are separate)",
			},
			{
				method: "stage_augment",
				label: "Stage 4: widen domain",
				desc: "add a subject model, a persona, and a judge; overwrite the domain cells (3 x 3 x 3, 2 replicas) — the analysis goes stale, existing evidence stays fresh",
			},
		],
	},
	{
		title: "Run controls",
		guide:
			"Target the analysis once, then run it: the demand pulls every " +
			"needed judgement, which pulls every needed rollout — watch cells " +
			"fill in the dashboard, with per-turn log lines streaming in each " +
			"running rollout's drawer. After stage 4, clear stale + run again: " +
			"only the new cells compute (old rollouts are served fresh). " +
			"Crash/revive is the durable-resumption arc: crash mid-run, revive " +
			"over the same state, re-run — completed steps and completed " +
			"nested generations replay from durable state.",
		buttons: [
			{
				method: "target_analysis",
				label: "Target analysis",
				desc: "durably declare the analysis cell a wanted DAG root (required before running)",
			},
			{
				method: "run_analysis",
				label: "Run analysis",
				desc: "demand the analysis in the background and return immediately — press Status (or watch the dashboard) for progress",
			},
			{
				method: "clear_stale",
				label: "Clear stale",
				desc: "clear every non-fresh task cell across evidence/ + analyses/ + transformations/ (demands fault on stale by design)",
			},
			{
				method: "status",
				label: "Status",
				desc: "one labeled snapshot: LLM mode, domain sizes, per-grid currency counts, analysis + background-demand states",
			},
			{
				method: "crash_runner",
				label: "Crash runner",
				desc: "cancel live background demands, then tear the whole runner down in-process (running generations poison to interrupted)",
			},
			{
				method: "revive_runner",
				label: "Revive runner",
				desc: "bring a fresh runner up over the same durable state (re-binds services, re-registers behavior-workflow)",
			},
		],
	},
];

// The transformation form (the one parameterized method): field order =
// the improve_sim invoke's positional args.
const IMPROVE_FIELDS = [
	{ key: "sim_id", label: "sim id", placeholder: "cautious_buyer" },
	{ key: "subj_id", label: "subject id", placeholder: "baseline" },
	{ key: "judge_id", label: "judge id", placeholder: "pushiness" },
	{
		key: "instruction",
		label: "instruction",
		placeholder: "make the customer harder to close",
	},
] as const;

const IMPROVE_GUIDE =
	"The ABR-style transformation: select the completed rollout+judgement " +
	"pairs for one (sim, subject, judge), snapshot them into a pinned " +
	"request entity, and run the improvement flow — the model proposes a " +
	"persona rewrite, the flow trials it with nested rollout+judge runs " +
	"in its own internals (same task fns, new place, isolated state), " +
	"iterates once more, then makes a final choice. All four inputs are " +
	"required; run the analysis first so evidence exists.";

// ===================================================================
// Panel state
// ===================================================================

const PANEL_KIND = "behavior_driver/v2";

interface RunEntry {
	readonly id: number;
	readonly method: string;
	readonly stub: OpResultStub<unknown>;
}

interface PanelState {
	readonly kind: typeof PANEL_KIND;
	readonly runs: ReadonlyArray<RunEntry>;
	/** The transform form's field values (persisted with the panel). */
	readonly improveInputs: Readonly<Record<string, string>>;
}

const PANEL_INITIAL: PanelState = { kind: PANEL_KIND, runs: [], improveInputs: {} };

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

export const BehaviorDriverFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
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

	const fire = (method: string, args: ReadonlyArray<unknown> = []) => {
		setPanel((p, ctx) => ({
			...p,
			runs: [
				...p.runs,
				{
					id: p.runs.length === 0 ? 1 : p.runs[p.runs.length - 1].id + 1,
					method,
					stub: ctx.allocateInvoke(stub, method, [...args]),
				},
			],
		}));
	};

	const improveInputs = panel.improveInputs;
	const setImproveInput = (key: string, value: string) =>
		setPanel((p) => ({ ...p, improveInputs: { ...p.improveInputs, [key]: value } }));
	const improveReady = IMPROVE_FIELDS.every((f) => (improveInputs[f.key] ?? "").trim() !== "");

	return (
		<div style={panelStyle}>
			<h2 style={titleStyle}>behavior research driver</h2>
			<p style={blurbStyle}>
				Each button drives the <code>behavior-workflow</code> capability the same way any client
				would — build the pipeline in stages, run it, then widen the domain and watch incremental
				recompute do only the new work.
			</p>
			{GROUPS.map((g) => (
				<div key={g.title} style={groupStyle}>
					<div style={groupTitleStyle}>{g.title}</div>
					<div style={groupBlurbStyle}>{g.guide}</div>
					<div style={buttonListStyle}>
						{g.buttons.map((b) => (
							<div key={b.method} style={buttonRowStyle}>
								<button type="button" style={buttonStyle} onClick={() => fire(b.method)}>
									{b.label}
								</button>
								{b.desc && <span style={buttonDescStyle}>{b.desc}</span>}
							</div>
						))}
					</div>
				</div>
			))}
			<div style={groupStyle}>
				<div style={groupTitleStyle}>Transform: improve a user sim</div>
				<div style={groupBlurbStyle}>{IMPROVE_GUIDE}</div>
				<div style={buttonListStyle}>
					{IMPROVE_FIELDS.map((f) => (
						<label key={f.key} style={inputRowStyle}>
							<span style={inputLabelStyle}>{f.label}</span>
							<input
								style={inputStyle}
								value={improveInputs[f.key] ?? ""}
								placeholder={f.placeholder}
								onChange={(e) => setImproveInput(f.key, e.target.value)}
							/>
						</label>
					))}
					<div style={buttonRowStyle}>
						<button
							type="button"
							style={improveReady ? buttonStyle : buttonDisabledStyle}
							disabled={!improveReady}
							onClick={() =>
								fire(
									"improve_sim",
									IMPROVE_FIELDS.map((f) => (improveInputs[f.key] ?? "").trim()),
								)
							}
						>
							Improve user sim
						</button>
						<span style={buttonDescStyle}>
							build the request + run the improvement flow in the background
						</span>
					</div>
				</div>
			</div>
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
			<span>behavior research</span>
		</span>
	);
}

export const BehaviorDriverBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = () => (
	<DriverPill />
);
export const BehaviorDriverInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> = () => (
	<DriverPill />
);
export const BehaviorDriverHoverPreview: DispatchedComponent<
	ServiceRefStub,
	HoverPreviewProps
> = () => (
	<div style={hoverCardStyle}>
		the behavior-research scenario's driver — open it in a panel and walk the stages
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
	whiteSpace: "nowrap",
};
const buttonDisabledStyle: CSSProperties = {
	...buttonStyle,
	opacity: 0.45,
	cursor: "not-allowed",
};
const inputRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "baseline",
	gap: 10,
};
const inputLabelStyle: CSSProperties = { fontSize: 11, opacity: 0.7, minWidth: 90 };
const inputStyle: CSSProperties = {
	flex: 1,
	maxWidth: 440,
	padding: "3px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	fontSize: 12,
	fontFamily: "inherit",
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
const dictKeyStyle: CSSProperties = { fontSize: 11, opacity: 0.7, minWidth: 110 };
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
	behavior_driver: {
		[FullPanel.uuid]: BehaviorDriverFullPanel,
		[BlockLink.uuid]: BehaviorDriverBlockLink,
		[InlineLink.uuid]: BehaviorDriverInlineLink,
		[HoverPreview.uuid]: BehaviorDriverHoverPreview,
	},
};
