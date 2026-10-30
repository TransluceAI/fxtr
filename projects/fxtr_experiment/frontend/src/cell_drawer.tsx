/**
 * The cell drawer (below the cells table): one selected cell's content
 * and verdicts, per kind (`notes/monitoring_ui.md`). Everything
 * renders from the cell's watch mirror (`derive/folds.ts`): the
 * `CellReport` (content id WITH the currency verdict from one session
 * snapshot — never observable as if fresh), and for task cells the
 * generation report (observations with per-edge match flags, failures,
 * unanswered requests, checkpoint entries), the reverse observers, and
 * the **run-log stream** — the current run's entries (task-authored
 * `log()` lines + engine lifecycle notes), reset per run, live-appended
 * while a run executes. Targets/sourcing state is joined client-side
 * from the workflow mirror by the caller.
 *
 * Mutations: the affordance list is the pure `deriveCellActions`
 * (write grants only; the demand-affordance doctrine lives there),
 * each button firing a step chain through the panel's action log
 * (`actions.tsx`). `ViewSel`s are built from the grid watch's meta
 * grant (`viewSel(meta.grant.token, boundByPosition(coords))`) — no
 * token cache, no heal path. Destructive/adoption gestures go through
 * an inline confirm expansion — never a silent `adopt` or delete.
 * "Run" on a targeted cell demands by RECORD (`demand_target`, the
 * verbatim round-trip); "Target + run" chains `add_target` with the
 * sel-based demand (no record exists client-side until the targets
 * mirror catches up).
 */

import type { FxtrSessionAtom } from "@fxtr-ui/core";
import { RefLink } from "@fxtr-ui/workstation-dispatch";
import { type CSSProperties, type ReactNode, useMemo, useState } from "react";

import type { RunAction } from "./actions.js";
import { type CellAction, deriveCellActions } from "./derive/actions.js";
import type { CellLogMirror, CellMirror } from "./derive/folds.js";
import {
	type GenSubjectStatus,
	deriveObservationRows,
	deriveStepRows,
} from "./derive/generation.js";
import { DrawerFrameTable, type FrameLoads } from "./frame_table.js";
import type { ActionStep, WatchSlots } from "./panel_state.js";
import { boundByPosition, viewSel } from "./wire/helpers.js";
import type {
	CellInfo,
	CellReport,
	GenerationReport,
	GridKind,
	GridMeta,
	LogEntry,
	ObserverInfo,
	SourcingInfo,
	TargetInfo,
	ViewSel,
} from "./wire/types.js";

