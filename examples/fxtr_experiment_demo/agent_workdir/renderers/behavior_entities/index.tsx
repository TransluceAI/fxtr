/**
 * Renderers for every `behavior_research_types` entity (from
 * `packages/behavior_research_types/src/behavior_research_types/entities.py`)
 * — one module, one subtype per entity type (see `fxtr_renderer.nix`):
 * transcripts as chat bubbles, configs as labeled cards, judge results
 * as score badges, the analysis table as an HTML table, plus compact
 * link pills + hover previews throughout, so every value the behavior
 * workflow produces opens as something readable.
 *
 * Imports are restricted to the curated externals list.
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
	RefLink,
} from "@fxtr-ui/workstation-dispatch";
import { atom, useAtomValue } from "jotai";
import { type CSSProperties, type ReactNode, useMemo } from "react";

// ===================================================================
// Wire shapes (the entities' JSON forms)
// ===================================================================

interface ChatMessageW {
	readonly role: string;
	readonly text: string;
}
interface TranscriptW {
	readonly messages: ReadonlyArray<ChatMessageW>;
	readonly subject_config: EntityRefStub;
	readonly sim_config: EntityRefStub;
	readonly rollout_config: EntityRefStub;
}
interface SubjectModelConfigW {
	readonly provider: string;
	readonly model: string;
	readonly system_prompt: string;
}
interface UserSimConfigW {
	readonly task_prompt: string;
	readonly persona: string;
}
interface JudgeConfigW {
	readonly provider: string;
	readonly model: string;
	readonly rubric: string;
}
interface RolloutConfigW {
	readonly max_turns: number;
}
interface SweepConfigW {
	readonly replicas: number;
}
interface JudgeResultW {
	readonly score: number;
	readonly reasoning: string;
	readonly judge_config: EntityRefStub;
	readonly transcript: EntityRefStub;
}
interface AnalysisTableW {
	readonly columns: ReadonlyArray<string>;
	readonly rows: ReadonlyArray<ReadonlyArray<string | number>>;
	readonly row_sources: ReadonlyArray<ReadonlyArray<EntityRefStub>>;
}
interface ServiceKeyW {
	readonly label: string;
	readonly detail: string;
}
interface ImprovementRequestW {
	readonly instruction: string;
	readonly improver_provider: string;
	readonly improver_model: string;
	readonly sim_config: EntityRefStub;
	readonly subject_config: EntityRefStub;
	readonly judge_config: EntityRefStub;
	readonly rollout_config: EntityRefStub;
	readonly rollouts: ReadonlyArray<EntityRefStub>;
	readonly judgements: ReadonlyArray<EntityRefStub>;
}
interface ImprovementEventW {
	readonly kind: string;
	readonly text: string;
	readonly subject: EntityRefStub | null;
}
interface ImprovementResultW {
	readonly request: EntityRefStub;
	readonly events: ReadonlyArray<EntityRefStub>;
	readonly final_bio: EntityRefStub;
	readonly final_notes: string;
}

// ===================================================================
// Shared load + shell helpers
// ===================================================================

type Loaded<T> =
	| { readonly kind: "loading" }
	| { readonly kind: "errored"; readonly message: string }
	| { readonly kind: "filled"; readonly value: T };

/**
 * Load the dispatched entity via the dispatch entry's shared slot.
 *
 * ONE load per dispatch entry: the shared atom is per-(stub, dispatch
 * key), so this hook must only be used for the dispatched component's
 * OWN stub. Loading further entities from a panel (e.g. the
 * improvement result's events) needs distinct slots carved from
 * `ownStorage` — see `EventLogRow`.
 */
function useEntity<T>(stub: EntityRefStub): Loaded<T> {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<T> | null>(null);
	const resultAtom = useMemo(() => makeLoadResultAtom<T>(sharedAtom, stub), [sharedAtom, stub]);
	return useAtomValue(resultAtom) as Loaded<T>;
}

