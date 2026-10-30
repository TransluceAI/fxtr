/**
 * Renderers for fxtr_experiment's **stored config entities** — the
 * canonical fingerprint carriers a grid's identity lives in:
 *
 * - `GridConfigEntity` (`fxtr_experiment.encoding`, wire form
 *   `{config: <tagged tree>}` per `config_to_json`): kind + axes, a
 *   dataframe's schema, and — the interesting part — a task grid's
 *   RULE: the task-fn slice plus every input binding (mode, slice
 *   template, received axis names, coord bindings).
 * - `SliceTemplateEntity` (`{template: <tagged tree>}` per
 *   `slice_to_json`): the standalone slice-template form (targets'
 *   slice ids, handle-fn templates in tests, …).
 *
 * Slice-template notation used throughout (legend shown in panels):
 * path steps join with `/`; a step's cell templates ride in `{…}`;
 * `internals:` marks a descent into a task's private internals;
 * axis templates render `$0`/`$1`… (bound to the holder grid's
 * flattened axis at that position), literals as their value, kept
 * axes as `*`; a trailing `→name` selects a task output.
 *
 * Exported as the `grid_config` / `slice_template` subtype maps in
 * `index.tsx`'s default export (dispatch-key-uuid keyed, ready to
 * splice into the module map).
 */

import type { EntityRefStub, OpResultStub } from "@fxtr-ui/core";
import { type DispatchedComponent, useDispatchSharedAtom } from "@fxtr-ui/react-dispatch";
import { makeLoadResultAtom } from "@fxtr-ui/state-util";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "@fxtr-ui/workstation-dispatch";
import { useAtomValue } from "jotai";
import { type CSSProperties, type ReactNode, useMemo } from "react";

// ===================================================================
// Wire shapes (the tagged trees from fxtr_experiment.encoding)
// ===================================================================

type TemplateW = readonly ["lit", string | number] | readonly ["param", number] | readonly ["keep"];
type StepW = readonly ["sub" | "int", ReadonlyArray<TemplateW>, string];
interface SliceW {
	readonly path: ReadonlyArray<StepW>;
	readonly target: ReadonlyArray<TemplateW>;
	readonly output: string | null;
}
type BindingW =
	| {
			readonly slice: SliceW;
			readonly mode: "read" | "write";
			readonly received: ReadonlyArray<string>;
	  }
	| {
			readonly coord: "str" | "int";
			readonly template: TemplateW;
			readonly received: string | null;
	  };
interface RuleW {
	readonly task_fn: SliceW;
	readonly inputs: Readonly<Record<string, BindingW>>;
}
type AxisW = readonly [string, string];
type OutputW = readonly ["entity"] | readonly ["frame", ReadonlyArray<AxisW>];
interface ConfigW {
	readonly kind: string;
	readonly axes: ReadonlyArray<AxisW>;
	readonly schema?: ReadonlyArray<AxisW>;
	readonly rule?: RuleW;
	readonly outputs?: Readonly<Record<string, OutputW>>;
}
interface GridConfigEntityW {
	readonly config: ConfigW;
}
interface SliceTemplateEntityW {
	readonly template: SliceW;
}

// ===================================================================
// Notation (pure text forms)
// ===================================================================

function templateText(t: TemplateW): string {
	if (t[0] === "lit") return JSON.stringify(t[1]);
	if (t[0] === "param") return `$${t[1]}`;
	return "*";
}

function sliceText(s: SliceW): string {
	const steps = s.path.map(([tag, cell, key]) => {
		const coords = cell.length ? `{${cell.map(templateText).join(",")}}` : "";
		return `${tag === "int" ? "internals:" : ""}${key}${coords}`;
	});
	const target = s.target.length ? `[${s.target.map(templateText).join(",")}]` : "";
	const output = s.output ? ` →${s.output}` : "";
	return `${steps.join("/") || "(root)"}${target}${output}`;
}

