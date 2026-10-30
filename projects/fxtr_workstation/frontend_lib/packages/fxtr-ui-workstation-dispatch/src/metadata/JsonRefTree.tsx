/**
 * Collapsible JSON tree renderer that knows how to display
 * `EntityRefStub` / `ServiceRefStub` leaves as `<RefLink as="inline">`.
 *
 * Used by `EntityMetadataView` to render the entity's loaded raw
 * data inside the metadata panel. Other JSON tree renderers in the
 * codebase (e.g. the workstation server's `debug_ui/src/tables/JsonTree.tsx`)
 * are deliberately not shared — they belong to their own surface
 * and have slightly different presentational needs.
 *
 * Node kinds:
 *
 *   - `EntityRefStub` / `ServiceRefStub` leaves → `<RefLink>` (no
 *     instanceof check on the stub class needed; we use
 *     `instanceof` against the abstract base classes from
 *     `@fxtr-ui/core`).
 *   - Plain objects / arrays → header row showing braces / brackets
 *     + child count, expandable; children indented one level.
 *   - `null` / `undefined` / `boolean` / `number` / `string` /
 *     `bigint` → styled inline literal.
 *   - Anything else (functions, symbols, class instances) →
 *     `[unknown]` fallback so we never crash on surprise leaves.
 *
 * The tree manages its own expand/collapse state inline. Objects
 * expand by default at the top level and collapse below — small
 * objects are easy to scan; deeply nested ones we want the user to
 * drill into explicitly. (`initiallyExpanded` prop lets callers
 * override.)
 */

import { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { type CSSProperties, type ReactNode, useState } from "react";
import { RefLink } from "../reflink/RefLink.js";

/**
 * Renderer for ref leaves inside the tree. Default uses
 * `<RefLink as="inline">` (dispatches to the consumer's
 * `InlineLink` renderer); debug surfaces pass a custom renderer
 * that produces `<DebugRefChip>` instead to avoid dispatch.
 */
export type RefLeafRenderer = (stub: EntityRefStub | ServiceRefStub) => ReactNode;

const DEFAULT_REF_RENDERER: RefLeafRenderer = (stub) => <RefLink stub={stub} as="inline" />;

export interface JsonRefTreeProps {
	readonly value: unknown;
	/** Top-level node starts expanded (default `true`). */
	readonly initiallyExpanded?: boolean;
	/** Override how `EntityRefStub` / `ServiceRefStub` leaves render. */
	readonly renderRef?: RefLeafRenderer;
}

export function JsonRefTree({
	value,
	initiallyExpanded = true,
	renderRef = DEFAULT_REF_RENDERER,
}: JsonRefTreeProps) {
	return (
		<div className="fw-jsonreftree" style={rootStyle}>
			<Node value={value} depth={0} expandedDefault={initiallyExpanded} renderRef={renderRef} />
		</div>
	);
}

interface NodeProps {
	readonly value: unknown;
	readonly depth: number;
	readonly expandedDefault: boolean;
	readonly renderRef: RefLeafRenderer;
}

function Node({ value, depth, expandedDefault, renderRef }: NodeProps) {
	// Refs first — instanceof against the abstract base classes
	// catches all concrete subclasses.
	if (value instanceof EntityRefStub || value instanceof ServiceRefStub) {
		return <>{renderRef(value)}</>;
	}
	if (Array.isArray(value)) {
		return (
			<ArrayNode
				value={value}
				depth={depth}
				expandedDefault={expandedDefault}
				renderRef={renderRef}
			/>
		);
	}
	if (value !== null && typeof value === "object") {
		return (
			<ObjectNode
				value={value as Record<string, unknown>}
				depth={depth}
				expandedDefault={expandedDefault}
				renderRef={renderRef}
			/>
		);
	}
	return <Primitive value={value} />;
}

function ArrayNode({
	value,
	depth,
	expandedDefault,
	renderRef,
}: NodeProps & { readonly value: unknown[] }) {
	const [expanded, setExpanded] = useState(expandedDefault);
	const empty = value.length === 0;
	if (empty) {
		return <span style={bracketStyle}>[]</span>;
	}
	return (
		<>
			<button
				type="button"
				style={toggleStyle}
				onClick={() => setExpanded((v) => !v)}
				aria-expanded={expanded}
			>
				<span style={triangleStyle}>{expanded ? "▾" : "▸"}</span>
				<span style={bracketStyle}>[</span>
				{!expanded && <span style={countStyle}>{value.length}</span>}
				{!expanded && <span style={bracketStyle}>]</span>}
			</button>
			{expanded && (
				<div style={{ ...indentStyle, marginLeft: indentFor(depth + 1) }}>
					{value.map((entry, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: array order is the key
						<div key={i} style={rowStyle}>
							<span style={keyStyle}>{i}</span>
							<span style={colonStyle}>:</span>
							<Node value={entry} depth={depth + 1} expandedDefault={false} renderRef={renderRef} />
							{i < value.length - 1 && <span style={commaStyle}>,</span>}
						</div>
					))}
					<span style={bracketStyle}>]</span>
				</div>
			)}
		</>
	);
}

