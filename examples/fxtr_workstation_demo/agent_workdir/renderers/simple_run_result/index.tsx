/**
 * Renderer for the `SimpleRunResult` ENTITY (from
 * `packages/simple_tasks/`) — the durable record a `run_batched`
 * call returns. Same grid layout as the live scheduler panel
 * (rows = inputs, columns = input + steps, cells = block RefLinks
 * of each step's output), but loaded from the stored entity rather
 * than the event stream, with a header identifying the run id +
 * completion timestamp.
 *
 * Cell states are derived from the entity: an output ref in
 * `cells[name][i]` renders as a block RefLink; `failures[name] ===
 * [i, msg]` renders the error at column `i`; later columns of a
 * failed chain render as "not run". Outputs that the streaming
 * scheduler served from its cache (`cached[name][i] === true`) get a
 * "cached" badge; the simple scheduler never sets `cached`, so its
 * results show none.
 */

import type { EntityRefStub, FxtrSessionAtom, OpResultStub } from "@fxtr-ui/core";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { atomWithDefault, makeLoadResultAtom, subAtom } from "@fxtr-ui/state-util";
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
import { useAtomValue } from "jotai";
import { type CSSProperties, useMemo } from "react";

// ----- wire-decoded entity shape (ChildFxtrID fields arrive as stubs) -----

interface RunResultLoaded {
	readonly run_id: string;
	readonly step_labels: readonly string[];
	readonly input_names: readonly string[];
	readonly inputs: Readonly<Record<string, EntityRefStub>>;
	readonly cells: Readonly<Record<string, Readonly<Record<string, EntityRefStub>>>>;
	readonly failures: Readonly<Record<string, readonly [number, string]>>;
	/** input_name -> str(step_index) -> whether that output was served
	 * from the scheduler's cache (streaming scheduler only; empty/absent
	 * for the simple scheduler). */
	readonly cached?: Readonly<Record<string, Readonly<Record<string, boolean>>>>;
	readonly completed_at?: string | null;
}

interface RunResultStorage {
	readonly load: OpResultStub<RunResultLoaded> | null;
}

function useRunResult(
	stub: EntityRefStub,
	storage: FxtrSessionAtom<unknown>,
):
	| { readonly kind: "loading" }
	| { readonly kind: "errored"; readonly message: string }
	| { readonly kind: "filled"; readonly value: RunResultLoaded } {
	const resultAtom = useMemo(() => {
		const panelAtom = atomWithDefault<RunResultStorage>(
			storage as FxtrSessionAtom<RunResultStorage | null>,
			{ load: null },
		);
		return makeLoadResultAtom<RunResultLoaded>(subAtom(panelAtom, "load"), stub);
	}, [storage, stub]);
	return useAtomValue(resultAtom);
}

// ----- FullPanel -----

export const RunResultFullPanel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({
	stub,
	ownStorage,
}) => {
	const result = useRunResult(stub, ownStorage);
	if (result.kind === "loading") {
		return (
			<div className="fw-panel__body" style={rootStyle}>
				<span style={mutedStyle}>loading run result…</span>
			</div>
		);
	}
	if (result.kind === "errored") {
		return (
			<div className="fw-panel__body" style={rootStyle}>
				<span style={errorStyle}>load failed: {result.message}</span>
			</div>
		);
	}
	const run = result.value;
	const nFailed = Object.keys(run.failures).length;
	const nOk = run.input_names.length - nFailed;
	return (
		<div className="fw-panel__body" style={rootStyle}>
			<h2 style={headerStyle}>
				run result · <code>{run.run_id}</code>
			</h2>
			<div style={statusRowStyle}>
				<span style={mutedStyle}>
					{nOk} ok / {nFailed} failed
					{run.completed_at != null ? ` — completed ${run.completed_at}` : ""}
				</span>
			</div>
			<div style={{ overflow: "auto" }}>
				<table style={tableStyle}>
					<thead>
						<tr>
							<th style={thStyle}>input</th>
							{run.step_labels.map((label, i) => (
								// biome-ignore lint/suspicious/noArrayIndexKey: column order is the identity
								<th key={i} style={thStyle}>
									{label}
								</th>
							))}
						</tr>
					</thead>
					<tbody>
						{run.input_names.map((name) => {
							const failure = run.failures[name];
							const row = run.cells[name] ?? {};
							return (
								<tr key={name}>
									<td style={tdStyle}>
										<div style={inputCellHeaderStyle}>{name}</div>
										{run.inputs[name] !== undefined ? (
											<RefLink stub={run.inputs[name] as EntityRefStub} as="block" />
										) : (
											<span style={mutedStyle}>(input unavailable)</span>
										)}
									</td>
									{run.step_labels.map((_label, i) => {
										const output = row[String(i)];
										const wasCached = run.cached?.[name]?.[String(i)] === true;
										return (
											// biome-ignore lint/suspicious/noArrayIndexKey: column order is the identity
											<td key={i} style={tdStyle}>
												{output !== undefined ? (
													<div style={outputCellStyle}>
														{wasCached ? <span style={cachedBadgeStyle}>cached</span> : null}
														<RefLink stub={output} as="block" />
													</div>
												) : failure !== undefined && failure[0] === i ? (
													<div style={errorStyle}>✗ {failure[1]}</div>
												) : (
													<span style={mutedStyle}>— not run</span>
												)}
											</td>
										);
									})}
								</tr>
							);
						})}
					</tbody>
				</table>
			</div>
		</div>
	);
};