export function CellDrawer({
	meta,
	kind,
	internals,
	path,
	cell,
	coordsText,
	targeted,
	targetRecord,
	gridSourced,
	sourcing,
	cellEntry,
	hasInternals,
	internalsRevealed,
	onBrowseInternals,
	onClose,
	retryCell,
	runAction,
	frameLoads,
}: {
	/** The grid watch's meta bundle — the grant `ViewSel`s build from. */
	readonly meta: GridMeta;
	readonly kind: GridKind;
	/** The grid lives inside a task's internals subtree (no mutations). */
	readonly internals: boolean;
	readonly path: string;
	readonly cell: CellInfo;
	readonly coordsText: string;
	readonly targeted: boolean;
	/** This cell's target record from the workflow mirror (null when
	 * untargeted or the mirror hasn't caught up — run steps fall back
	 * to the sel-based demand, which requires the target impl-side). */
	readonly targetRecord: TargetInfo | null;
	/** The grid has a user sourcing record (targeting an unsourced
	 * grid prompts for explicit adoption). */
	readonly gridSourced: boolean;
	/** This grid's sourcing rows (already filtered to `path`); the
	 * drawer picks out this cell's records by coords text. */
	readonly sourcing: readonly SourcingInfo[];
	/** The cell watch slot (spec-matched here — a mismatched entry is
	 * a previous selection's). */
	readonly cellEntry: WatchSlots["cell"];
	readonly hasInternals: boolean;
	readonly internalsRevealed: boolean;
	readonly onBrowseInternals: () => void;
	readonly onClose: () => void;
	/** Drop a parked (errored) cell watch entry — the driver
	 * resubscribes. */
	readonly retryCell: () => void;
	readonly runAction: RunAction;
	/** The frame-entity load slots (`frame_table.tsx`). */
	readonly frameLoads: FxtrSessionAtom<FrameLoads>;
}): ReactNode {
	const isTask = kind === "task";

	// Subtree cells are namespaces, not values — nothing to inspect
	// (the panel's spec derivation subscribes no watch for them).
	const inspectable = kind === "data" || kind === "dataframe" || kind === "handle" || isTask;

	const sel = useMemo(
		() => viewSel(meta.grant.token, boundByPosition(cell.coords)),
		[meta.grant.token, cell.coords],
	);
	// Task reads require an output selection; any single output demands
	// the whole generation, so the first declared output stands in for
	// "run this cell". Non-task demands use the plain selection.
	const demandSel = useMemo(() => {
		if (kind !== "task") return sel;
		const outputs = Object.keys(meta.grant.outputs ?? {});
		return viewSel(meta.grant.token, boundByPosition(cell.coords), outputs[0] ?? null);
	}, [kind, sel, meta.grant.token, meta.grant.outputs, cell.coords]);

	const entry =
		cellEntry !== null && cellEntry.spec.path === path && cellEntry.spec.coords === coordsText
			? cellEntry
			: null;

	const cellSourcing = sourcing.filter((s) => s.coords === coordsText);

	return (
		<div style={drawerStyle}>
			<div style={drawerBarStyle}>
				<code style={coordsStyle}>{coordsText}</code>
				<span style={kindTagStyle}>{kind} cell</span>
				{targeted && (
					<span
						style={{ ...mutedInlineStyle, color: "#3a8a4a" }}
						title="a target record wants this cell"
					>
						● targeted
					</span>
				)}
				<span style={{ flex: 1 }} />
				<button type="button" style={buttonStyle} onClick={onClose}>
					close
				</button>
			</div>
			{!inspectable ? (
				<div style={mutedStyle}>
					a subtree cell is a namespace of child grids, not a value — its contents are the child
					grids in the tree
				</div>
			) : entry !== null && entry.phase === "errored" ? (
				<div style={errorStyle}>
					cell watch failed: {entry.message}{" "}
					<button type="button" style={buttonStyle} onClick={retryCell}>
						retry
					</button>
				</div>
			) : entry === null || !entry.mirror.snapshotted ? (
				<div style={mutedStyle}>inspecting…</div>
			) : entry.mirror.gone ? (
				<div style={mutedStyle}>this cell's grid no longer exists</div>
			) : (
				<DrawerBody
					mirror={entry.mirror}
					kind={kind}
					internals={internals}
					mode={meta.grant.mode}
					outputs={meta.grant.outputs}
					sel={sel}
					demandSel={demandSel}
					path={path}
					coordsText={coordsText}
					cell={cell}
					targeted={targeted}
					targetRecord={targetRecord}
					gridSourced={gridSourced}
					cellSourcing={cellSourcing}
					hasInternals={hasInternals}
					internalsRevealed={internalsRevealed}
					onBrowseInternals={onBrowseInternals}
					runAction={runAction}
					frameLoads={frameLoads}
				/>
			)}
		</div>
	);
}