/** Project a possibly-missing record slot to the `null` default
 * `makeLoadResultAtom` expects (subAtom reads absent keys as
 * `undefined`; writes pass through). */
function withNullDefault<T>(base: FxtrSessionAtom<T | undefined>): FxtrSessionAtom<T | null> {
	return atom(
		(get) => get(base) ?? null,
		(_get, set, update: unknown, options?: unknown) =>
			(set as (a: unknown, u: unknown, o: unknown) => void)(base, update, options),
	) as FxtrSessionAtom<T | null>;
}

/** Panel shell: handles loading/error states, then renders the value. */
function panelFor<T>(render: (value: T) => ReactNode): DispatchedComponent<
	EntityRefStub,
	FullPanelProps
> {
	return ({ stub }) => {
		const result = useEntity<T>(stub);
		return (
			<div style={panelStyle}>
				{result.kind === "errored" ? (
					<span style={errorStyle}>load failed: {result.message}</span>
				) : result.kind === "loading" ? (
					<span style={mutedStyle}>loading…</span>
				) : (
					render(result.value)
				)}
			</div>
		);
	};
}

/** Link pill: a fixed tag + a loaded label. */
function linksFor<T>(
	tag: string,
	labelOf: (value: T) => string,
): {
	readonly block: DispatchedComponent<EntityRefStub, LinkProps>;
	readonly inline: DispatchedComponent<EntityRefStub, LinkProps>;
} {
	const block: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub, linkText }) => {
		const result = useEntity<T>(stub);
		const label = linkText ?? (result.kind === "filled" ? labelOf(result.value) : "…");
		return (
			<span style={pillStyle}>
				<span style={pillTagStyle}>{tag}</span>
				<span style={pillLabelStyle}>{label}</span>
			</span>
		);
	};
	const inline: DispatchedComponent<EntityRefStub, LinkProps> = ({ stub, linkText }) => {
		const result = useEntity<T>(stub);
		const label = linkText ?? (result.kind === "filled" ? labelOf(result.value) : "…");
		return (
			<code style={inlinePillStyle}>
				{tag}:{label}
			</code>
		);
	};
	return { block, inline };
}

