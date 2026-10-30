/**
 * Walk an MDX-compiled tree (the loaded `MdxTree` shape the report
 * entities store in their body fields) and emit React elements.
 *
 * The tree's structural invariant — every position is a primitive
 * leaf or a `[type, props, children]` node tuple — is captured by
 * the recursive `MdxChild` type in `./types.ts`. The renderer
 * dispatches via control-flow narrowing; no runtime tuple
 * predicate needed.
 *
 * Recognized element types:
 *
 * - `"__fragment"` — children are flattened inline. (Producers
 *   shouldn't emit top-level fragments — `evaluate.ts` strips
 *   them — but nested ones are legal.)
 * - HTML / SVG element names — `createElement(type, props, ...children)`.
 * - `"RenderFigure"` — `props.entity` is an `EntityRefStub` /
 *   `ServiceRefStub`; `props.figureKey` is the MDX-assigned key.
 *   Renders `<RenderDispatch as={Figure} stub={entity}
 *   ownStorage={...}/>`, threading per-figure storage projected
 *   through `figureStorage(figureKey)`.
 * - `"RenderLink"` — `props.entity` + optional `props.linkText`.
 *   Renders `<RefLink stub={entity} as="inline" linkText={...}/>`.
 * - Anything else — a small dashed "unregistered component"
 *   placeholder.
 */

import { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { RefLink } from "@fxtr-ui/workstation-dispatch";
import { type ReactNode, createElement } from "react";
import type { FigureStorageLookup } from "./figure_storage.js";
import { Figure } from "./keys.js";
import type { MdxChild, MdxTree } from "./types.js";

// ---- Element type allowlists ----

const HTML_ELEMENTS: ReadonlySet<string> = new Set([
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

const SVG_ELEMENTS: ReadonlySet<string> = new Set([
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

// ---- Props ----

export interface ReportTreeRendererProps {
	/** Wire-decoded body tree from a loaded report entity. */
	readonly document: MdxTree;
	/** Per-figure storage lookup, keyed by the MDX-assigned figureKey. */
	readonly figureStorage: FigureStorageLookup;
}

interface RenderContext {
	readonly figureStorage: FigureStorageLookup;
}

// ---- Top-level renderer ----

export function ReportTreeRenderer(props: ReportTreeRendererProps): ReactNode {
	const ctx: RenderContext = { figureStorage: props.figureStorage };
	return <>{props.document.map((child, i) => renderChild(child, i, ctx))}</>;
}

function renderChild(child: MdxChild, key: number, ctx: RenderContext): ReactNode {
	if (child === null) return null;
	if (typeof child === "boolean") return null;
	if (typeof child === "string" || typeof child === "number") return child;

	// Control-flow narrowing: anything past the primitive arms is
	// necessarily an `MdxNode` tuple `[type, props, children]`.
	const [type, propsObj, children] = child;

	if (type === "__fragment") {
		return children.map((c, i) => renderChild(c, i, ctx));
	}

	if (type === "RenderFigure") {
		return renderFigureNode(propsObj, key, ctx);
	}

	if (type === "RenderLink") {
		return renderLinkNode(propsObj, key);
	}

	if (HTML_ELEMENTS.has(type) || SVG_ELEMENTS.has(type)) {
		const renderedChildren = children.map((c, i) => renderChild(c, i, ctx));
		return createElement(type, { key, ...propsObj }, ...renderedChildren);
	}

	return unknownElementPlaceholder(type, key);
}

// ---- RenderFigure ----

function renderFigureNode(
	propsObj: Record<string, unknown>,
	key: number,
	ctx: RenderContext,
): ReactNode {
	const entity = propsObj.entity;
	const figureKey = propsObj.figureKey;
	if (!(entity instanceof EntityRefStub || entity instanceof ServiceRefStub)) {
		return missingPropPlaceholder("RenderFigure", "entity", key);
	}
	if (typeof figureKey !== "string") {
		return missingPropPlaceholder("RenderFigure", "figureKey", key);
	}
	const ownStorage = ctx.figureStorage(figureKey);
	return <RenderDispatch key={key} stub={entity} as={Figure} ownStorage={ownStorage} />;
}

// ---- RenderLink ----

function renderLinkNode(propsObj: Record<string, unknown>, key: number): ReactNode {
	const entity = propsObj.entity;
	if (!(entity instanceof EntityRefStub || entity instanceof ServiceRefStub)) {
		return missingPropPlaceholder("RenderLink", "entity", key);
	}
	const linkText = propsObj.linkText;
	if (typeof linkText === "string") {
		return <RefLink key={key} stub={entity} as="inline" linkText={linkText} />;
	}
	return <RefLink key={key} stub={entity} as="inline" />;
}

// ---- Placeholders ----

const placeholderStyle: React.CSSProperties = {
	display: "inline-block",
	border: "1px dashed var(--border-subtle, #ccc)",
	padding: "var(--sp-1, 4px)",
	margin: "var(--sp-1, 4px) 0",
	fontFamily: "var(--font-mono)",
	fontSize: "var(--text-caption, 12px)",
	color: "var(--text-muted, #66707a)",
};

function unknownElementPlaceholder(type: string, key: number): ReactNode {
	return (
		<span key={key} style={placeholderStyle}>
			&lt;{type}&gt; (unregistered)
		</span>
	);
}

function missingPropPlaceholder(elementName: string, propName: string, key: number): ReactNode {
	return (
		<span key={key} style={placeholderStyle}>
			&lt;{elementName}&gt; missing or invalid {propName}
		</span>
	);
}
