/**
 * FxtrCbor-aware recursive tree view. Adapted from thingwatcher's
 * components/common/JsonTree.tsx with:
 * - tagged ID classes rendered as <EntityLink as="inline"> so nested
 *   refs are clickable / hoverable / draggable through the dispatch
 *   system.
 * - Uint8Array rendered as `bytes(N): hex…`
 * - styling via workstation theme tokens (inline styles; no CSS module)
 */

import { type FxtrCbor, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID } from "@fxtr/core";
import type React from "react";
import { type CSSProperties, useState } from "react";
import { EntityLink } from "../EntityLink.js";

// ---------------------------------------------------------------------------
// Inline style tokens — lean on theme CSS custom properties
// ---------------------------------------------------------------------------

const mono: CSSProperties = { fontFamily: "var(--font-mono, ui-monospace, Menlo, monospace)" };
const muted: CSSProperties = { color: "var(--text-muted, #888)" };

const S = {
	root: { ...mono, fontSize: "12px", lineHeight: "1.5" } satisfies CSSProperties,
	key: { color: "var(--text-muted, #666)", marginRight: "4px" } satisfies CSSProperties,
	string: { color: "var(--text-body, #222)" } satisfies CSSProperties,
	number: { color: "var(--status-ok, #0a7d3d)" } satisfies CSSProperties,
	bool: { color: "var(--accent, #2d6cdf)" } satisfies CSSProperties,
	nullish: { ...muted, fontStyle: "italic" } satisfies CSSProperties,
	bytes: { ...muted } satisfies CSSProperties,
	bracket: muted,
	toggle: {
		cursor: "pointer",
		userSelect: "none",
		color: "var(--text-muted, #888)",
		display: "inline-block",
		width: "12px",
	} satisfies CSSProperties,
	collapsed: {
		...muted,
		cursor: "pointer",
		marginLeft: "4px",
	} satisfies CSSProperties,
	children: {
		marginLeft: "16px",
		borderLeft: "1px dotted var(--surface-line, #e3e3e3)",
		paddingLeft: "8px",
	} satisfies CSSProperties,
	refWrap: { display: "inline-block" } satisfies CSSProperties,
	builtinBadge: {
		fontFamily: "var(--font-mono, ui-monospace, Menlo, monospace)",
		fontSize: "0.9em",
		padding: "1px 4px",
		borderRadius: "var(--radius-sm, 3px)",
		background: "var(--surface-alt, #f1f1f1)",
		color: "var(--text-muted, #888)",
		border: "1px dashed var(--surface-line, #d8d8d8)",
	} satisfies CSSProperties,
};

// ---------------------------------------------------------------------------
// Keyboard-accessible toggle primitives
// ---------------------------------------------------------------------------

const buttonReset: CSSProperties = {
	background: "none",
	border: "none",
	padding: 0,
	font: "inherit",
	color: "inherit",
	textAlign: "left",
};

function ToggleChevron({ collapsed, onToggle }: { collapsed: boolean; onToggle: () => void }) {
	return (
		<button type="button" onClick={onToggle} style={{ ...buttonReset, ...S.toggle }}>
			{collapsed ? "▶" : "▼"}
		</button>
	);
}

function ExpandLabel({
	children,
	onExpand,
}: {
	children: React.ReactNode;
	onExpand: () => void;
}) {
	return (
		<button type="button" onClick={onExpand} style={{ ...buttonReset, ...S.collapsed }}>
			{children}
		</button>
	);
}

// ---------------------------------------------------------------------------
// JsonTree
// ---------------------------------------------------------------------------

export interface JsonTreeProps {
	value: FxtrCbor;
	level?: number;
}

export function JsonTree(props: JsonTreeProps) {
	return (
		<div style={S.root}>
			<Node value={props.value} level={props.level ?? 0} />
		</div>
	);
}

function Node({ value, level }: { value: FxtrCbor; level: number }) {
	if (value === null) return <span style={S.nullish}>null</span>;

	if (typeof value === "boolean") return <span style={S.bool}>{value ? "true" : "false"}</span>;
	if (typeof value === "number") return <span style={S.number}>{String(value)}</span>;
	if (typeof value === "string") return <span style={S.string}>"{value}"</span>;

	if (value instanceof Uint8Array) {
		const hex = Array.from(value.slice(0, 16))
			.map((b) => b.toString(16).padStart(2, "0"))
			.join("");
		const suffix = value.length > 16 ? "…" : "";
		return (
			<span style={S.bytes}>
				bytes({value.length}): {hex}
				{suffix}
			</span>
		);
	}

	if (
		value instanceof RawWeakFxtrID ||
		value instanceof RawStrongFxtrID ||
		value instanceof RawFxtrEntityMarkerID
	) {
		const inner = value.innerId;
		// Contentless IDs ('.' here-node, '!' builtin) aren't loadable — <Render>
		// would throw. Show a plain badge so these round-trip visually without
		// triggering a dispatch.
		const tag = inner.getTag();
		if (tag === "." || tag === "!") {
			return <span style={S.builtinBadge}>{inner.idString()}</span>;
		}
		return (
			<span style={S.refWrap}>
				<EntityLink entity={inner} as="inline" />
			</span>
		);
	}

	if (Array.isArray(value)) return <ArrayNode value={value} level={level} />;

	// Plain object (record of FxtrCbor)
	return <ObjectNode value={value as { readonly [key: string]: FxtrCbor }} level={level} />;
}

function ArrayNode({ value, level }: { value: readonly FxtrCbor[]; level: number }) {
	const [collapsed, setCollapsed] = useState(level > 2);
	if (value.length === 0) return <span style={S.bracket}>[]</span>;
	return (
		<span>
			<ToggleChevron collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
			<span style={S.bracket}>[</span>
			{collapsed ? (
				<>
					<ExpandLabel onExpand={() => setCollapsed(false)}>{value.length} items</ExpandLabel>
					<span style={S.bracket}>]</span>
				</>
			) : (
				<>
					<div style={S.children}>
						{value.map((item, i) => (
							// biome-ignore lint/suspicious/noArrayIndexKey: array is indexed by position
							<div key={i}>
								<span style={S.key}>{i}:</span>
								<Node value={item} level={level + 1} />
								{i < value.length - 1 ? <span style={S.bracket}>,</span> : null}
							</div>
						))}
					</div>
					<span style={S.bracket}>]</span>
				</>
			)}
		</span>
	);
}

function ObjectNode({
	value,
	level,
}: {
	value: { readonly [key: string]: FxtrCbor };
	level: number;
}) {
	const entries = Object.entries(value);
	const [collapsed, setCollapsed] = useState(level > 2);
	if (entries.length === 0) return <span style={S.bracket}>{"{}"}</span>;
	return (
		<span>
			<ToggleChevron collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
			<span style={S.bracket}>{"{"}</span>
			{collapsed ? (
				<>
					<ExpandLabel onExpand={() => setCollapsed(false)}>
						{entries.length} {entries.length === 1 ? "key" : "keys"}
					</ExpandLabel>
					<span style={S.bracket}>{"}"}</span>
				</>
			) : (
				<>
					<div style={S.children}>
						{entries.map(([k, v], i) => (
							<div key={k}>
								<span style={S.key}>{k}:</span>
								<Node value={v} level={level + 1} />
								{i < entries.length - 1 ? <span style={S.bracket}>,</span> : null}
							</div>
						))}
					</div>
					<span style={S.bracket}>{"}"}</span>
				</>
			)}
		</span>
	);
}