function bindingText(b: BindingW): string {
	if ("coord" in b) {
		const received = b.received ? ` as ${b.received}` : "";
		return `coord(${b.coord}) ${templateText(b.template)}${received}`;
	}
	const received = b.received.length ? ` as (${b.received.join(", ")})` : "";
	return `${sliceText(b.slice)}${received}`;
}

function axesText(axes: ReadonlyArray<AxisW>): string {
	return axes.map(([name]) => name).join(", ");
}

function configLabel(c: ConfigW): string {
	return `${c.kind}(${axesText(c.axes)})`;
}

function configHover(c: ConfigW): string {
	const lines = [`axes: ${c.axes.map(([n, k]) => `${n}:${k}`).join(", ") || "(scalar)"}`];
	if (c.schema) lines.push(`schema: ${c.schema.map(([n, k]) => `${n}:${k}`).join(", ")}`);
	if (c.rule) {
		lines.push(`fn: ${sliceText(c.rule.task_fn)}`);
		for (const [name, b] of Object.entries(c.rule.inputs)) {
			lines.push(`${name} ← ${bindingText(b)}`);
		}
	}
	return lines.join("\n");
}

// ===================================================================
// Shared load + shell helpers
// ===================================================================

type Loaded<T> =
	| { readonly kind: "loading" }
	| { readonly kind: "errored"; readonly message: string }
	| { readonly kind: "filled"; readonly value: T };

function useEntity<T>(stub: EntityRefStub): Loaded<T> {
	const sharedAtom = useDispatchSharedAtom<OpResultStub<T> | null>(null);
	const resultAtom = useMemo(() => makeLoadResultAtom<T>(sharedAtom, stub), [sharedAtom, stub]);
	return useAtomValue(resultAtom) as Loaded<T>;
}

function entryFor<T>(spec: {
	readonly tag: string;
	readonly label: (value: T) => string;
	readonly hoverBody: (value: T) => string;
	readonly panel: (value: T) => ReactNode;
}) {
	const panel: DispatchedComponent<EntityRefStub, FullPanelProps> = ({ stub }) => {
		const result = useEntity<T>(stub);
		return (
			<div style={panelStyle}>
				{result.kind === "errored" ? (
					<span style={errorStyle}>load failed: {result.message}</span>
				) : result.kind === "loading" ? (
					<span style={mutedStyle}>loading…</span>
				) : (
					spec.panel(result.value)
				)}
			</div>
		);
	};
	const link =
		(inline: boolean): DispatchedComponent<EntityRefStub, LinkProps> =>
		({ stub, linkText }) => {
			const result = useEntity<T>(stub);
			const label = linkText ?? (result.kind === "filled" ? spec.label(result.value) : "…");
			return inline ? (
				<code style={inlinePillStyle}>
					{spec.tag}:{label}
				</code>
			) : (
				<span style={pillStyle}>
					<span style={pillTagStyle}>{spec.tag}</span>
					<span style={pillLabelStyle}>{label}</span>
				</span>
			);
		};
	const hover: DispatchedComponent<EntityRefStub, HoverPreviewProps> = ({ stub }) => {
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
		return (
			<div style={hoverCardStyle}>
				<div style={hoverTitleStyle}>{spec.label(result.value)}</div>
				<div style={hoverBodyStyle}>{spec.hoverBody(result.value)}</div>
			</div>
		);
	};
	return {
		[FullPanel.uuid]: panel,
		[BlockLink.uuid]: link(false),
		[InlineLink.uuid]: link(true),
		[HoverPreview.uuid]: hover,
	};
}

// ===================================================================
// Panel pieces
// ===================================================================

function KindChip({ kind }: { kind: string }) {
	return <span style={kindChipStyle}>{kind}</span>;
}

