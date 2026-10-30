/**
 * Renderer for the `WorkstationDemoHelper` SERVICE (host-side helper defined in
 * `examples/fxtr_workstation_demo/demo_helper_service.py`, marker package
 * `packages/demo_helper/`). This is the **self-contained demo auto-driver**: a
 * small in-panel UI that stands in for "ask an agent to spawn service X and run
 * Y", except a human drives it by clicking.
 *
 * Each **scenario** is its own component managing its own state slice (no uniform
 * step abstraction). A scenario spawns service(s) via the helper, **presents** the
 * returned ref(s) as clickable `RefLink`s (open one to see that service's own
 * renderer panel — e.g. the scheduler's live run grid), then drives the
 * *interesting* invoke(s) directly on the returned refs and presents each result
 * as a `RefLink` too. The driver deliberately does NO channel/grid rendering of
 * its own — the live stream lives on the scheduler's panel; only the final
 * `run_batched` result returns here.
 *
 * Keyed by the helper's `_service_type_id().id_string()` in this dir's
 * `fxtr_renderer.nix` slice (same table as entity renderers); components receive a
 * `ServiceRefStub`. Imports are restricted to the curated externals list (react,
 * jotai, `@fxtr-ui/*`).
 */

import {
	EntityRefStub,
	type FxtrSessionAtom,
	type OpResultStub,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { useResult } from "@fxtr-ui/react";
import { type DispatchedComponent } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, subAtom } from "@fxtr-ui/state-util";
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
import { type CSSProperties, type ReactNode, useMemo, useState } from "react";

// ===================================================================
// Shared result rendering
// ===================================================================

/** Render an invoke/spawn result value: ref → block `RefLink` (opens its own
 * panel); dict/array → one child per entry; scalar → preformatted text. */
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

/** A labelled card showing one op-result's status + value. Always mounted with a
 * non-null stub so `useResult` is unconditional. */
