/**
 * Component tree renderer for the report system.
 *
 * Walks FxtrCbor tuple trees [type, props, children] and produces React elements.
 * Handles:
 * - HTML elements (div, p, h1, etc.)
 * - SVG elements (svg, rect, circle, path, etc.)
 * - Custom report components (RenderFigure, RenderLink)
 * - Fragment unwrapping
 * - Preview target click handling for interactive SVG figures
 */

import { type FxtrCbor, type FxtrID, RawStrongFxtrID, RawWeakFxtrID } from "@fxtr/core";
import { Render } from "@fxtr/react-dispatch";
import { FigureBlock, ReferenceLink } from "@fxtr/reports-react";
import { type ReactNode, Suspense, createElement } from "react";

// ---------------------------------------------------------------------------
// Element type sets
// ---------------------------------------------------------------------------

const HTML_ELEMENTS = new Set([
	"a",
	"abbr",
	"address",
	"article",
	"aside",
	"b",
	"blockquote",
	"br",
	"code",
	"dd",
	"del",
	"details",
	"div",
	"dl",
	"dt",
	"em",
	"figcaption",
	"figure",
	"footer",
	"h1",
	"h2",
	"h3",
	"h4",
	"h5",
	"h6",
	"header",
	"hr",
	"i",
	"img",
	"input",
	"kbd",
	"li",
	"main",
	"mark",
	"nav",
	"ol",
	"p",
	"pre",
	"s",
	"section",
	"small",
	"span",
	"strong",
	"sub",
	"summary",
	"sup",
	"table",
	"tbody",
	"td",
	"tfoot",
	"th",
	"thead",
	"tr",
	"u",
	"ul",
]);

const SVG_ELEMENTS = new Set([
	"svg",
	"g",
	"rect",
	"circle",
	"ellipse",
	"line",
	"polyline",
	"polygon",
	"path",
	"text",
	"tspan",
	"defs",
	"use",
	"clipPath",
	"mask",
	"pattern",
	"linearGradient",
	"radialGradient",
	"stop",
	"filter",
	"feGaussianBlur",
	"feOffset",
	"feMerge",
	"feMergeNode",
	"title",
	"desc",
	"image",
	"foreignObject",
]);

// ---------------------------------------------------------------------------
// Type guard for node tuples
// ---------------------------------------------------------------------------

function isNodeTuple(
	value: FxtrCbor,
): value is readonly [string, Record<string, FxtrCbor>, readonly FxtrCbor[]] {
	return (
		Array.isArray(value) &&
		value.length === 3 &&
		typeof value[0] === "string" &&
		typeof value[1] === "object" &&
		value[1] !== null &&
		!Array.isArray(value[1]) &&
		Array.isArray(value[2])
	);
}

// ---------------------------------------------------------------------------
// Extract FxtrID from a raw ref value
// ---------------------------------------------------------------------------

function extractFxtrId(value: FxtrCbor): FxtrID | null {
	if (value instanceof RawWeakFxtrID) return value.innerId;
	if (value instanceof RawStrongFxtrID) return value.innerId;
	return null;
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ReportTreeRendererProps {
	/** The component tree fragment to render. */
	document: readonly FxtrCbor[];
	/**
	 * Callback when an SVG element with data-preview-target is clicked.
	 * If provided, elements with this attribute become interactive.
	 */
	onPreviewTargetClick?: (targetKey: string) => void;
	/** The currently selected preview target key. The matching SVG element gets an outline. */
	selectedPreviewTarget?: string | undefined;
}

// ---------------------------------------------------------------------------
// Renderer context (threaded through render calls)
// ---------------------------------------------------------------------------

interface RenderContext {
	onPreviewTargetClick: ((targetKey: string) => void) | undefined;
	selectedPreviewTarget: string | undefined;
}

// ---------------------------------------------------------------------------
// Renderer
// ---------------------------------------------------------------------------

export function ReportTreeRenderer({
	document,
	onPreviewTargetClick,
	selectedPreviewTarget,
}: ReportTreeRendererProps) {
	const ctx: RenderContext = { onPreviewTargetClick, selectedPreviewTarget };
	return <>{document.map((child, i) => renderChild(child, i, ctx))}</>;
}

function renderChild(child: FxtrCbor, key: number, ctx: RenderContext): ReactNode {
	if (child == null || typeof child === "boolean") return null;
	if (typeof child === "string" || typeof child === "number") return child;
	if (!isNodeTuple(child)) return null;

	const [type, props, children] = child;

	// Fragment: unwrap children inline
	if (type === "__fragment") {
		return (children as FxtrCbor[]).map((c, i) => renderChild(c, i, ctx));
	}

	// Custom report components
	if (type === "RenderFigure") {
		const entityProp = (props as Record<string, FxtrCbor>).entity;
		const entityId = entityProp ? extractFxtrId(entityProp) : null;
		if (!entityId) return null;
		return (
			<Suspense key={key} fallback={<div>Loading figure...</div>}>
				<Render entity={entityId} as={FigureBlock} />
			</Suspense>
		);
	}

	if (type === "RenderLink") {
		const entityProp = (props as Record<string, FxtrCbor>).entity;
		const entityId = entityProp ? extractFxtrId(entityProp) : null;
		if (!entityId) return null;
		const linkText = (props as Record<string, FxtrCbor>).linkText as string | undefined;
		const extra: { linkText?: string } = {};
		if (linkText !== undefined) {
			extra.linkText = linkText;
		}
		return (
			<Suspense key={key} fallback={<span>...</span>}>
				<Render entity={entityId} as={ReferenceLink} {...extra} />
			</Suspense>
		);
	}

	// HTML elements
	if (HTML_ELEMENTS.has(type)) {
		const renderedChildren = (children as FxtrCbor[]).map((c, i) => renderChild(c, i, ctx));
		return createElement(type, { key, ...(props as Record<string, unknown>) }, ...renderedChildren);
	}

	// SVG elements
	if (SVG_ELEMENTS.has(type)) {
		return renderSvgElement(
			type,
			props as Record<string, FxtrCbor>,
			children as FxtrCbor[],
			key,
			ctx,
		);
	}

	// Unknown element — placeholder
	return (
		<div key={key} style={{ border: "1px dashed #ccc", padding: "4px", margin: "4px 0" }}>
			<em>{type}</em> (unregistered component)
		</div>
	);
}

// ---------------------------------------------------------------------------
// SVG rendering with preview target support
// ---------------------------------------------------------------------------

function renderSvgElement(
	type: string,
	props: Record<string, FxtrCbor>,
	children: FxtrCbor[],
	key: number,
	ctx: RenderContext,
): ReactNode {
	const renderedChildren = children.map((c, i) => renderChild(c, i, ctx));

	const elementProps: Record<string, unknown> = { key };

	// Copy props, converting data-preview-target to interactive + selection styling
	for (const [k, v] of Object.entries(props)) {
		if (k === "data-preview-target" && ctx.onPreviewTargetClick) {
			const targetKey = v as string;
			const isSelected = ctx.selectedPreviewTarget === targetKey;
			elementProps["data-preview-target"] = targetKey;
			elementProps.onClick = (e: React.MouseEvent) => {
				e.stopPropagation();
				ctx.onPreviewTargetClick?.(targetKey);
			};
			elementProps.style = {
				...(elementProps.style as object | undefined),
				cursor: "pointer",
				...(isSelected ? { stroke: "#0066cc", strokeWidth: "3" } : {}),
			};
		} else {
			elementProps[k] = v;
		}
	}

	return createElement(type, elementProps, ...renderedChildren);
}