function DrawerBody({
	mirror,
	kind,
	internals,
	mode,
	outputs,
	sel,
	demandSel,
	path,
	coordsText,
	cell,
	targeted,
	targetRecord,
	gridSourced,
	cellSourcing,
	hasInternals,
	internalsRevealed,
	onBrowseInternals,
	runAction,
	frameLoads,
}: {
	readonly mirror: CellMirror;
	readonly kind: GridKind;
	readonly internals: boolean;
	readonly mode: "read" | "write";
	/** The grant's declared outputs (task grids): frame outputs carry a
	 * `schema` field, which is what gates the inline frame table. */
	readonly outputs: GridMeta["grant"]["outputs"];
	readonly sel: ViewSel;
	/** `sel` with the demand channel's output selection (task cells). */
	readonly demandSel: ViewSel;
	readonly path: string;
	readonly coordsText: string;
	readonly cell: CellInfo;
	readonly targeted: boolean;
	readonly targetRecord: TargetInfo | null;
	readonly gridSourced: boolean;
	readonly cellSourcing: readonly SourcingInfo[];
	readonly hasInternals: boolean;
	readonly internalsRevealed: boolean;
	readonly onBrowseInternals: () => void;
	readonly runAction: RunAction;
	readonly frameLoads: FxtrSessionAtom<FrameLoads>;
}): ReactNode {
	const report = mirror.report;
	if (report === null) {
		// The snapshot batch always carries the report; a beat where it
		// hasn't landed yet reads as still-inspecting.
		return <div style={mutedStyle}>inspecting…</div>;
	}
	const status = report.status ?? cell.status;
	return (
		<div style={bodyStyle}>
			<VerdictLine report={report} fallback={cell} />
			<SourcingLine records={cellSourcing} />
			<ActionsRow
				actions={deriveCellActions({
					kind,
					mode,
					internals,
					status,
					currency: report.currency ?? cell.currency,
					targeted,
					gridSourced,
					hasCellSourcing: cellSourcing.length > 0,
					populated: kind === "task" ? status !== null : report.value !== null,
				})}
				running={kind === "task" && status === "running"}
				sel={sel}
				demandSel={demandSel}
				path={path}
				coordsText={coordsText}
				targetRecord={targetRecord}
				runAction={runAction}
			/>
			{kind === "handle" ? (
				<HandleContent report={report} />
			) : kind === "task" ? (
				<TaskContent
					report={report}
					mirror={mirror}
					outputs={outputs}
					frameLoads={frameLoads}
					hasInternals={hasInternals}
					internalsRevealed={internalsRevealed}
					onBrowseInternals={onBrowseInternals}
				/>
			) : (
				<ValueContent report={report} isFrame={kind === "dataframe"} frameLoads={frameLoads} />
			)}
		</div>
	);
}

/** The step chain behind one affordance. "Run"-shaped gestures prefer
 * the by-record demand (`demand_target`, verbatim round-trip) when a
 * record is joined; the sel-based demand covers the rest — including
 * "Target + run", where no record exists client-side yet. */
function stepsFor(
	action: CellAction,
	sel: ViewSel,
	demandSel: ViewSel,
	record: TargetInfo | null,
	adopt: boolean,
): readonly ActionStep[] {
	const demandStep: ActionStep =
		record !== null
			? { kind: "demand_target", record: record.target }
			: { kind: "demand", sel: demandSel };
	switch (action.id) {
		case "run":
		case "retry":
			return [demandStep];
		case "clear_rerun":
			return [{ kind: "clear_cells", sel }, demandStep];
		case "clear":
			return [{ kind: "clear_cells", sel }];
		case "target_run":
			return [
				{ kind: "add_target", sel, adopt },
				{ kind: "demand", sel: demandSel },
			];
		case "target":
			return [{ kind: "add_target", sel, adopt }];
		case "untarget":
			return [{ kind: "remove_target", sel }];
		case "delete_cell":
			return [{ kind: "delete_cell", sel }];
		case "source_cell":
			return [{ kind: "source_cell", sel }];
		case "revoke_cell_sourcing":
			return [{ kind: "revoke_cell_sourcing", sel }];
	}
}

/** The drawer's mutation buttons. Actions flagged `confirm` or
 * `needsAdopt` expand inline first — a destructive step or a
 * grid-adoption side effect is never one accidental click. */