function ObjectNode({
	value,
	depth,
	expandedDefault,
	renderRef,
}: NodeProps & { readonly value: Record<string, unknown> }) {
	const [expanded, setExpanded] = useState(expandedDefault);
	const entries = Object.entries(value);
	if (entries.length === 0) {
		return <span style={bracketStyle}>{"{}"}</span>;
	}
	return (
		<>
			<button
				type="button"
				style={toggleStyle}
				onClick={() => setExpanded((v) => !v)}
				aria-expanded={expanded}
			>
				<span style={triangleStyle}>{expanded ? "▾" : "▸"}</span>
				<span style={bracketStyle}>{"{"}</span>
				{!expanded && <span style={countStyle}>{entries.length}</span>}
				{!expanded && <span style={bracketStyle}>{"}"}</span>}
			</button>
			{expanded && (
				<div style={{ ...indentStyle, marginLeft: indentFor(depth + 1) }}>
					{entries.map(([k, v], i) => (
						<div key={k} style={rowStyle}>
							<span style={keyStyle}>{JSON.stringify(k)}</span>
							<span style={colonStyle}>:</span>
							<Node value={v} depth={depth + 1} expandedDefault={false} renderRef={renderRef} />
							{i < entries.length - 1 && <span style={commaStyle}>,</span>}
						</div>
					))}
					<span style={bracketStyle}>{"}"}</span>
				</div>
			)}
		</>
	);
}

function Primitive({ value }: { readonly value: unknown }) {
	let rendered: ReactNode;
	let style: CSSProperties = primitiveStyle;
	if (value === null) {
		rendered = "null";
		style = { ...style, color: "var(--text-muted)" };
	} else if (value === undefined) {
		rendered = "undefined";
		style = { ...style, color: "var(--text-muted)" };
	} else if (typeof value === "boolean") {
		rendered = String(value);
		style = { ...style, color: "var(--text-secondary)" };
	} else if (typeof value === "number" || typeof value === "bigint") {
		rendered = String(value);
		style = { ...style, color: "var(--text-secondary)" };
	} else if (typeof value === "string") {
		rendered = JSON.stringify(value);
		style = { ...style, color: "var(--text-primary)" };
	} else {
		rendered = "[unknown]";
		style = { ...style, color: "var(--text-muted)", fontStyle: "italic" };
	}
	return <span style={style}>{rendered}</span>;
}

function indentFor(depth: number): string {
	return `${depth * 12}px`;
}

const rootStyle: CSSProperties = {
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-body-sm)",
	color: "var(--text-primary)",
	lineHeight: 1.5,
};

const toggleStyle: CSSProperties = {
	display: "inline-flex",
	alignItems: "center",
	gap: "2px",
	padding: 0,
	border: "none",
	background: "transparent",
	color: "inherit",
	font: "inherit",
	cursor: "pointer",
};

const triangleStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontSize: "10px",
	width: "10px",
	display: "inline-block",
};

const bracketStyle: CSSProperties = {
	color: "var(--text-secondary)",
};

const countStyle: CSSProperties = {
	color: "var(--text-muted)",
	fontStyle: "italic",
	padding: "0 4px",
};

const indentStyle: CSSProperties = {
	display: "flex",
	flexDirection: "column",
	gap: "2px",
};

const rowStyle: CSSProperties = {
	display: "flex",
	alignItems: "baseline",
	gap: "4px",
	flexWrap: "wrap",
};

const keyStyle: CSSProperties = {
	color: "var(--text-accent, #06c)",
};

const colonStyle: CSSProperties = {
	color: "var(--text-muted)",
};

const commaStyle: CSSProperties = {
	color: "var(--text-muted)",
};

const primitiveStyle: CSSProperties = {
	whiteSpace: "pre-wrap",
};