// ----- link keys -----

function useSharedRunResult(stub: EntityRefStub) {
	const sharedAtom = useDispatchSharedAtom<RunResultStorage | null>(null);
	const resultAtom = useMemo(() => {
		const lifted = atomWithDefault<RunResultStorage>(sharedAtom, { load: null });
		return makeLoadResultAtom<RunResultLoaded>(subAtom(lifted, "load"), stub);
	}, [sharedAtom, stub]);
	return useAtomValue(resultAtom);
}

export const RunResultBlockLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const result = useSharedRunResult(stub);
	return (
		<span style={linkPillStyle}>
			<span style={linkTagStyle}>run result</span>
			<span style={linkLabelStyle}>
				{linkText ??
					(result.kind === "filled" ? `run ${result.value.run_id}` : `run · ${stub.shortId}`)}
			</span>
		</span>
	);
};

export const RunResultInlineLink: DispatchedComponent<EntityRefStub, LinkProps> = ({
	stub,
	linkText,
}) => {
	const result = useSharedRunResult(stub);
	return (
		<code style={inlineLinkStyle}>
			{linkText ?? (result.kind === "filled" ? `run ${result.value.run_id}` : stub.shortId)}
		</code>
	);
};

export const RunResultHoverPreview: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({
	stub,
}) => {
	const result = useSharedRunResult(stub);
	return (
		<div style={hoverCardStyle}>
			<div style={hoverTitleStyle}>
				{result.kind === "filled" ? `run result · ${result.value.run_id}` : "run result"}
			</div>
			{result.kind === "filled" ? (
				<div style={hoverBodyStyle}>
					{result.value.input_names.length} inputs ×{" "}
					{result.value.step_labels.join(" → ") || "(no steps)"}
					{result.value.completed_at != null ? (
						<>
							<br />
							completed {result.value.completed_at}
						</>
					) : null}
				</div>
			) : (
				<span style={mutedStyle}>{result.kind === "errored" ? result.message : "loading…"}</span>
			)}
		</div>
	);
};

// ----- styles (matched to the live scheduler grid) -----

const rootStyle: CSSProperties = {
	padding: "var(--sp-5, 16px)",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-3, 12px)",
	height: "100%",
};
const headerStyle: CSSProperties = {
	margin: 0,
	fontSize: "var(--text-heading, 18px)",
	color: "var(--text-primary)",
};
const statusRowStyle: CSSProperties = {
	display: "flex",
	alignItems: "center",
	gap: "var(--sp-2, 8px)",
};
const tableStyle: CSSProperties = {
	borderCollapse: "collapse",
	fontSize: "var(--text-body-sm)",
};
const thStyle: CSSProperties = {
	textAlign: "left",
	padding: "var(--sp-1) var(--sp-2)",
	borderBottom: "2px solid var(--border-subtle)",
	color: "var(--text-secondary)",
	verticalAlign: "bottom",
};
const tdStyle: CSSProperties = {
	padding: "var(--sp-2)",
	border: "1px solid var(--border-subtle)",
	verticalAlign: "top",
	minWidth: 160,
	maxWidth: 360,
};
const inputCellHeaderStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption)",
	color: "var(--text-muted)",
	marginBottom: "var(--sp-1, 4px)",
};
const errorStyle: CSSProperties = {
	color: "var(--status-error, #c00)",
	fontSize: "var(--text-body-sm)",
	whiteSpace: "pre-wrap",
	wordBreak: "break-word",
};
const mutedStyle: CSSProperties = { color: "var(--text-muted)", fontSize: "var(--text-body-sm)" };
const outputCellStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-1, 4px)",
	alignItems: "flex-start",
};
const cachedBadgeStyle: CSSProperties = {
	fontSize: "var(--text-caption)",
	color: "var(--status-info, #06c)",
	border: "1px solid var(--status-info, #06c)",
	borderRadius: "var(--radius-badge, 6px)",
	padding: "0 5px",
	textTransform: "uppercase",
	letterSpacing: "0.05em",
};
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
const linkLabelStyle: CSSProperties = { color: "var(--text-primary)" };
const inlineLinkStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "var(--surface-sunken)",
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
};
const hoverCardStyle: CSSProperties = {
	minWidth: 220,
	display: "flex",
	flexDirection: "column",
	gap: "var(--sp-2, 8px)",
	padding: "var(--sp-3, 12px)",
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600, color: "var(--text-primary)" };
const hoverBodyStyle: CSSProperties = {
	color: "var(--text-secondary)",
	fontSize: "var(--text-body-sm)",
	lineHeight: 1.4,
};

export default {
	simple_run_result: {
		[FullPanel.uuid]: RunResultFullPanel,
		[BlockLink.uuid]: RunResultBlockLink,
		[InlineLink.uuid]: RunResultInlineLink,
		[HoverPreview.uuid]: RunResultHoverPreview,
	},
};