function ResultRow({ label, stub }: { readonly label: string; readonly stub: OpResultStub<unknown> }) {
	const r = useResult(stub);
	return (
		<div style={cardStyle}>
			<div style={cardLabelStyle}>
				{label} · {r.status}
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

/** Render `children(value)` once `stub` is filled (else nothing — a sibling
 * `ResultRow` shows the pending/error state). Lets a later step read a prior
 * result's resolved ref to feed into its own invoke. */
function WhenFilled<T>({
	stub,
	children,
}: {
	readonly stub: OpResultStub<T>;
	readonly children: (value: T) => ReactNode;
}) {
	const r = useResult(stub);
	if (r.status !== "filled") return null;
	return <>{children(r.value)}</>;
}

function Toolbar({ onBack, onReset }: { readonly onBack: () => void; readonly onReset: () => void }) {
	return (
		<div style={toolbarStyle}>
			<button type="button" onClick={onBack} style={buttonStyle}>
				← scenarios
			</button>
			<button type="button" onClick={onReset} style={buttonStyle}>
				reset
			</button>
		</div>
	);
}

function Step({ n, label, children }: { readonly n: number; readonly label: string; readonly children: ReactNode }) {
	return (
		<div style={stepStyle}>
			<div style={stepLabelStyle}>
				<span style={stepNumStyle}>{n}</span>
				{label}
			</div>
			<div style={stepBodyStyle}>{children}</div>
		</div>
	);
}

// ===================================================================
// Scenario: spawn workflow → make inputs → run_batched
// ===================================================================
//
// Shared by the simple / streaming / vivid scenarios — they differ only in the
// spawn method and how `run_batched`'s args are shaped.

const WF_KIND = "demo_helper_wf/v1";

interface WfState {
	readonly kind: typeof WF_KIND;
	readonly spawn: OpResultStub<unknown> | null; // {scheduler, task}
	readonly inputs: OpResultStub<unknown> | null; // {name: noteRef}
	readonly run: OpResultStub<unknown> | null; // SimpleRunResult ref
}
const WF_INITIAL: WfState = { kind: WF_KIND, spawn: null, inputs: null, run: null };
function isWf(s: WfState): boolean {
	return s?.kind === WF_KIND;
}

function WorkflowScenario({
	stub,
	storage,
	onBack,
	spawnMethod,
	buildRunArgs,
}: {
	readonly stub: ServiceRefStub<unknown>;
	readonly storage: FxtrSessionAtom<unknown>;
	readonly onBack: () => void;
	readonly spawnMethod: string;
	readonly buildRunArgs: (task: unknown, inputs: unknown) => readonly unknown[];
}) {
	const atom = useMemo(
		() => atomWithDefault<WfState>(storage as FxtrSessionAtom<WfState | null>, WF_INITIAL, isWf),
		[storage],
	);
	const state = useAtomValue(atom);
	const setState = useSetAtom(atom);

	const spawn = (): void =>
		setState((p, ctx) => ({ ...p, spawn: ctx.allocateInvoke(stub, spawnMethod, []) }));
	const makeInputs = (): void =>
		setState((p, ctx) => ({ ...p, inputs: ctx.allocateInvoke(stub, "make_demo_inputs", []) }));

	return (
		<div style={scenarioStyle}>
			<Toolbar onBack={onBack} onReset={() => setState(WF_INITIAL)} />
			<Step n={1} label="Spawn the workflow services (scheduler + task)">
				<button type="button" onClick={spawn} disabled={state.spawn !== null} style={buttonStyle}>
					spawn services
				</button>
				{state.spawn !== null && <ResultRow label="services" stub={state.spawn} />}
				<div style={mutedStyle}>Open the scheduler ref to watch live progress in its own panel.</div>
			</Step>
			<Step n={2} label="Make demo input notes">
				<button
					type="button"
					onClick={makeInputs}
					disabled={state.spawn === null || state.inputs !== null}
					style={buttonStyle}
				>
					make inputs
				</button>
				{state.inputs !== null && <ResultRow label="inputs" stub={state.inputs} />}
			</Step>
			<Step n={3} label="Run the batch — the final result returns here">
				{state.spawn !== null && state.inputs !== null && state.run === null && (
					<WhenFilled stub={state.spawn}>
						{(sv) => (
							<WhenFilled stub={state.inputs as OpResultStub<unknown>}>
								{(iv) => (
									<button
										type="button"
										style={buttonStyle}
										onClick={() =>
											setState((p, ctx) => ({
												...p,
												run: ctx.allocateInvoke(
													(sv as { scheduler: ServiceRefStub<unknown> }).scheduler,
													"run_batched",
													buildRunArgs((sv as { task: unknown }).task, iv),
												),
											}))
										}
									>
										run_batched
									</button>
								)}
							</WhenFilled>
						)}
					</WhenFilled>
				)}
				{state.run !== null && <ResultRow label="result" stub={state.run} />}
			</Step>
		</div>
	);
}

// ===================================================================
// Scenario: configurable streaming workflow (caching playground)
// ===================================================================
//
// Like WorkflowScenario but with editable inputs + a toggleable step set + a
// run *history*, so you can exercise the streaming scheduler's per-cell cache:
// re-run the same notes/steps and watch cells come back `cached` (badged in the
// scheduler's own grid panel + flagged in the returned SimpleRunResult). Notes
// are content-addressed, so the same title/body yields the same input id across
// runs — which is what makes a cell a cache hit. Used by the streaming + vivid
// scenarios (both spawn the streaming scheduler).

const CFG_KIND = "demo_helper_cfg/v1";

interface NoteSpec {
	readonly id: number;
	readonly title: string;
	readonly body: string;
}
interface StepSpec {
	readonly id: number;
	readonly name: string;
	readonly enabled: boolean;
}
interface CfgState {
	readonly kind: typeof CFG_KIND;
	readonly spawn: OpResultStub<unknown> | null; // {scheduler, task}
	readonly inputs: OpResultStub<unknown> | null; // {title: noteRef}
	readonly notes: ReadonlyArray<NoteSpec>;
	readonly steps: ReadonlyArray<StepSpec>;
	readonly runs: ReadonlyArray<InvokeSlot>; // SimpleRunResult refs (history)
}
function makeCfgInitial(stepNames: readonly string[]): CfgState {
	return {
		kind: CFG_KIND,
		spawn: null,
		inputs: null,
		notes: [
			{ id: 1, title: "calm sea", body: "A quiet tide breathing at dawn." },
			{ id: 2, title: "city night", body: "Neon smeared across wet streets." },
		],
		// First step on, the rest off — toggle them to grow/shrink the chain.
		steps: stepNames.map((name, i) => ({ id: i + 1, name, enabled: i === 0 })),
		runs: [],
	};
}
function isCfg(s: CfgState): boolean {
	return s?.kind === CFG_KIND && Array.isArray(s.notes) && Array.isArray(s.steps);
}
function maxId(list: ReadonlyArray<{ readonly id: number }>): number {
	let m = 0;
	for (const e of list) if (e.id > m) m = e.id;
	return m;
}

function ConfigurableWorkflowScenario({
	stub,
	storage,
	onBack,
	spawnMethod,
	defaultSteps,
}: {
	readonly stub: ServiceRefStub<unknown>;
	readonly storage: FxtrSessionAtom<unknown>;
	readonly onBack: () => void;
	readonly spawnMethod: string;
	readonly defaultSteps: readonly string[];
}) {
	const initial = useMemo(() => makeCfgInitial(defaultSteps), [defaultSteps]);
	const atom = useMemo(
		() => atomWithDefault<CfgState>(storage as FxtrSessionAtom<CfgState | null>, initial, isCfg),
		[storage, initial],
	);
	const state = useAtomValue(atom);
	const setState = useSetAtom(atom);

	const spawn = (): void =>
		setState((p, ctx) => ({ ...p, spawn: ctx.allocateInvoke(stub, spawnMethod, []) }));
	// Building inputs re-mints the notes; editing a note clears the built inputs
	// (set `inputs: null`) so you rebuild before the next run.
	const buildInputs = (): void =>
		setState((p, ctx) => ({
			...p,
			inputs: ctx.allocateInvoke(stub, "make_inputs", [
				p.notes.map((n) => ({ title: n.title, body: n.body })),
			]),
		}));

	const addNote = (): void =>
		setState((p) => ({
			...p,
			notes: [...p.notes, { id: maxId(p.notes) + 1, title: "", body: "" }],
			inputs: null,
		}));
	const removeNote = (id: number): void =>
		setState((p) => ({ ...p, notes: p.notes.filter((n) => n.id !== id), inputs: null }));
	const editNote = (id: number, field: "title" | "body", value: string): void =>
		setState((p) => ({
			...p,
			notes: p.notes.map((n) => (n.id === id ? { ...n, [field]: value } : n)),
			inputs: null,
		}));

	const toggleStep = (id: number): void =>
		setState((p) => ({
			...p,
			steps: p.steps.map((s) => (s.id === id ? { ...s, enabled: !s.enabled } : s)),
		}));
	const addStep = (): void =>
		setState((p) => ({
			...p,
			steps: [...p.steps, { id: maxId(p.steps) + 1, name: `step-${p.steps.length + 1}`, enabled: true }],
		}));
	const removeStep = (id: number): void =>
		setState((p) => ({ ...p, steps: p.steps.filter((s) => s.id !== id) }));
	const editStepName = (id: number, name: string): void =>
		setState((p) => ({ ...p, steps: p.steps.map((s) => (s.id === id ? { ...s, name } : s)) }));

	const enabledCount = state.steps.filter((s) => s.enabled).length;

	return (
		<div style={scenarioStyle}>
			<Toolbar onBack={onBack} onReset={() => setState(initial)} />
			<Step n={1} label="Spawn the streaming scheduler + task">
				<button type="button" onClick={spawn} disabled={state.spawn !== null} style={buttonStyle}>
					spawn services
				</button>
				{state.spawn !== null && <ResultRow label="services" stub={state.spawn} />}
				<div style={mutedStyle}>
					Open the scheduler ref to watch the live grid — cached cells are badged there.
				</div>
			</Step>
			<Step n={2} label="Input notes — content-addressed, so stable across runs (edit → rebuild)">
				{state.notes.map((n) => (
					<div key={n.id} style={inputRowStyle}>
						<input
							type="text"
							value={n.title}
							placeholder="title"
							onChange={(e) => editNote(n.id, "title", e.target.value)}
							style={textInputStyle}
						/>
						<input
							type="text"
							value={n.body}
							placeholder="body"
							onChange={(e) => editNote(n.id, "body", e.target.value)}
							style={{ ...textInputStyle, flex: 1 }}
						/>
						<button type="button" onClick={() => removeNote(n.id)} style={buttonStyle}>
							✕
						</button>
					</div>
				))}
				<div style={inputRowStyle}>
					<button type="button" onClick={addNote} style={buttonStyle}>
						+ note
					</button>
					<button
						type="button"
						onClick={buildInputs}
						disabled={state.spawn === null || state.notes.length === 0}
						style={buttonStyle}
					>
						build inputs
					</button>
				</div>
				{state.inputs !== null && <ResultRow label="inputs" stub={state.inputs} />}
			</Step>
			<Step n={3} label="Steps — toggle to include; the step name is the per-cell cache key">
				{state.steps.map((s) => (
					<div key={s.id} style={inputRowStyle}>
						<label style={checkboxLabelStyle}>
							<input type="checkbox" checked={s.enabled} onChange={() => toggleStep(s.id)} />
							<input
								type="text"
								value={s.name}
								onChange={(e) => editStepName(s.id, e.target.value)}
								style={textInputStyle}
							/>
						</label>
						<button type="button" onClick={() => removeStep(s.id)} style={buttonStyle}>
							✕
						</button>
					</div>
				))}
				<button type="button" onClick={addStep} style={buttonStyle}>
					+ step
				</button>
			</Step>
			<Step n={4} label="Run — re-run with the same notes/steps to see cells served from cache">
				{state.spawn !== null && state.inputs !== null && (
					<WhenFilled stub={state.spawn}>
						{(sv) => (
							<WhenFilled stub={state.inputs as OpResultStub<unknown>}>
								{(iv) => (
									<button
										type="button"
										style={buttonStyle}
										disabled={enabledCount === 0}
										onClick={() =>
											setState((p, ctx) => {
												const enabled = p.steps.filter((s) => s.enabled);
												const task = (sv as { task: ServiceRefStub<unknown> }).task;
												const namedSteps = enabled.map((s) => [s.name, task]);
												return {
													...p,
													runs: [
														...p.runs,
														{
															id: maxId(p.runs) + 1,
															label: `run #${maxId(p.runs) + 1} · [${enabled.map((s) => s.name).join(", ")}]`,
															stub: ctx.allocateInvoke(
																(sv as { scheduler: ServiceRefStub<unknown> }).scheduler,
																"run_batched",
																[namedSteps, iv],
															),
														},
													],
												};
											})
										}
									>
										run_batched
									</button>
								)}
							</WhenFilled>
						)}
					</WhenFilled>
				)}
				<div style={historyStyle}>
					{state.runs
						.slice()
						.reverse()
						.map((r) => (
							<ResultRow key={r.id} label={r.label} stub={r.stub} />
						))}
				</div>
			</Step>
		</div>
	);
}

// ===================================================================
// Scenario: delayed_echo (spawn → echo a value after a delay)
// ===================================================================

const ECHO_KIND = "demo_helper_echo/v1";

interface InvokeSlot {
	readonly id: number;
	readonly label: string;
	readonly stub: OpResultStub<unknown>;
}
interface EchoState {
	readonly kind: typeof ECHO_KIND;
	readonly spawn: OpResultStub<unknown> | null;
	readonly echoes: ReadonlyArray<InvokeSlot>;
}
const ECHO_INITIAL: EchoState = { kind: ECHO_KIND, spawn: null, echoes: [] };
function isEcho(s: EchoState): boolean {
	return s?.kind === ECHO_KIND && Array.isArray(s.echoes);
}
function nextId(list: ReadonlyArray<InvokeSlot>): number {
	let max = 0;
	for (const e of list) if (e.id > max) max = e.id;
	return max + 1;
}

function EchoScenario({
	stub,
	storage,
	onBack,
}: {
	readonly stub: ServiceRefStub<unknown>;
	readonly storage: FxtrSessionAtom<unknown>;
	readonly onBack: () => void;
}) {
	const atom = useMemo(
		() => atomWithDefault<EchoState>(storage as FxtrSessionAtom<EchoState | null>, ECHO_INITIAL, isEcho),
		[storage],
	);
	const state = useAtomValue(atom);
	const setState = useSetAtom(atom);
	const [value, setValue] = useState("hello");
	const [delay, setDelay] = useState(2);

	const spawn = (): void =>
		setState((p, ctx) => ({ ...p, spawn: ctx.allocateInvoke(stub, "spawn_delayed_echo", ["demo"]) }));

	return (
		<div style={scenarioStyle}>
			<Toolbar onBack={onBack} onReset={() => setState(ECHO_INITIAL)} />
			<Step n={1} label="Spawn the delayed-echo service">
				<button type="button" onClick={spawn} disabled={state.spawn !== null} style={buttonStyle}>
					spawn delayed_echo
				</button>
				{state.spawn !== null && <ResultRow label="service" stub={state.spawn} />}
			</Step>
			<Step n={2} label="Invoke echo(value, delay) — the value comes back after the delay">
				{state.spawn !== null && (
					<WhenFilled stub={state.spawn}>
						{(echoSvc) => (
							<div style={inputRowStyle}>
								<input
									type="text"
									value={value}
									onChange={(e) => setValue(e.target.value)}
									style={textInputStyle}
								/>
								<input
									type="number"
									value={delay}
									min={0}
									step={0.5}
									onChange={(e) => setDelay(Number.parseFloat(e.target.value) || 0)}
									style={{ ...textInputStyle, width: "7ch" }}
								/>
								<button
									type="button"
									style={buttonStyle}
									onClick={() =>
										setState((p, ctx) => ({
											...p,
											echoes: [
												...p.echoes,
												{
													id: nextId(p.echoes),
													label: `echo(${JSON.stringify(value)}, ${delay})`,
													stub: ctx.allocateInvoke(echoSvc as ServiceRefStub<unknown>, "echo", [
														value,
														Math.max(0, delay),
													]),
												},
											],
										}))
									}
								>
									invoke echo
								</button>
							</div>
						)}
					</WhenFilled>
				)}
				<div style={historyStyle}>
					{state.echoes
						.slice()
						.reverse()
						.map((slot) => (
							<ResultRow key={slot.id} label={slot.label} stub={slot.stub} />
						))}
				</div>
			</Step>
		</div>
	);
}

// ===================================================================
// Scenario: LLM ask (spawn haiku service → ask a question)
// ===================================================================

const ASK_KIND = "demo_helper_ask/v1";

interface AskState {
	readonly kind: typeof ASK_KIND;
	readonly spawn: OpResultStub<unknown> | null;
	readonly answers: ReadonlyArray<InvokeSlot>;
}
const ASK_INITIAL: AskState = { kind: ASK_KIND, spawn: null, answers: [] };
function isAsk(s: AskState): boolean {
	return s?.kind === ASK_KIND && Array.isArray(s.answers);
}

function AskScenario({
	stub,
	storage,
	onBack,
}: {
	readonly stub: ServiceRefStub<unknown>;
	readonly storage: FxtrSessionAtom<unknown>;
	readonly onBack: () => void;
}) {
	const atom = useMemo(
		() => atomWithDefault<AskState>(storage as FxtrSessionAtom<AskState | null>, ASK_INITIAL, isAsk),
		[storage],
	);
	const state = useAtomValue(atom);
	const setState = useSetAtom(atom);
	const [message, setMessage] = useState("Write a haiku about workstations.");

	const spawn = (): void =>
		setState((p, ctx) => ({ ...p, spawn: ctx.allocateInvoke(stub, "spawn_haiku", []) }));

	return (
		<div style={scenarioStyle}>
			<Toolbar onBack={onBack} onReset={() => setState(ASK_INITIAL)} />
			<Step n={1} label="Spawn the haiku service (needs --allow-llm-apis + ANTHROPIC_API_KEY)">
				<button type="button" onClick={spawn} disabled={state.spawn !== null} style={buttonStyle}>
					spawn haiku service
				</button>
				{state.spawn !== null && <ResultRow label="service" stub={state.spawn} />}
			</Step>
			<Step n={2} label="Ask the model">
				{state.spawn !== null && (
					<WhenFilled stub={state.spawn}>
						{(askSvc) => (
							<div style={inputRowStyle}>
								<input
									type="text"
									value={message}
									onChange={(e) => setMessage(e.target.value)}
									style={{ ...textInputStyle, flex: 1 }}
								/>
								<button
									type="button"
									style={buttonStyle}
									onClick={() =>
										setState((p, ctx) => ({
											...p,
											answers: [
												...p.answers,
												{
													id: nextId(p.answers),
													label: `ask(${JSON.stringify(message.slice(0, 32))}…)`,
													stub: ctx.allocateInvoke(askSvc as ServiceRefStub<unknown>, "ask", [message]),
												},
											],
										}))
									}
								>
									ask
								</button>
							</div>
						)}
					</WhenFilled>
				)}
				<div style={historyStyle}>
					{state.answers
						.slice()
						.reverse()
						.map((slot) => (
							<ResultRow key={slot.id} label={slot.label} stub={slot.stub} />
						))}
				</div>
			</Step>
		</div>
	);
}

// ===================================================================
// Scenario registry + the FullPanel picker
// ===================================================================

interface Scenario {
	readonly id: string;
	readonly title: string;
	readonly blurb: string;
	readonly initial: unknown;
	readonly render: (props: {
		stub: ServiceRefStub<unknown>;
		storage: FxtrSessionAtom<unknown>;
		onBack: () => void;
	}) => ReactNode;
}

const SCENARIOS: ReadonlyArray<Scenario> = [
	{
		id: "delayed_echo",
		title: "Delayed echo",
		blurb: "Spawn the delayed-echo service, then echo values back after a delay.",
		initial: ECHO_INITIAL,
		render: (p) => <EchoScenario {...p} />,
	},
	{
		id: "simple_workflow",
		title: "Simple workflow",
		blurb: "Spawn the simple scheduler + a task, run demo notes through it; final SimpleRunResult returns here.",
		initial: WF_INITIAL,
		render: (p) => (
			<WorkflowScenario
				{...p}
				spawnMethod="spawn_simple_workflow"
				buildRunArgs={(task, inputs) => [[task], inputs, ["shout"]]}
			/>
		),
	},
	{
		id: "streaming_workflow",
		title: "Streaming workflow (caching playground)",
		blurb: "Spawn the streaming scheduler + word_shout task; edit notes + toggle steps and re-run to watch cells come back cached.",
		initial: makeCfgInitial(["shout", "shout-again"]),
		render: (p) => (
			<ConfigurableWorkflowScenario
				{...p}
				spawnMethod="spawn_streaming_workflow"
				defaultSteps={["shout", "shout-again"]}
			/>
		),
	},
	{
		id: "vivid_workflow",
		title: "Streaming vivid-description (LLM, heavy)",
		blurb: "Streaming scheduler + a container LLM task; needs --allow-llm-apis. First spawn is slow (OCI build). Same caching playground.",
		initial: makeCfgInitial(["vivid", "vivid-again"]),
		render: (p) => (
			<ConfigurableWorkflowScenario
				{...p}
				spawnMethod="spawn_vivid_workflow"
				defaultSteps={["vivid", "vivid-again"]}
			/>
		),
	},
	{
		id: "llm_ask",
		title: "LLM ask",
		blurb: "Spawn a haiku service and ask the model directly; needs --allow-llm-apis.",
		initial: ASK_INITIAL,
		render: (p) => <AskScenario {...p} />,
	},
];

const PANEL_KIND = "demo_helper/v1";

interface PanelState {
	readonly kind: typeof PANEL_KIND;
	readonly activeScenario: string | null;
	readonly scenarioState: unknown;
}
const PANEL_INITIAL: PanelState = { kind: PANEL_KIND, activeScenario: null, scenarioState: null };
function isPanel(s: PanelState): boolean {
	return s?.kind === PANEL_KIND;
}

export const DemoHelperFullPanel: DispatchedComponent<ServiceRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const panelAtom = useMemo(
		() => atomWithDefault<PanelState>(ownStorage as FxtrSessionAtom<PanelState | null>, PANEL_INITIAL, isPanel),
		[ownStorage],
	);
	const panel = useAtomValue(panelAtom);
	const setPanel = useSetAtom(panelAtom);
	const scenarioStateAtom = useMemo(
		() => subAtom(panelAtom, "scenarioState") as unknown as FxtrSessionAtom<unknown>,
		[panelAtom],
	);

	const active = SCENARIOS.find((s) => s.id === panel.activeScenario) ?? null;

	return (
		<div className="fw-panel__body" style={rootStyle}>
			<h2 style={headerStyle}>demo driver · {stub.shortId}</h2>
			{active === null ? (
				<>
					<div style={mutedStyle}>
						Pick a scenario to spawn services and drive them — a stand-in for asking an agent.
						Results show as links you can open in their own panels.
					</div>
					<div style={pickerStyle}>
						{SCENARIOS.map((s) => (
							<button
								key={s.id}
								type="button"
								style={pickerItemStyle}
								onClick={() =>
									setPanel((p) => ({ ...p, activeScenario: s.id, scenarioState: s.initial }))
								}
							>
								<div style={pickerTitleStyle}>{s.title}</div>
								<div style={mutedStyle}>{s.blurb}</div>
							</button>
						))}
					</div>
				</>
			) : (
				<>
					<div style={pickerTitleStyle}>{active.title}</div>
					{active.render({
						stub: stub as ServiceRefStub<unknown>,
						storage: scenarioStateAtom,
						onBack: () =>
							setPanel((p) => ({ ...p, activeScenario: null, scenarioState: null })),
					})}
				</>
			)}
		</div>
	);
};

// ----- link / hover slots (static pills — a service has no payload) -----

export const DemoHelperBlockLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({ stub, linkText }) => (
	<span style={linkPillStyle}>
		<span style={linkTagStyle}>demo-driver</span>
		<span>{linkText ?? `service · ${stub.shortId}`}</span>
	</span>
);

export const DemoHelperInlineLink: DispatchedComponent<ServiceRefStub, LinkProps> = ({ stub, linkText }) => (
	<code style={inlineLinkStyle}>demo-driver:{linkText ?? stub.shortId}</code>
);

export const DemoHelperHoverPreview: DispatchedComponent<ServiceRefStub, HoverPreviewProps> = ({ stub }) => (
	<div style={hoverCardStyle}>
		<div style={{ fontWeight: 600 }}>demo driver service</div>
		<div style={mutedStyle}>
			<code>{stub.shortId}</code> — open the panel to spawn + drive demo services.
		</div>
	</div>
);

// ===================================================================
// styles (theme-aware CSS vars, with fallbacks)
// ===================================================================

const rootStyle: CSSProperties = {
	padding: "var(--sp-5, 16px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-4, 16px)",
	height: "100%",
};
const headerStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};
const mutedStyle: CSSProperties = { color: "var(--text-muted)", fontSize: "var(--text-body-sm)" };
const pickerStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--sp-2, 8px)" };
const pickerItemStyle: CSSProperties = {
	textAlign: "left",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
	padding: "var(--sp-2) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card, 4px)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	cursor: "pointer",
};
const pickerTitleStyle: CSSProperties = { fontWeight: 600, color: "var(--text-primary)" };
const scenarioStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--sp-3, 12px)" };
const toolbarStyle: CSSProperties = { display: "flex", gap: "var(--sp-2, 8px)" };
const stepStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	paddingBottom: "var(--sp-2, 8px)",
	borderBottom: "1px solid var(--border-subtle)",
};
const stepLabelStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
};
const stepNumStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	justifyContent: "center",
	width: 20,
	height: 20,
	borderRadius: "50%",
	background: "var(--surface-sunken)",
	fontSize: "var(--text-caption)",
};
const stepBodyStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--sp-2, 8px)" };
const inputRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
	flexWrap: "wrap",
};
const historyStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--sp-2, 8px)" };
const checkboxLabelStyle: CSSProperties = { display: "flex", alignItems: "center", gap: "var(--sp-2, 6px)" };
const textInputStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-2)",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input, 4px)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	minWidth: "10ch",
};
const buttonStyle: CSSProperties = {
	padding: "var(--sp-1) var(--sp-3)",
	fontSize: "var(--text-body-sm)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-input, 4px)",
	background: "var(--surface-raised)",
	color: "var(--text-primary)",
	cursor: "pointer",
};
const cardStyle: CSSProperties = {
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-card, 4px)",
	padding: "var(--sp-2) var(--sp-3)",
	background: "var(--surface-sunken)",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
};
const cardLabelStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	fontFamily: "var(--font-mono)",
};
const dictStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: "var(--sp-1, 4px)" };
const dictRowStyle: CSSProperties = { display: "flex", alignItems: "center", gap: "var(--sp-2, 8px)" };
const dictKeyStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	fontFamily: "var(--font-mono)",
	minWidth: "8ch",
};
const scalarStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
	margin: 0,
};
const errorStyle: CSSProperties = { color: "var(--status-error, #c00)", fontSize: "var(--text-body-sm)" };
const linkPillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "var(--sp-2, 6px)",
	padding: "var(--sp-1) var(--sp-3)",
	border: "1px solid var(--border-subtle)",
	borderRadius: "var(--radius-badge, 6px)",
	background: "var(--surface-raised)",
	fontSize: "var(--text-body-sm)",
};
const linkTagStyle: CSSProperties = {
	textTransform: "uppercase",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	letterSpacing: "0.05em",
};
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "var(--surface-sunken)",
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
};
const hoverCardStyle: CSSProperties = {
	minWidth: 200,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
};

export default {
	demo_helper: {
		[FullPanel.uuid]: DemoHelperFullPanel,
		[BlockLink.uuid]: DemoHelperBlockLink,
		[InlineLink.uuid]: DemoHelperInlineLink,
		[HoverPreview.uuid]: DemoHelperHoverPreview,
	},
};
