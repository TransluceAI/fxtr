/**
 * UnknownEntityView — collapsible CBOR tree viewer.
 *
 * Renders raw CBOR as pretty-printed monospace text with collapsible
 * subtrees. FxtrID references are shown as badges that can be expanded
 * to inline-load and display the referenced entity's CBOR tree.
 */

import {
	type EntityRef,
	EntityRef as EntityRefClass,
	type FxtrCbor,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	RawWeakFxtrID,
	type UnknownEntity,
	unknownLoadCodec,
} from "@fxtr/core";
import { useFxtrLoad, useFxtrLoadQuery } from "@fxtr/react";
import { Fragment, useMemo, useState } from "react";

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export function UnknownEntityView({ entityRef }: { entityRef: EntityRef<UnknownEntity> }) {
	const entity = useFxtrLoad(entityRef);
	return (
		<pre className="cbor-tree">
			<CborTree value={entity.raw_cbor} indent={0} />
		</pre>
	);
}

// ---------------------------------------------------------------------------
// CborTree — recursive CBOR renderer
// ---------------------------------------------------------------------------

function pad(indent: number): string {
	return "  ".repeat(indent);
}

function CborTree({ value, indent }: { value: FxtrCbor; indent: number }) {
	if (value === null) return <span className="cbor-null">null</span>;
	if (typeof value === "boolean") return <span className="cbor-bool">{String(value)}</span>;
	if (typeof value === "number") return <span className="cbor-number">{value}</span>;
	if (typeof value === "string") return <span className="cbor-string">"{value}"</span>;
	if (value instanceof Uint8Array) {
		const hex = Array.from(value)
			.map((b) => b.toString(16).padStart(2, "0"))
			.join("");
		return <span className="cbor-bytes">0x{hex}</span>;
	}
	if (
		value instanceof RawWeakFxtrID ||
		value instanceof RawStrongFxtrID ||
		value instanceof RawFxtrEntityMarkerID
	) {
		return <FxtrIdBadge rawRef={value} indent={indent} />;
	}
	if (Array.isArray(value)) {
		return <CollapsibleArray items={value as readonly FxtrCbor[]} indent={indent} />;
	}
	if (typeof value === "object") {
		const entries = Object.entries(value as Record<string, FxtrCbor>);
		return <CollapsibleObject entries={entries} indent={indent} />;
	}
	return <span>{String(value)}</span>;
}

// ---------------------------------------------------------------------------
// Clickable inline toggle (handles a11y keyboard events)
// ---------------------------------------------------------------------------

function Toggle({
	className,
	onClick,
	children,
}: { className: string; onClick: () => void; children: React.ReactNode }) {
	return (
		<button type="button" className={className} onClick={onClick}>
			{children}
		</button>
	);
}

// ---------------------------------------------------------------------------
// Collapsible containers
// ---------------------------------------------------------------------------

function CollapsibleObject({
	entries,
	indent,
}: {
	entries: [string, FxtrCbor][];
	indent: number;
}) {
	const [collapsed, setCollapsed] = useState(false);

	if (entries.length === 0) return <span>{"{}"}</span>;

	if (collapsed) {
		const summary = entries.map(([k]) => k).join(", ");
		return (
			<Toggle className="cbor-toggle" onClick={() => setCollapsed(false)}>
				{`{${summary}}`}
			</Toggle>
		);
	}

	return (
		<>
			<Toggle className="cbor-toggle" onClick={() => setCollapsed(true)}>
				{"{"}
			</Toggle>
			{"\n"}
			{entries.map(([key, val], i) => (
				<Fragment key={key}>
					{pad(indent + 1)}
					<span className="cbor-key">"{key}"</span>: <CborTree value={val} indent={indent + 1} />
					{i < entries.length - 1 ? "," : ""}
					{"\n"}
				</Fragment>
			))}
			{pad(indent)}
			{"}"}
		</>
	);
}

function CollapsibleArray({
	items,
	indent,
}: {
	items: readonly FxtrCbor[];
	indent: number;
}) {
	const [collapsed, setCollapsed] = useState(false);

	if (items.length === 0) return <span>[]</span>;

	if (collapsed) {
		return (
			<Toggle className="cbor-toggle" onClick={() => setCollapsed(false)}>
				[{items.length} items]
			</Toggle>
		);
	}

	return (
		<>
			<Toggle className="cbor-toggle" onClick={() => setCollapsed(true)}>
				[
			</Toggle>
			{"\n"}
			{items.map((item, i) => (
				// biome-ignore lint/suspicious/noArrayIndexKey: CBOR arrays have no stable keys
				<Fragment key={i}>
					{pad(indent + 1)}
					<CborTree value={item} indent={indent + 1} />
					{i < items.length - 1 ? "," : ""}
					{"\n"}
				</Fragment>
			))}
			{pad(indent)}]
		</>
	);
}

// ---------------------------------------------------------------------------
// FxtrID badges — click to expand and inline-load the referenced entity
// ---------------------------------------------------------------------------

function FxtrIdBadge({
	rawRef,
	indent,
}: {
	rawRef: RawWeakFxtrID | RawStrongFxtrID | RawFxtrEntityMarkerID;
	indent: number;
}) {
	const [expanded, setExpanded] = useState(false);
	const id = rawRef.innerId;
	const idStr = id.idString();
	const tagLabel =
		rawRef instanceof RawStrongFxtrID
			? "strong"
			: rawRef instanceof RawFxtrEntityMarkerID
				? "marker"
				: "weak";

	// Contentless IDs (here-node "." and builtin "!") can't be loaded.
	const isContentless = idStr.startsWith(".") || idStr.startsWith("!");
	const label = `[${tagLabel}: ${idStr}]`;

	const ref = useMemo(() => new EntityRefClass(id, unknownLoadCodec), [id]);
	const { data, loading, error } = useFxtrLoadQuery(ref, { enabled: expanded && !isContentless });

	if (isContentless) {
		return <span className="fxtr-badge">{label}</span>;
	}

	if (!expanded) {
		return (
			<Toggle className="fxtr-badge" onClick={() => setExpanded(true)}>
				{label}
			</Toggle>
		);
	}

	return (
		<span>
			<Toggle className="fxtr-badge" onClick={() => setExpanded(false)}>
				{label}
			</Toggle>
			{" \u2192 "}
			{loading && <span className="loading">loading...</span>}
			{error != null && <span className="error">error: {String(error)}</span>}
			{data && <CborTree value={data.raw_cbor} indent={indent} />}
		</span>
	);
}
