/**
 * The workflow header: the lint badge (sound / clean / findings) plus
 * the workflow-level lists (targets / sourcing / bindings / the lint
 * inventory) behind toggle buttons, the live-watch indicator, and the
 * manual "resubscribe" control (the escape hatch — drops every watch
 * for fresh snapshots). Pure presentation over the workflow watch's
 * folded mirror (`derive/folds.ts`); before the snapshot batch lands,
 * counts render as "…".
 */

import { RefLink } from "@fxtr-ui/workstation-dispatch";
import type { CSSProperties, ReactNode } from "react";

import type { RunAction } from "./actions.js";
import type { WorkflowMirror } from "./derive/folds.js";
import type { HeaderList } from "./panel_state.js";
import { lintClean, lintSound } from "./wire/helpers.js";
import type { LintReport } from "./wire/types.js";

/** The workflow watch's header presence: a quiet "live" marker while
 * subscribed (with an "updating…" suffix during a slow recompute), and
 * an explicit failure + retry when the subscribe errored. */
export type LiveStatus = {
	readonly phase: "connecting" | "live" | "errored";
	readonly message: string | null;
	/** A slow-flagged recompute (summarize / lint) is in flight. */
	readonly recomputing: boolean;
	readonly onRetry: () => void;
};

export function HeaderView({
	workflow,
	openList,
	onToggleList,
	onResubscribe,
	runAction,
	live,
}: {
	readonly workflow: WorkflowMirror;
	readonly openList: HeaderList | null;
	readonly onToggleList: (list: HeaderList) => void;
	readonly onResubscribe: () => void;
	readonly runAction: RunAction;
	readonly live: LiveStatus;
}): ReactNode {
	const loading = !workflow.snapshotted;
	return (
		<div>
			<div style={barStyle}>
				<h2 style={titleStyle}>experiment dashboard</h2>
				<LintBadge lint={workflow.lint} loading={loading} onClick={() => onToggleList("lint")} />
				<ListToggle
					label="targets"
					count={loading ? null : workflow.targets.length}
					open={openList === "targets"}
					onClick={() => onToggleList("targets")}
				/>
				<ListToggle
					label="sourcing"
					count={loading ? null : workflow.sourcing.length}
					open={openList === "sourcing"}
					onClick={() => onToggleList("sourcing")}
				/>
				<ListToggle
					label="bindings"
					count={loading ? null : workflow.bindings.length}
					open={openList === "bindings"}
					onClick={() => onToggleList("bindings")}
				/>
				<span style={{ flex: 1 }} />
				<LiveIndicator live={live} />
				<button
					type="button"
					style={buttonStyle}
					title="drop every watch and resubscribe for fresh snapshots (the manual escape hatch)"
					onClick={onResubscribe}
				>
					resubscribe
				</button>
			</div>
			{openList === "targets" && <TargetsList workflow={workflow} runAction={runAction} />}
			{openList === "sourcing" && <SourcingList workflow={workflow} />}
			{openList === "bindings" && <BindingsList workflow={workflow} />}
			{openList === "lint" && <LintList workflow={workflow} />}
		</div>
	);
}

function LiveIndicator({ live }: { readonly live: LiveStatus }): ReactNode {
	if (live.phase === "live") {
		return (
			<span
				style={{ ...liveStyle, color: "#3a8a4a" }}
				title="subscribed to the workflow watch — the mirror folds server-pushed patches"
			>
				● live{live.recomputing ? <span style={{ opacity: 0.65 }}> · updating…</span> : null}
			</span>
		);
	}
	if (live.phase === "errored") {
		return (
			<span style={{ ...liveStyle, color: "#c04040" }} title={live.message ?? undefined}>
				live updates failed
				<button type="button" style={{ ...buttonStyle, marginLeft: 6 }} onClick={live.onRetry}>
					retry
				</button>
			</span>
		);
	}
	return <span style={{ ...liveStyle, opacity: 0.55 }}>connecting…</span>;
}

function LintBadge({
	lint,
	loading,
	onClick,
}: {
	readonly lint: LintReport | null;
	readonly loading: boolean;
	readonly onClick: () => void;
}): ReactNode {
	let label = "lint: …";
	let color = "var(--fxtr-border, #aab8c6)";
	if (!loading && lint !== null) {
		if (lintClean(lint)) {
			label = "lint: clean";
			color = "#3a8a4a";
		} else if (lintSound(lint)) {
			label = `lint: sound, ${lint.ungrounded.length} ungrounded`;
			color = "#c09a3a";
		} else {
			const findings =
				lint.dangling_sourcing.length + lint.dangling_targets.length + lint.target_findings.length;
			label = `lint: ${findings} finding${findings === 1 ? "" : "s"}`;
			color = "#c04040";
		}
	}
	return (
		<button type="button" style={{ ...pillStyle, borderColor: color, color }} onClick={onClick}>
			{label}
		</button>
	);
}