function ActionsRow({
	actions,
	running,
	sel,
	demandSel,
	path,
	coordsText,
	targetRecord,
	runAction,
}: {
	readonly actions: readonly CellAction[];
	readonly running: boolean;
	readonly sel: ViewSel;
	readonly demandSel: ViewSel;
	readonly path: string;
	readonly coordsText: string;
	readonly targetRecord: TargetInfo | null;
	readonly runAction: RunAction;
}): ReactNode {
	const [expanded, setExpanded] = useState<CellAction | null>(null);
	if (running) {
		return <div style={mutedStyle}>running — actions return once the run settles</div>;
	}
	if (actions.length === 0) return null;
	const fire = (action: CellAction, adopt: boolean) => {
		setExpanded(null);
		runAction(
			`${action.label} ${path} @ ${coordsText}`,
			stepsFor(action, sel, demandSel, targetRecord, adopt),
		);
	};
	return (
		<div>
			<div style={lineStyle}>
				{actions.map((a) => (
					<button
						key={a.id}
						type="button"
						style={buttonStyle}
						onClick={() =>
							a.needsAdopt || a.confirm
								? setExpanded(expanded?.id === a.id ? null : a)
								: fire(a, false)
						}
					>
						{a.label}
						{a.needsAdopt || a.confirm ? "…" : ""}
					</button>
				))}
			</div>
			{expanded !== null && (
				<div style={confirmBoxStyle}>
					{expanded.needsAdopt ? (
						<>
							<span style={mutedInlineStyle}>
								this grid has no user sourcing record — targeting requires one (adoption is a
								grid-level ownership claim, so it is never added silently)
							</span>
							<button type="button" style={buttonStyle} onClick={() => fire(expanded, true)}>
								adopt grid + {expanded.label.toLowerCase()}
							</button>
						</>
					) : (
						<>
							<span style={mutedInlineStyle}>
								{expanded.id === "delete_cell"
									? "remove this cell's value (observers go stale; a sourcing record would go dangling)?"
									: "drop this cell's generation state (results, records, checkpoints)?"}
							</span>
							<button type="button" style={buttonStyle} onClick={() => fire(expanded, false)}>
								{expanded.label.toLowerCase()}
							</button>
						</>
					)}
					<button type="button" style={buttonStyle} onClick={() => setExpanded(null)}>
						cancel
					</button>
				</div>
			)}
		</div>
	);
}

/** One line of verdicts, all from the watch's report (the labeled
 * channel — the table row may be a staler snapshot, so the report
 * wins; the row only fills in while nothing is loaded yet). */
function VerdictLine({
	report,
	fallback,
}: {
	readonly report: CellReport;
	readonly fallback: CellInfo;
}): ReactNode {
	const status = report.status ?? fallback.status;
	const currency = report.currency ?? fallback.currency;
	const reason = report.reason ?? fallback.reason;
	return (
		<div style={lineStyle}>
			{status !== null && <span style={{ color: statusColor(status) }}>{status}</span>}
			{currency !== null && <span style={{ color: currencyColor(currency) }}>{currency}</span>}
			{reason !== null && reason !== "" && (
				// First line only (an errored cell's reason embeds the whole
				// traceback — the failures section is its real home).
				<span style={reasonInlineStyle} title={reason}>
					({(reason.split("\n", 1)[0] as string).slice(0, 200)})
				</span>
			)}
			<span style={{ color: groundingColor(report.grounding) }}>
				{report.grounding ?? "no grounding verdict"}
				{report.grounding_reason ? ` (${report.grounding_reason})` : ""}
			</span>
			{report.wanted !== null && (
				<span style={mutedInlineStyle} title={report.wanted_reason ?? undefined}>
					wanted: {report.wanted ? "✓" : "✗"}
				</span>
			)}
			{report.bound !== null && (
				<span
					style={{ color: report.bound ? "#3a8a4a" : "#c04040" }}
					title="whether a live connector binding holds this handle's key"
				>
					{report.bound ? "bound" : "unbound"}
				</span>
			)}
		</div>
	);
}