/** Trim to a label-sized snippet (single line, ellipsized). */
function snip(text: string, max = 60): string {
	const flat = text.replace(/\s+/g, " ").trim();
	return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

/** Hover preview: a titled card with a body snippet. */
function hoverFor<T>(
	titleOf: (value: T) => string,
	bodyOf: (value: T) => string,
): DispatchedComponent<EntityRefStub, HoverPreviewProps> {
	return ({ stub }) => {
		const result = useEntity<T>(stub);
		if (result.kind !== "filled") {
			return (
				<div style={hoverCardStyle}>
					{result.kind === "errored" ? (
						<span style={errorStyle}>load failed: {result.message}</span>
					) : (
						<span style={mutedStyle}>loading…</span>
					)}
				</div>
			);
		}
		const body = bodyOf(result.value);
		const snippet = body.length > 400 ? `${body.slice(0, 400)}…` : body;
		return (
			<div style={hoverCardStyle}>
				<div style={hoverTitleStyle}>{titleOf(result.value)}</div>
				{snippet && <div style={hoverBodyStyle}>{snippet}</div>}
			</div>
		);
	};
}

/** The standard four-key entry from panel + links + hover. */
function entryFor<T>(spec: {
	readonly tag: string;
	readonly label: (value: T) => string;
	readonly hoverBody: (value: T) => string;
	readonly panel: (value: T) => ReactNode;
}) {
	const { block, inline } = linksFor<T>(spec.tag, spec.label);
	return {
		[FullPanel.uuid]: panelFor<T>(spec.panel),
		[BlockLink.uuid]: block,
		[InlineLink.uuid]: inline,
		[HoverPreview.uuid]: hoverFor<T>(spec.label, spec.hoverBody),
	};
}

/** The provenance footer: labeled RefLinks back to the entity's inputs. */
function DerivedFrom({
	items,
}: {
	items: ReadonlyArray<readonly [string, EntityRefStub]>;
}) {
	return (
		<div style={derivedFromStyle}>
			<div style={fieldNameStyle}>derived from</div>
			{items.map(([label, stub]) => (
				<div key={label} style={derivedRowStyle}>
					<span style={derivedLabelStyle}>{label}</span>
					<RefLink stub={stub} as="inline" />
				</div>
			))}
		</div>
	);
}

/** A labeled field row inside a config-card panel. */
function Field({ name, value, mono }: { name: string; value: string; mono?: boolean }) {
	return (
		<div style={fieldStyle}>
			<div style={fieldNameStyle}>{name}</div>
			<div style={mono ? fieldValueMonoStyle : fieldValueStyle}>{value}</div>
		</div>
	);
}

function scoreColor(score: number): string {
	// Low = cool gray-blue, high = saturated violet; the scale is
	// "how present is the rubric's behavior", not good/bad.
	const clamped = Math.min(1, Math.max(0, score));
	return `hsl(${262 - clamped * 30}, ${20 + clamped * 55}%, ${88 - clamped * 38}%)`;
}

function ScoreBadge({ score }: { score: number }) {
	return (
		<span
			style={{
				...scoreBadgeStyle,
				background: scoreColor(score),
				color: score > 0.55 ? "#fff" : "#333",
			}}
		>
			{score.toFixed(2)}
		</span>
	);
}

// ===================================================================
// Per-type entries
// ===================================================================

const transcriptEntry = entryFor<TranscriptW>({
	tag: "transcript",
	label: (t) =>
		t.messages.length
			? `${t.messages.length} msgs · “${snip(t.messages[0].text, 48)}”`
			: "0 msgs",
	hoverBody: (t) =>
		t.messages.length
			? t.messages
					.slice(0, 3)
					.map((m) => `${m.role}: ${snip(m.text, 110)}`)
					.join("\n") + (t.messages.length > 3 ? `\n… ${t.messages.length - 3} more` : "")
			: "(empty)",
	panel: (t) => (
		<div style={chatColumnStyle}>
			<h2 style={titleStyle}>transcript</h2>
			{t.messages.map((m, i) => (
				<div
					key={i}
					style={m.role === "assistant" ? bubbleRowAssistantStyle : bubbleRowUserStyle}
				>
					<div style={m.role === "assistant" ? bubbleAssistantStyle : bubbleUserStyle}>
						<div style={bubbleRoleStyle}>{m.role}</div>
						<div style={bubbleTextStyle}>{m.text}</div>
					</div>
				</div>
			))}
			{t.messages.length === 0 && <div style={mutedStyle}>(empty transcript)</div>}
			<DerivedFrom
				items={[
					["subject model", t.subject_config],
					["user sim", t.sim_config],
					["rollout cfg", t.rollout_config],
				]}
			/>
		</div>
	),
});

const subjectModelEntry = entryFor<SubjectModelConfigW>({
	tag: "subject model",
	label: (c) => `${c.model} · “${snip(c.system_prompt, 44)}”`,
	hoverBody: (c) => `${c.provider}/${c.model}\n${c.system_prompt}`,
	panel: (c) => (
		<>
			<h2 style={titleStyle}>subject model config</h2>
			<Field name="provider" value={c.provider} mono />
			<Field name="model" value={c.model} mono />
			<Field name="system prompt" value={c.system_prompt} />
		</>
	),
});

const userSimEntry = entryFor<UserSimConfigW>({
	tag: "user sim",
	label: (c) => `“${snip(c.persona, 52)}”`,
	hoverBody: (c) => c.persona,
	panel: (c) => (
		<>
			<h2 style={titleStyle}>user simulator config</h2>
			<Field name="persona" value={c.persona} />
			<Field name="task prompt" value={c.task_prompt} />
		</>
	),
});

const judgeConfigEntry = entryFor<JudgeConfigW>({
	tag: "judge",
	label: (c) => `${c.model} · “${snip(c.rubric, 44)}”`,
	hoverBody: (c) => `${c.provider}/${c.model}\n${c.rubric}`,
	panel: (c) => (
		<>
			<h2 style={titleStyle}>judge config</h2>
			<Field name="provider" value={c.provider} mono />
			<Field name="model" value={c.model} mono />
			<Field name="rubric" value={c.rubric} />
		</>
	),
});

const rolloutConfigEntry = entryFor<RolloutConfigW>({
	tag: "rollout cfg",
	label: (c) => `${c.max_turns} turns`,
	hoverBody: (c) => `max_turns = ${c.max_turns} (user/assistant exchanges per rollout)`,
	panel: (c) => (
		<>
			<h2 style={titleStyle}>rollout config</h2>
			<Field name="max turns" value={String(c.max_turns)} mono />
		</>
	),
});

const sweepConfigEntry = entryFor<SweepConfigW>({
	tag: "sweep cfg",
	label: (c) => `${c.replicas} replica${c.replicas === 1 ? "" : "s"}`,
	hoverBody: (c) => `replicas = ${c.replicas} (rollouts per subject x sim, spanned by the analysis)`,
	panel: (c) => (
		<>
			<h2 style={titleStyle}>sweep config</h2>
			<Field name="replicas" value={String(c.replicas)} mono />
		</>
	),
});

const judgeResultEntry = (() => {
	const { block, inline } = linksFor<JudgeResultW>(
		"verdict",
		(r) => `${r.score.toFixed(2)} · “${snip(r.reasoning, 48)}”`,
	);
	return {
		[FullPanel.uuid]: panelFor<JudgeResultW>((r) => (
			<>
				<h2 style={titleStyle}>
					judge result <ScoreBadge score={r.score} />
				</h2>
				<p style={reasoningStyle}>{r.reasoning}</p>
				<DerivedFrom
					items={[
						["judged transcript", r.transcript],
						["rubric", r.judge_config],
					]}
				/>
			</>
		)),
		[BlockLink.uuid]: block,
		[InlineLink.uuid]: inline,
		[HoverPreview.uuid]: hoverFor<JudgeResultW>(
			(r) => `score ${r.score.toFixed(2)}`,
			(r) => r.reasoning,
		),
	};
})();

const analysisTableEntry = entryFor<AnalysisTableW>({
	tag: "analysis",
	label: (t) => `${t.rows.length} rows · ${t.columns.join(", ")}`,
	hoverBody: (t) =>
		t.rows
			.slice(0, 6)
			.map((row) => row.map((c) => (typeof c === "number" ? snip(String(c), 8) : c)).join(" · "))
			.join("\n") + (t.rows.length > 6 ? `\n… ${t.rows.length - 6} more rows` : ""),
	panel: (t) => (
		<>
			<h2 style={titleStyle}>analysis table</h2>
			<table style={tableStyle}>
				<thead>
					<tr>
						{t.columns.map((c) => (
							<th key={c} style={thStyle}>
								{c}
							</th>
						))}
						<th style={thStyle}>sources</th>
					</tr>
				</thead>
				<tbody>
					{t.rows.map((row, i) => (
						<tr key={i}>
							{row.map((cell, j) => (
								<td key={j} style={typeof cell === "number" ? tdNumStyle : tdStyle}>
									{typeof cell === "number" && t.columns[j] === "mean_score" ? (
										<ScoreBadge score={cell} />
									) : typeof cell === "number" ? (
										Number.isInteger(cell) ? (
											cell
										) : (
											cell.toFixed(3)
										)
									) : (
										cell
									)}
								</td>
							))}
							<td style={tdStyle}>
								{/* Numbered per-verdict links (hover for the
								    reasoning; click to trace the aggregate back
								    to its data points). */}
								<span style={sourceLinksStyle}>
									{(t.row_sources[i] ?? []).map((source, k) => (
										<RefLink
											key={source.shortId}
											stub={source}
											as="inline"
											linkText={String(k + 1)}
										/>
									))}
								</span>
							</td>
						</tr>
					))}
				</tbody>
			</table>
			{t.rows.length === 0 && <div style={mutedStyle}>(no rows)</div>}
		</>
	),
});

/** A numbered row of inline RefLinks (hover for details, click to open). */
function NumberedRefs({ label, stubs }: { label: string; stubs: ReadonlyArray<EntityRefStub> }) {
	return (
		<div style={derivedRowStyle}>
			<span style={derivedLabelStyle}>{label}</span>
			<span style={sourceLinksStyle}>
				{stubs.map((stub, i) => (
					<RefLink key={stub.shortId} stub={stub} as="inline" linkText={String(i + 1)} />
				))}
				{stubs.length === 0 && <span style={mutedStyle}>(none)</span>}
			</span>
		</div>
	);
}

const improvementRequestEntry = entryFor<ImprovementRequestW>({
	tag: "improve request",
	label: (r) => `“${snip(r.instruction, 52)}”`,
	hoverBody: (r) =>
		`${r.instruction}\n${r.rollouts.length} source trials · improver ${r.improver_provider}/${r.improver_model}`,
	panel: (r) => (
		<>
			<h2 style={titleStyle}>user-sim improvement request</h2>
			<Field name="instruction" value={r.instruction} />
			<Field name="improver" value={`${r.improver_provider}/${r.improver_model}`} mono />
			<div style={derivedFromStyle}>
				<div style={fieldNameStyle}>pinned inputs (snapshot — detached from live configs)</div>
				<div style={derivedRowStyle}>
					<span style={derivedLabelStyle}>user sim</span>
					<RefLink stub={r.sim_config} as="inline" />
				</div>
				<div style={derivedRowStyle}>
					<span style={derivedLabelStyle}>subject model</span>
					<RefLink stub={r.subject_config} as="inline" />
				</div>
				<div style={derivedRowStyle}>
					<span style={derivedLabelStyle}>judge</span>
					<RefLink stub={r.judge_config} as="inline" />
				</div>
				<div style={derivedRowStyle}>
					<span style={derivedLabelStyle}>rollout cfg</span>
					<RefLink stub={r.rollout_config} as="inline" />
				</div>
				<NumberedRefs label="source transcripts" stubs={r.rollouts} />
				<NumberedRefs label="source judgements" stubs={r.judgements} />
			</div>
		</>
	),
});

/** kind → the log entry's actor label + chip color family. */
function eventStyleFor(kind: string): { actor: string; chip: CSSProperties } {
	if (kind === "request") return { actor: "researcher", chip: eventChipRequestStyle };
	if (kind === "proposal" || kind === "final_choice")
		return { actor: "improver", chip: eventChipImproverStyle };
	return { actor: "trial run", chip: eventChipTrialStyle };
}

function eventKindLabel(kind: string): string {
	return kind.replace(/_/g, " ");
}

/** The result panel's persisted state: its own load slot plus one
 * per-event slot (keyed by the event stub's short id) — the dispatch
 * SHARED slot can't be reused here, it's one-per-dispatch-entry and
 * already holds the result's own load. */
interface ResultPanelState {
	readonly self: OpResultStub<ImprovementResultW> | null;
	readonly events: Readonly<Record<string, OpResultStub<ImprovementEventW> | null>>;
}

const RESULT_PANEL_DEFAULT: ResultPanelState = { self: null, events: {} };

function isResultPanelState(v: ResultPanelState): boolean {
	return v != null && typeof v === "object" && "events" in v;
}

/** One log entry (loads its own event stub — `Child` fields cross as
 * ids — through a per-event slot carved from the panel's storage). */
function EventLogRow({
	stub,
	eventsAtom,
}: {
	readonly stub: EntityRefStub;
	readonly eventsAtom: FxtrSessionAtom<ResultPanelState["events"]>;
}) {
	const resultAtom = useMemo(
		() =>
			makeLoadResultAtom<ImprovementEventW>(
				withNullDefault(subAtom(eventsAtom, stub.shortId)),
				stub,
			),
		[eventsAtom, stub],
	);
	const result = useAtomValue(resultAtom) as Loaded<ImprovementEventW>;
	if (result.kind !== "filled") {
		return (
			<div style={eventCardStyle}>
				{result.kind === "errored" ? (
					<span style={errorStyle}>event load failed: {result.message}</span>
				) : (
					<span style={mutedStyle}>loading event…</span>
				)}
			</div>
		);
	}
	const event = result.value;
	const { actor, chip } = eventStyleFor(event.kind);
	return (
		<div style={eventCardStyle}>
			<div style={eventHeaderStyle}>
				<span style={chip}>{eventKindLabel(event.kind)}</span>
				<span style={eventActorStyle}>{actor}</span>
			</div>
			{event.text && <div style={eventTextStyle}>{event.text}</div>}
			{event.subject !== null && (
				<div style={eventSubjectStyle}>
					<RefLink stub={event.subject} as="inline" />
				</div>
			)}
		</div>
	);
}

const improvementEventEntry = entryFor<ImprovementEventW>({
	tag: "event",
	label: (e) => `${eventKindLabel(e.kind)}${e.text ? ` · “${snip(e.text, 44)}”` : ""}`,
	hoverBody: (e) => e.text || `(see the ${eventKindLabel(e.kind)}'s subject)`,
	panel: (e) => (
		<>
			<h2 style={titleStyle}>
				improvement event <span style={eventStyleFor(e.kind).chip}>{eventKindLabel(e.kind)}</span>
			</h2>
			{e.text && <Field name="text" value={e.text} />}
			{e.subject !== null && (
				<div style={derivedRowStyle}>
					<span style={derivedLabelStyle}>subject</span>
					<RefLink stub={e.subject} as="inline" />
				</div>
			)}
		</>
	),
});

/** Custom panel (not `panelFor`): the event log needs per-event load
 * slots carved from `ownStorage` beside the panel's own load. */
const ImprovementResultFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const rootAtom = useMemo(
		() =>
			atomWithDefault<ResultPanelState>(
				ownStorage as FxtrSessionAtom<ResultPanelState | null>,
				RESULT_PANEL_DEFAULT,
				isResultPanelState,
			),
		[ownStorage],
	);
	const eventsAtom = useMemo(() => subAtom(rootAtom, "events"), [rootAtom]);
	const resultAtom = useMemo(
		() => makeLoadResultAtom<ImprovementResultW>(subAtom(rootAtom, "self"), stub),
		[rootAtom, stub],
	);
	const result = useAtomValue(resultAtom);
	return (
		<div style={panelStyle}>
			{result.kind === "errored" ? (
				<span style={errorStyle}>load failed: {result.message}</span>
			) : result.kind === "loading" ? (
				<span style={mutedStyle}>loading…</span>
			) : (
				<>
					<h2 style={titleStyle}>user-sim improvement result</h2>
					<div style={derivedRowStyle}>
						<span style={derivedLabelStyle}>final bio</span>
						<RefLink stub={result.value.final_bio} as="inline" />
					</div>
					<div style={derivedRowStyle}>
						<span style={derivedLabelStyle}>request</span>
						<RefLink stub={result.value.request} as="inline" />
					</div>
					<div style={eventLogStyle}>
						{result.value.events.map((eventStub) => (
							<EventLogRow key={eventStub.shortId} stub={eventStub} eventsAtom={eventsAtom} />
						))}
					</div>
				</>
			)}
		</div>
	);
};