function PairTable({ title, pairs }: { title: string; pairs: ReadonlyArray<AxisW> }) {
	return (
		<div style={sectionStyle}>
			<div style={sectionTitleStyle}>{title}</div>
			{pairs.length === 0 ? (
				<span style={mutedStyle}>(none — scalar)</span>
			) : (
				<div style={chipRowStyle}>
					{pairs.map(([name, kind]) => (
						<span key={name} style={chipStyle}>
							{name}
							<span style={chipKindStyle}>:{kind}</span>
						</span>
					))}
				</div>
			)}
		</div>
	);
}

function RuleSection({ rule }: { rule: RuleW }) {
	return (
		<>
			<div style={sectionStyle}>
				<div style={sectionTitleStyle}>task fn</div>
				<code style={sliceCodeStyle}>{sliceText(rule.task_fn)}</code>
			</div>
			<div style={sectionStyle}>
				<div style={sectionTitleStyle}>input bindings</div>
				<table style={tableStyle}>
					<tbody>
						{Object.entries(rule.inputs).map(([name, b]) => (
							<tr key={name}>
								<td style={tdNameStyle}>{name}</td>
								<td style={tdStyle}>
									<span style={"coord" in b ? coordChipStyle : modeChipStyle(b.mode)}>
										{"coord" in b ? "coord" : b.mode}
									</span>
								</td>
								<td style={tdStyle}>
									<code style={sliceCodeStyle}>{bindingText(b)}</code>
								</td>
							</tr>
						))}
					</tbody>
				</table>
			</div>
		</>
	);
}

function OutputsSection({ outputs }: { outputs: Readonly<Record<string, OutputW>> }) {
	return (
		<div style={sectionStyle}>
			<div style={sectionTitleStyle}>outputs</div>
			<table style={tableStyle}>
				<tbody>
					{Object.entries(outputs).map(([name, o]) => (
						<tr key={name}>
							<td style={tdNameStyle}>{name}</td>
							<td style={tdStyle}>
								{o[0] === "frame" ? (
									<code style={sliceCodeStyle}>
										frame({o[1].map(([n, k]) => `${n}:${k}`).join(", ")})
									</code>
								) : (
									<code style={sliceCodeStyle}>entity</code>
								)}
							</td>
						</tr>
					))}
				</tbody>
			</table>
		</div>
	);
}

const NOTATION_LEGEND =
	"notation: path steps join with /, {…} = cell coordinate templates, " +
	"internals: = descent into a task's internals, $N = the holder grid's " +
	"flattened axis N, * = kept axis, →name = task output selection";

/** The `grid_config` subtype map ({dispatch key uuid: component}). */
export const gridConfigRenderers = entryFor<GridConfigEntityW>({
	tag: "grid config",
	label: (e) => configLabel(e.config),
	hoverBody: (e) => configHover(e.config),
	panel: (e) => (
		<>
			<h2 style={titleStyle}>
				grid config <KindChip kind={e.config.kind} />
			</h2>
			<PairTable title="axes (own)" pairs={e.config.axes} />
			{e.config.schema && <PairTable title="schema" pairs={e.config.schema} />}
			{e.config.rule && <RuleSection rule={e.config.rule} />}
			{e.config.outputs && <OutputsSection outputs={e.config.outputs} />}
			<p style={legendStyle}>{NOTATION_LEGEND}</p>
		</>
	),
});