function SourcingLine({ records }: { readonly records: readonly SourcingInfo[] }): ReactNode {
	if (records.length === 0) return null;
	return (
		<div style={lineStyle}>
			{records.map((s, i) => (
				// biome-ignore lint/suspicious/noArrayIndexKey: one mirror value, order-stable
				<span key={i} style={mutedInlineStyle}>
					user-sourced at pinned value ({s.status}): <RefLink stub={s.fingerprint} as="inline" />
				</span>
			))}
		</div>
	);
}

function ValueContent({
	report,
	isFrame,
	frameLoads,
}: {
	readonly report: CellReport;
	/** Dataframe cells inline the frame table under the ref link. */
	readonly isFrame: boolean;
	readonly frameLoads: FxtrSessionAtom<FrameLoads>;
}): ReactNode {
	if (report.value === null) {
		return <div style={mutedStyle}>no value</div>;
	}
	return (
		<div style={sectionStyle}>
			<div style={sectionTitleStyle}>value</div>
			<RefLink stub={report.value} as="block" />
			{isFrame && <DrawerFrameTable stub={report.value} loadsAtom={frameLoads} />}
		</div>
	);
}

function HandleContent({ report }: { readonly report: CellReport }): ReactNode {
	if (report.value === null) {
		return <div style={mutedStyle}>no key</div>;
	}
	return (
		<div style={sectionStyle}>
			<div style={sectionTitleStyle}>handle key</div>
			<RefLink stub={report.value} as="block" />
		</div>
	);
}

function TaskContent({
	report,
	mirror,
	outputs,
	frameLoads,
	hasInternals,
	internalsRevealed,
	onBrowseInternals,
}: {
	readonly report: CellReport;
	readonly mirror: CellMirror;
	readonly outputs: GridMeta["grant"]["outputs"];
	readonly frameLoads: FxtrSessionAtom<FrameLoads>;
	readonly hasInternals: boolean;
	readonly internalsRevealed: boolean;
	readonly onBrowseInternals: () => void;
}): ReactNode {
	const results = Object.entries(report.results ?? {});
	// A declared FrameOutput's wire form carries a `schema` field (an
	// EntityOutput crosses as an empty object) — that gates the inline
	// frame table under the result's ref link.
	const isFrameOutput = (name: string): boolean => {
		const spec = outputs?.[name];
		return typeof spec === "object" && spec !== null && "schema" in spec;
	};
	// Section order: the task's outputs first, then the live run
	// narrative (run log, then the generation's steps/observations).
	return (
		<>
			<div style={sectionStyle}>
				<div style={sectionTitleStyle}>results</div>
				{results.length === 0 ? (
					<div style={mutedStyle}>no results yet</div>
				) : (
					results.map(([name, ref]) => (
						<div key={name}>
							<div style={lineStyle}>
								<span style={labelStyle}>{name}</span>
								<RefLink stub={ref} as="inline" />
							</div>
							{isFrameOutput(name) && <DrawerFrameTable stub={ref} loadsAtom={frameLoads} />}
						</div>
					))
				)}
			</div>
			{mirror.log !== null && <LogSection log={mirror.log} />}
			<GenerationSection gen={mirror.generation} log={mirror.log} />
			{hasInternals && (
				<div style={lineStyle}>
					<button type="button" style={buttonStyle} onClick={onBrowseInternals}>
						{internalsRevealed ? "hide internals in tree" : "browse internals…"}
					</button>
					<span style={mutedInlineStyle}>
						this task's internals grids (shown under its tree node)
					</span>
				</div>
			)}
			<ObserversSection observers={mirror.observers} />
		</>
	);
}