function ListToggle({
	label,
	count,
	open,
	onClick,
}: {
	readonly label: string;
	/** Null while the mirror is still loading. */
	readonly count: number | null;
	readonly open: boolean;
	readonly onClick: () => void;
}): ReactNode {
	return (
		<button
			type="button"
			style={{ ...pillStyle, ...(open ? pillOpenStyle : {}) }}
			onClick={onClick}
		>
			{label} ({count ?? "…"})
		</button>
	);
}

function ListShell({
	loading,
	children,
}: {
	readonly loading: boolean;
	readonly children: ReactNode;
}): ReactNode {
	if (loading) return <div style={mutedStyle}>loading…</div>;
	return <div style={listStyle}>{children}</div>;
}

/** One target = "this cell is a wanted DAG root, pinned at this
 * expected grid config". Location + only-if-abnormal badges + the
 * labeled pin. Attached records get "run" — `demand_target` with the
 * record passed back VERBATIM (the wire round-trip contract); a
 * dangling record faults on demand, so its row offers none. */
function TargetsList({
	workflow,
	runAction,
}: {
	readonly workflow: WorkflowMirror;
	readonly runAction: RunAction;
}) {
	return (
		<ListShell loading={!workflow.snapshotted}>
			{workflow.targets.length === 0 ? (
				<div style={mutedStyle}>no targets</div>
			) : (
				workflow.targets.map((t, i) => (
					// biome-ignore lint/suspicious/noArrayIndexKey: display list, order-stable per mirror value
					<div key={i} style={listRowStyle}>
						<code style={codeStyle}>
							{t.grid_path}
							{t.coords}
						</code>
						<AbnormalChip status={t.status} />
						<AbnormalChip status={t.grid_grounding} />
						<span style={mutedInlineStyle}>pinned config:</span>
						<RefLink stub={t.target.expected_config} as="inline" />
						{t.status === "attached" && (
							<button
								type="button"
								style={rowButtonStyle}
								title="demand this target now (the stored record, verbatim)"
								onClick={() =>
									runAction(`run target ${t.grid_path} @ ${t.coords}`, [
										{ kind: "demand_target", record: t.target },
									])
								}
							>
								run
							</button>
						)}
					</div>
				))
			)}
		</ListShell>
	);
}

/** One sourcing record = the user's declared root fact: "this grid
 * (at its pinned config) / this cell's value (at its pinned value id)
 * is user-sourced" — grid-level and cell-level rows are distinct
 * record kinds. ("Vouch" is deliberately avoided: in the grounding
 * vocabulary it belongs to the request-edge self-vouching mechanism.) */
function SourcingList({ workflow }: { readonly workflow: WorkflowMirror }) {
	return (
		<ListShell loading={!workflow.snapshotted}>
			{workflow.sourcing.length === 0 ? (
				<div style={mutedStyle}>no sourcing records</div>
			) : (
				workflow.sourcing.map((s, i) => (
					// biome-ignore lint/suspicious/noArrayIndexKey: display list, order-stable per mirror value
					<div key={i} style={listRowStyle}>
						<span style={tagStyle}>{s.coords === "" ? "grid" : "cell"}</span>
						<code style={codeStyle}>
							{s.grid_path}
							{s.coords}
						</code>
						<AbnormalChip status={s.status} />
						<span style={mutedInlineStyle}>
							{s.coords === "" ? "user-sourced at pinned config:" : "user-sourced at pinned value:"}
						</span>
						<RefLink stub={s.fingerprint} as="inline" />
					</div>
				))
			)}
		</ListShell>
	);
}

/** One binding = a handle entity currently bound to a live connector
 * (ephemeral runner config), joined to the handle cells holding it. */
function BindingsList({ workflow }: { readonly workflow: WorkflowMirror }) {
	return (
		<ListShell loading={!workflow.snapshotted}>
			{workflow.bindings.length === 0 ? (
				<div style={mutedStyle}>no live bindings</div>
			) : (
				workflow.bindings.map((b) => (
					<div key={b.key.shortId} style={listRowStyle}>
						<span style={tagStyle}>handle key</span>
						<RefLink stub={b.key} as="inline" />
						<span style={mutedInlineStyle}>
							{b.cells.length === 0 ? "live connector bound — held by no handle cell" : "at"}
						</span>
						{b.cells.map((c) => (
							<code key={`${c.grid_path}${c.coords}`} style={codeStyle}>
								{c.grid_path}
								{c.coords}
							</code>
						))}
					</div>
				))
			)}
		</ListShell>
	);
}