/** The `slice_template` subtype map ({dispatch key uuid: component}). */
export const sliceTemplateRenderers = entryFor<SliceTemplateEntityW>({
	tag: "slice",
	label: (e) => sliceText(e.template),
	hoverBody: (e) =>
		[
			`path: ${e.template.path.length ? "" : "(root)"}`,
			...e.template.path.map(
				([tag, cell, key]) =>
					`  ${tag === "int" ? "internals:" : ""}${key}${
						cell.length ? ` {${cell.map(templateText).join(", ")}}` : ""
					}`,
			),
			`target: [${e.template.target.map(templateText).join(", ")}]`,
			...(e.template.output ? [`output: ${e.template.output}`] : []),
		].join("\n"),
	panel: (e) => (
		<>
			<h2 style={titleStyle}>slice template</h2>
			<code style={sliceCodeStyle}>{sliceText(e.template)}</code>
			<p style={legendStyle}>{NOTATION_LEGEND}</p>
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
const titleStyle: CSSProperties = {
	margin: "0 0 10px 0",
	fontSize: 16,
	display: "flex",
	alignItems: "center",
	gap: 8,
};
const errorStyle: CSSProperties = { color: "#c00", fontSize: 12 };
const mutedStyle: CSSProperties = { opacity: 0.6, fontStyle: "italic", fontSize: 12 };

const pillStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: 6,
	padding: "2px 8px",
	borderRadius: 6,
	border: "1px solid var(--fxtr-border, #a9c0b2)",
	background: "var(--fxtr-bg-raised, #f1f8f3)",
	color: "#28523a",
	fontSize: 12,
};
const pillTagStyle: CSSProperties = { fontWeight: 600 };
const pillLabelStyle: CSSProperties = { fontSize: 11, fontFamily: "monospace" };
const inlinePillStyle: CSSProperties = {
	padding: "1px 4px",
	borderRadius: 4,
	background: "#f1f8f3",
	color: "#28523a",
	fontSize: 12,
};
const hoverCardStyle: CSSProperties = {
	padding: 8,
	border: "1px solid var(--fxtr-border, #a9c0b2)",
	borderRadius: 6,
	background: "#f1f8f3",
	color: "#28523a",
	fontSize: 12,
	minWidth: 220,
	maxWidth: 420,
};
const hoverTitleStyle: CSSProperties = { fontWeight: 600, fontFamily: "monospace" };
const hoverBodyStyle: CSSProperties = {
	fontSize: 11,
	marginTop: 4,
	opacity: 0.85,
	whiteSpace: "pre-wrap",
	fontFamily: "monospace",
};

const kindChipStyle: CSSProperties = {
	padding: "1px 8px",
	borderRadius: 10,
	background: "#28523a",
	color: "#fff",
	fontSize: 11,
	fontWeight: 700,
};
const sectionStyle: CSSProperties = { marginTop: 12 };
const sectionTitleStyle: CSSProperties = { fontSize: 11, opacity: 0.65, fontWeight: 600 };
const chipRowStyle: CSSProperties = { display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 };
const chipStyle: CSSProperties = {
	padding: "2px 8px",
	borderRadius: 10,
	background: "#eef3f9",
	border: "1px solid #c8d6e8",
	fontSize: 12,
	fontFamily: "monospace",
};
const chipKindStyle: CSSProperties = { opacity: 0.55 };
const sliceCodeStyle: CSSProperties = { fontSize: 12, whiteSpace: "pre-wrap" };
const tableStyle: CSSProperties = { borderCollapse: "collapse", marginTop: 4 };
const tdStyle: CSSProperties = {
	padding: "3px 10px 3px 0",
	borderBottom: "1px solid var(--fxtr-border, #e2e8ee)",
	verticalAlign: "baseline",
};
const tdNameStyle: CSSProperties = { ...tdStyle, fontWeight: 600, fontSize: 12 };
const coordChipStyle: CSSProperties = {
	padding: "1px 6px",
	borderRadius: 8,
	background: "#eef3f9",
	border: "1px solid #c8d6e8",
	fontSize: 10,
	fontWeight: 700,
};
function modeChipStyle(mode: "read" | "write"): CSSProperties {
	return {
		padding: "1px 6px",
		borderRadius: 8,
		background: mode === "write" ? "#fdecec" : "#eef7f0",
		border: `1px solid ${mode === "write" ? "#e6b3b3" : "#b3d6bd"}`,
		fontSize: 10,
		fontWeight: 700,
	};
}
const legendStyle: CSSProperties = {
	marginTop: 14,
	fontSize: 10,
	opacity: 0.55,
	fontStyle: "italic",
};