const improvementResultEntry = (() => {
	const { block, inline } = linksFor<ImprovementResultW>(
		"improved sim",
		(r) => `“${snip(r.final_notes, 52)}”`,
	);
	return {
		[FullPanel.uuid]: ImprovementResultFullPanel,
		[BlockLink.uuid]: block,
		[InlineLink.uuid]: inline,
		[HoverPreview.uuid]: hoverFor<ImprovementResultW>(
			(r) => `“${snip(r.final_notes, 52)}”`,
			(r) => `${r.final_notes}\n(${r.events.length} log events)`,
		),
	};
})();

const serviceKeyEntry = entryFor<ServiceKeyW>({
	tag: "key",
	label: (k) => k.label,
	hoverBody: (k) => k.detail,
	panel: (k) => (
		<>
			<h2 style={titleStyle}>{k.label}</h2>
			<p style={reasoningStyle}>{k.detail}</p>
			<p style={mutedStyle}>
				A content-addressed handle key: the id is the identity the workflow observes; binding a
				different service means writing a different key.
			</p>
		</>
	),
});

// ===================================================================
// Styles
// ===================================================================

const panelStyle: CSSProperties = {
	padding: 16,
	height: "100%",
	overflow: "auto",
	color: "var(--fxtr-fg, #1a2b3c)",
	fontSize: 13,
};
const titleStyle: CSSProperties = { margin: "0 0 10px 0", fontSize: 16 };
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12 };

const pillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #b8a6d9)",
	background: "var(--fxtr-bg-raised, #f7f3fc)",
	color: "#4a3670",
	fontSize: 12,
};
const pillTagStyle: CSSProperties = { fontWeight: 600 };
const pillLabelStyle: CSSProperties = { fontSize: 11 };
const inlinePillStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#f7f3fc",
	color: "#4a3670",
	fontSize: 12,
};
const hoverCardStyle: CSSProperties = {
	padding: 8,
	border: "1px solid var(--fxtr-border, #b8a6d9)",
	borderRadius: 6,
	background: "#f7f3fc",
	color: "#4a3670",
	fontSize: 12,
	minWidth: 180,
	maxWidth: 320,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600 };
const hoverBodyStyle: CSSProperties = {
	fontSize: 11,
	marginTop: 4,
	opacity: 0.85,
	whiteSpace: "pre-wrap",
};

const fieldStyle: CSSProperties = { marginTop: 8 };
const fieldNameStyle: CSSProperties = { fontSize: 11, opacity: 0.65, fontWeight: 600 };
const derivedFromStyle: CSSProperties = {
	marginTop: 14,
	paddingTop: 8,
	borderTop: "1px dashed var(--fxtr-border, #ccd5df)",
};
const derivedRowStyle: CSSProperties = {
	display: "flex",
	gap: 8,
	alignItems: "baseline",
	marginTop: 4,
};
const derivedLabelStyle: CSSProperties = { fontSize: 11, opacity: 0.65, minWidth: 110 };
const sourceLinksStyle: CSSProperties = {
	display: "inline-flex",
	flexWrap: "wrap",
	gap: 4,
};