function LintList({ workflow }: { readonly workflow: WorkflowMirror }) {
	const lint = workflow.lint;
	return (
		<ListShell loading={!workflow.snapshotted || lint === null}>
			{lint !== null && (
				<>
					{lint.target_findings.map((f, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: display list, order-stable per mirror value
						<div key={`f${i}`} style={listRowStyle}>
							<StatusChip status="red target" />
							<code style={codeStyle}>{f.grid_path}</code>
							<code style={codeStyle}>{f.coords}</code>
							<span style={mutedInlineStyle}>
								{f.verdict}
								{f.reason ? ` — ${f.reason}` : ""}
							</span>
						</div>
					))}
					{lint.dangling_targets.map((t, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: display list, order-stable per mirror value
						<div key={`dt${i}`} style={listRowStyle}>
							<StatusChip status={`dangling target (${t.status})`} />
							<code style={codeStyle}>
								{t.grid_path}
								{t.coords}
							</code>
						</div>
					))}
					{lint.dangling_sourcing.map((s, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: display list, order-stable per mirror value
						<div key={`ds${i}`} style={listRowStyle}>
							<StatusChip status={`dangling sourcing (${s.status})`} />
							<code style={codeStyle}>{s.grid_path}</code>
						</div>
					))}
					{lint.ungrounded.map((u, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: display list, order-stable per mirror value
						<div key={`u${i}`} style={listRowStyle}>
							<StatusChip status={u.verdict} />
							<span style={mutedInlineStyle}>{u.kind}</span>
							<code style={codeStyle}>{u.grid_path}</code>
							{u.coords !== "" && <code style={codeStyle}>{u.coords}</code>}
							{u.reason && <span style={mutedInlineStyle}>{u.reason}</span>}
						</div>
					))}
					{lintClean(lint) && <div style={mutedStyle}>clean — nothing to report</div>}
				</>
			)}
		</ListShell>
	);
}

function StatusChip({ status }: { readonly status: string }): ReactNode {
	const color = status.includes("dangling")
		? "#c04040"
		: status === "attached" || status === "grounded"
			? "#3a8a4a"
			: status === "orphaned" || status.includes("red")
				? "#c04040"
				: "#c09a3a";
	return <span style={{ ...chipStyle, borderColor: color, color }}>{status}</span>;
}

/** A status chip that stays silent for the healthy default states —
 * rows only badge what's abnormal (dangling / unanchored / orphaned). */
function AbnormalChip({ status }: { readonly status: string | null }): ReactNode {
	if (status === null || status === "attached" || status === "grounded") return null;
	return <StatusChip status={status} />;
}

const barStyle: CSSProperties = {
	display: "flex",
	alignItems: "baseline",
	gap: 8,
	paddingBottom: 8,
	borderBottom: "1px solid var(--fxtr-border, #ccd5df)",
};
const titleStyle: CSSProperties = { margin: 0, fontSize: 16, marginRight: 6 };
const pillStyle: CSSProperties = {
	fontSize: 11,
	padding: "2px 8px",
	borderRadius: 10,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	background: "none",
	cursor: "pointer",
};
const pillOpenStyle: CSSProperties = { background: "var(--fxtr-bg-selected, #dbe7f5)" };
const liveStyle: CSSProperties = {
	fontSize: 11,
	display: "inline-flex",
	alignItems: "baseline",
};
const buttonStyle: CSSProperties = {
	padding: "3px 10px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	background: "var(--fxtr-bg-raised, #f4f7fa)",
	cursor: "pointer",
	fontSize: 12,
};
const listStyle: CSSProperties = {
	padding: 8,
	borderBottom: "1px solid var(--fxtr-border, #ccd5df)",
	display: "flex",
	flexDirection: "column",
	gap: 4,
	maxHeight: 180,
	overflow: "auto",
};
const listRowStyle: CSSProperties = {
	display: "flex",
	gap: 8,
	alignItems: "baseline",
	fontSize: 12,
};
const chipStyle: CSSProperties = {
	fontSize: 10,
	padding: "1px 6px",
	borderRadius: 8,
	border: "1px solid",
};
const tagStyle: CSSProperties = {
	fontSize: 10,
	padding: "1px 6px",
	borderRadius: 3,
	background: "var(--fxtr-bg-raised, #eef2f6)",
	opacity: 0.8,
};
const rowButtonStyle: CSSProperties = {
	padding: "0 6px",
	borderRadius: 4,
	border: "1px solid var(--fxtr-border, #aab8c6)",
	background: "var(--fxtr-bg-raised, #f4f7fa)",
	cursor: "pointer",
	fontSize: 10,
};
const codeStyle: CSSProperties = { fontSize: 11, opacity: 0.8 };
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12, padding: 4 };
const mutedInlineStyle: CSSProperties = { opacity: 0.65, fontSize: 11 };