/** The run-log stream: the current (or last) run's entries — reset per
 * run, live-appended while it executes. Lifecycle notes render muted;
 * task-authored `log()` lines render full-strength with the invoking
 * step's identity when present. */
function LogSection({ log }: { readonly log: CellLogMirror }): ReactNode {
	return (
		<div style={sectionStyle}>
			<div style={sectionTitleStyle}>
				run log <code style={genIdStyle}>{log.generation}</code>
			</div>
			{log.entries.length === 0 ? (
				<div style={mutedStyle}>no entries yet</div>
			) : (
				<div style={logBoxStyle}>
					{log.entries.map((entry, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: append-only within a run
						<LogLine key={i} entry={entry} />
					))}
				</div>
			)}
		</div>
	);
}

function LogLine({ entry }: { readonly entry: LogEntry }): ReactNode {
	const authored = entry.entry_kind === "log";
	// One flowing text line (colored via inline spans, separated by plain
	// spaces) that wraps at the container width — no columns, so a long
	// step path can never squeeze the message.
	return (
		<div style={{ ...logLineStyle, opacity: authored ? 1 : 0.6 }}>
			<span style={logTsStyle} title={entry.ts}>
				{entry.ts.slice(11, 19)}
			</span>
			{!authored && <span style={logKindStyle}> {entry.entry_kind}</span>}
			{entry.step !== null && (
				<span style={logStepStyle}>
					{" "}
					{entry.step.grid_path}
					{entry.step.coords !== "" ? ` @ ${entry.step.coords}` : ""}
				</span>
			)}
			<span> {entry.message}</span>
		</div>
	);
}