const eventLogStyle: CSSProperties = {
	marginTop: 14,
	display: "flex",
	flexDirection: "column",
	gap: 8,
};
const eventCardStyle: CSSProperties = {
	padding: "6px 10px",
	borderRadius: 8,
	border: "1px solid var(--fxtr-border, #dde4ec)",
	background: "var(--fxtr-bg-raised, #fafbfd)",
};
const eventHeaderStyle: CSSProperties = {
	display: "flex",
	alignItems: "baseline",
	gap: 8,
};
const eventChipBaseStyle: CSSProperties = {
	padding: "1px 8px",
	borderRadius: 10,
	fontSize: 10,
	fontWeight: 700,
};
const eventChipRequestStyle: CSSProperties = {
	...eventChipBaseStyle,
	background: "#eef3f9",
	border: "1px solid #c8d6e8",
	color: "#2a4a6e",
};
const eventChipImproverStyle: CSSProperties = {
	...eventChipBaseStyle,
	background: "#f3eefc",
	border: "1px solid #d3c4ec",
	color: "#4a3670",
};
const eventChipTrialStyle: CSSProperties = {
	...eventChipBaseStyle,
	background: "#eef7f0",
	border: "1px solid #b3d6bd",
	color: "#28523a",
};
const eventActorStyle: CSSProperties = { fontSize: 10, opacity: 0.55 };
const eventTextStyle: CSSProperties = { whiteSpace: "pre-wrap", marginTop: 4, fontSize: 12 };
const eventSubjectStyle: CSSProperties = { marginTop: 4 };
const fieldValueStyle: CSSProperties = { whiteSpace: "pre-wrap", marginTop: 2 };
const fieldValueMonoStyle: CSSProperties = {
	fontFamily: "monospace",
	fontSize: 12,
	marginTop: 2,
};

const chatColumnStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 8 };
const bubbleRowUserStyle: CSSProperties = { display: "flex", justifyContent: "flex-start" };
const bubbleRowAssistantStyle: CSSProperties = { display: "flex", justifyContent: "flex-end" };
const bubbleBaseStyle: CSSProperties = {
	maxWidth: "78%",
	padding: "6px 10px",
	borderRadius: 10,
	fontSize: 13,
};
const bubbleUserStyle: CSSProperties = {
	...bubbleBaseStyle,
	background: "#eef3f9",
	border: "1px solid #c8d6e8",
};
const bubbleAssistantStyle: CSSProperties = {
	...bubbleBaseStyle,
	background: "#f3eefc",
	border: "1px solid #d3c4ec",
};
const bubbleRoleStyle: CSSProperties = { fontSize: 10, fontWeight: 700, opacity: 0.55 };
const bubbleTextStyle: CSSProperties = { whiteSpace: "pre-wrap", marginTop: 2 };

const scoreBadgeStyle: CSSProperties = {
	display: "inline-block",
	padding: "1px 8px",
	borderRadius: 10,
	fontSize: 12,
	fontWeight: 700,
	verticalAlign: "middle",
};
const reasoningStyle: CSSProperties = { whiteSpace: "pre-wrap", marginTop: 10 };

