/**
 * Recursive renderer: walks a FxtrCbor component tree and produces React elements.
 *
 * - Primitives (strings, numbers) render as text.
 * - Node tuples [type, props, children] render as elements.
 * - Standard HTML types (h1, p, div, etc.) render as native elements.
 * - Custom types (Chart, FxtrStats, etc.) look up the client component registry.
 * - Unknown custom types render a generic placeholder.
 */

import type { FxtrCbor } from "@fxtr/core";
import { type ReactNode, createElement } from "react";
import { clientComponents } from "./components.js";
import { isNodeTuple } from "./types.js";

/** HTML element types that we pass through to React's createElement directly. */
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

function renderChild(child: FxtrCbor, key: number): ReactNode {
	if (child == null || typeof child === "boolean") return null;
	if (typeof child === "string" || typeof child === "number") return child;

	if (!isNodeTuple(child)) return null;

	const [type, props, children] = child;

	// __fragment: just render children inline
	if (type === "__fragment") {
		return (children as FxtrCbor[]).map(renderChild);
	}

	// Standard HTML element
	if (HTML_ELEMENTS.has(type)) {
		const renderedChildren = (children as FxtrCbor[]).map(renderChild);
		return createElement(type, { key, ...props }, ...renderedChildren);
	}

	// Client component from registry
	const ClientComp = clientComponents[type];
	if (ClientComp) {
		return (
			<ClientComp
				key={key}
				type={type}
				props={props}
				jsonChildren={children}
				renderChild={renderChild}
			/>
		);
	}

	// Unknown custom type: generic placeholder
	return (
		<div key={key} className="client-component client-unknown">
			<div className="client-component-label">{type} (unregistered)</div>
			<div className="client-component-props">
				<pre>{JSON.stringify(props, null, 2)}</pre>
			</div>
		</div>
	);
}

/**
 * Render a FxtrCbor component tree (array of document children) as React elements.
 */
export function FxtrTreeRenderer({ document }: { document: readonly FxtrCbor[] }) {
	return <>{document.map(renderChild)}</>;
}