function GenerationSection({
	gen,
	log,
}: {
	readonly gen: GenerationReport | null;
	readonly log: CellLogMirror | null;
}): ReactNode {
	// Steps merge the report's checkpoints (complete) with the log's
	// running/errored steps, so an in-progress run shows live progress.
	const stepRows = deriveStepRows(gen?.checkpoints ?? [], log);
	const obsRows = deriveObservationRows(gen?.observations ?? [], gen?.unanswered_requests ?? []);
	if (gen === null || gen.generation === null) {
		// No generation, but a run may still have started steps (the log)
		// before the generation report caught up — show them if so.
		if (stepRows.length === 0) {
			return (
				<div style={sectionStyle}>
					<div style={sectionTitleStyle}>generation</div>
					<div style={mutedStyle}>no generation yet</div>
				</div>
			);
		}
	}
	return (
		<div style={sectionStyle}>
			<div style={sectionTitleStyle}>
				generation {gen?.generation && <code style={genIdStyle}>{gen.generation}</code>}
				{gen?.status != null && (
					<span style={{ color: statusColor(gen.status), marginLeft: 6 }}>{gen.status}</span>
				)}
				{gen?.executed_fn != null && (
					<span style={{ ...mutedInlineStyle, marginLeft: 8 }}>
						fn: <RefLink stub={gen.executed_fn} as="inline" />
					</span>
				)}
			</div>
			{gen !== null && gen.failures.length > 0 && (
				<div style={subSectionStyle}>
					<div style={subTitleStyle}>failures</div>
					{gen.failures.map((f, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: report order is the display order
						<div key={i} style={failureStyle}>
							<span style={{ color: f.kind === "fault" ? "#b06010" : "#c04040" }}>{f.kind}</span>{" "}
							<b>{f.error_class}</b> <span style={mutedInlineStyle}>{f.ts}</span>
							<div style={failureMsgStyle}>{f.message}</div>
						</div>
					))}
				</div>
			)}
			{stepRows.length > 0 && (
				<div style={subSectionStyle}>
					<div style={subTitleStyle}>step checkpoints</div>
					<table style={tableStyle}>
						<thead>
							<tr>
								<th style={thStyle}>grid</th>
								<th style={thStyle}>coords</th>
								<th style={thStyle}>status</th>
								<th style={thStyle}>step</th>
								<th style={thStyle}>args</th>
								<th style={thStyle}>value</th>
							</tr>
						</thead>
						<tbody>
							{stepRows.map((s) => (
								<tr key={`${s.grid_path} ${s.coords}`}>
									<td style={tdStyle}>
										<code style={coordsStyle}>{s.grid_path}</code>
									</td>
									<td style={tdStyle}>
										<code style={coordsStyle}>{s.coords}</code>
									</td>
									<td
										style={{ ...tdStyle, color: statusColor(s.status) }}
										title={s.error ?? undefined}
									>
										{s.status}
									</td>
									<td style={tdStyle}>
										{s.checkpoint !== null ? (
											<RefLink stub={s.checkpoint.step_type_id} as="inline" />
										) : (
											<span style={mutedInlineStyle}>—</span>
										)}
									</td>
									<td style={tdStyle}>
										{s.checkpoint !== null ? (
											<RefLink stub={s.checkpoint.args_id} as="inline" />
										) : (
											<span style={mutedInlineStyle}>—</span>
										)}
									</td>
									<td style={tdStyle}>
										{s.checkpoint !== null ? (
											<RefLink stub={s.checkpoint.value_id} as="inline" />
										) : (
											<span style={mutedInlineStyle}>—</span>
										)}
									</td>
								</tr>
							))}
						</tbody>
					</table>
				</div>
			)}
			<div style={subSectionStyle}>
				<div style={subTitleStyle}>observations (why is this stale / fresh)</div>
				{obsRows.length === 0 ? (
					<div style={mutedStyle}>none recorded</div>
				) : (
					<table style={tableStyle}>
						<thead>
							<tr>
								<th style={thStyle}>subject</th>
								<th style={thStyle}>kind</th>
								<th style={thStyle}>recorded</th>
								<th style={thStyle}>now</th>
							</tr>
						</thead>
						<tbody>
							{obsRows.map((o, i) => (
								// biome-ignore lint/suspicious/noArrayIndexKey: one mirror value, order-stable
								<tr key={i}>
									<td style={tdStyle}>
										<code style={coordsStyle}>
											{o.grid_path}
											{o.coords !== "" ? ` @ ${o.coords}` : ""}
											{o.output !== "" ? ` · ${o.output}` : ""}
										</code>
									</td>
									<td style={tdStyle}>{o.kind}</td>
									<td style={tdStyle}>
										{o.fingerprint !== null ? (
											<RefLink stub={o.fingerprint} as="inline" />
										) : (
											<span style={mutedInlineStyle}>—</span>
										)}
									</td>
									<td style={{ ...tdStyle, color: genStatusColor(o.status) }}>{o.status}</td>
								</tr>
							))}
						</tbody>
					</table>
				)}
			</div>
		</div>
	);
}

function ObserversSection({
	observers,
}: {
	readonly observers: readonly ObserverInfo[];
}): ReactNode {
	return (
		<div style={sectionStyle}>
			<div style={sectionTitleStyle}>observers (who read this cell)</div>
			{observers.length === 0 ? (
				<div style={mutedStyle}>none</div>
			) : (
				observers.map((o, i) => (
					// biome-ignore lint/suspicious/noArrayIndexKey: one mirror value, order-stable
					<div key={i} style={lineStyle}>
						<code style={coordsStyle}>
							{o.grid_path}
							{o.coords !== "" ? ` @ ${o.coords}` : ""}
						</code>
						<span style={{ color: statusColor(o.status) }}>{o.status}</span>
						{o.currency !== null && (
							<span style={{ color: currencyColor(o.currency) }}>{o.currency}</span>
						)}
					</div>
				))
			)}
		</div>
	);
}

// ---- shared color scales (kept in sync with grid_view.tsx) ----