const chipRowStyle: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 6 };
const chipStyle: CSSProperties = {
	padding: "2px 8px",
	borderRadius: 10,
	background: "#eef3f9",
	border: "1px solid #c8d6e8",
	fontSize: 12,
};

const tableStyle: CSSProperties = { borderCollapse: "collapse", marginTop: 4 };
const thStyle: CSSProperties = {
	textAlign: "left",
	padding: "4px 10px",
	borderBottom: "2px solid var(--fxtr-border, #ccd5df)",
	fontSize: 11,
};
const tdStyle: CSSProperties = {
	padding: "4px 10px",
	borderBottom: "1px solid var(--fxtr-border, #e2e8ee)",
};
const tdNumStyle: CSSProperties = {
	padding: "4px 10px",
	borderBottom: "1px solid var(--fxtr-border, #e2e8ee)",
	textAlign: "right",
	fontVariantNumeric: "tabular-nums",
};

// ===================================================================
// Dispatch map (subtype names match fxtr_renderer.nix)
// ===================================================================

export default {
	transcript: transcriptEntry,
	subject_model_config: subjectModelEntry,
	user_sim_config: userSimEntry,
	judge_config: judgeConfigEntry,
	rollout_config: rolloutConfigEntry,
	sweep_config: sweepConfigEntry,
	judge_result: judgeResultEntry,
	analysis_table: analysisTableEntry,
	service_key: serviceKeyEntry,
	user_sim_improvement_request: improvementRequestEntry,
	improvement_event: improvementEventEntry,
	user_sim_improvement_result: improvementResultEntry,
};