function statusColor(status: string | null): string | undefined {
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

function currencyColor(currency: string | null): string | undefined {
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

function groundingColor(verdict: string | null): string {
	switch (verdict) {
		case "orphaned":
			return "#c04040";
		case "unanchored":
			return "#c09a3a";
		default:
			return "#3a8a4a";
	}
}

function genStatusColor(status: GenSubjectStatus): string | undefined {
	switch (status) {
		case "match":
			return "#3a8a4a";
		case "changed":
			return "#907000";
		case "missing":
			return "#c04040";
		case "pending":
			return "#2266cc";
	}
}

// ---- styles ----

const drawerStyle: CSSProperties = {
	marginTop: 10,
	padding: "8px 10px",
	border: "1px solid var(--fxtr-border, #ccd5df)",
	borderRadius: 6,
	fontSize: 12,
};
const drawerBarStyle: CSSProperties = {
	display: "flex",
	gap: 10,
	alignItems: "baseline",
	marginBottom: 4,
};
const bodyStyle: CSSProperties = { display: "flex", flexDirection: "column", gap: 6 };
const coordsStyle: CSSProperties = { fontSize: 11 };
const kindTagStyle: CSSProperties = {
	fontSize: 10,
	padding: "1px 6px",
	borderRadius: 3,
	background: "var(--fxtr-bg-raised, #eef2f6)",
	opacity: 0.8,
};
const lineStyle: CSSProperties = {
	display: "flex",
	gap: 10,
	alignItems: "baseline",
	flexWrap: "wrap",
};
const labelStyle: CSSProperties = { fontSize: 11, opacity: 0.7, minWidth: 60 };
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12 };
const mutedInlineStyle: CSSProperties = { opacity: 0.65, fontSize: 11 };
const reasonInlineStyle: CSSProperties = { opacity: 0.7, fontSize: 11 };
const errorStyle: CSSProperties = { color: "#c04040", fontSize: 12 };
const buttonStyle: CSSProperties = {
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
const sectionStyle: CSSProperties = {
	borderTop: "1px solid var(--fxtr-border-faint, #e8edf2)",
	paddingTop: 6,
};
const sectionTitleStyle: CSSProperties = { fontWeight: 600, fontSize: 11, marginBottom: 4 };
const subSectionStyle: CSSProperties = { marginTop: 6 };
const subTitleStyle: CSSProperties = { fontWeight: 600, fontSize: 10.5, opacity: 0.75 };
const genIdStyle: CSSProperties = { fontSize: 10, opacity: 0.7 };
const failureStyle: CSSProperties = { marginTop: 2 };
const failureMsgStyle: CSSProperties = {
	whiteSpace: "pre-wrap",
	fontSize: 11,
	opacity: 0.85,
	margin: "2px 0 4px 0",
	// Tracebacks can be long — cap the record, scroll for the rest.
	maxHeight: 180,
	overflow: "auto",
};
const logBoxStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: 1,
	maxHeight: 200,
	overflow: "auto",
	padding: "2px 4px",
	border: "1px solid var(--fxtr-border-faint, #e8edf2)",
	borderRadius: 4,
	fontFamily: "monospace",
	fontSize: 10.5,
};
const logLineStyle: CSSProperties = {
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
const logTsStyle: CSSProperties = { opacity: 0.5 };
const logKindStyle: CSSProperties = { opacity: 0.8 };
const logStepStyle: CSSProperties = { opacity: 0.7 };
const tableStyle: CSSProperties = { borderSpacing: 0, fontSize: 11, marginTop: 2 };
const thStyle: CSSProperties = {
	textAlign: "left",
	fontWeight: 600,
	fontSize: 10.5,
	opacity: 0.7,
	padding: "1px 10px 2px 2px",
	borderBottom: "1px solid var(--fxtr-border, #ccd5df)",
	whiteSpace: "nowrap",
};
const tdStyle: CSSProperties = {
	padding: "1px 10px 1px 2px",
	borderBottom: "1px solid var(--fxtr-border-faint, #e8edf2)",
	whiteSpace: "nowrap",
	verticalAlign: "baseline",
};
