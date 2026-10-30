/**
 * Custom JSX runtime for MDX-to-JSON compilation.
 *
 * Instead of producing React elements, this runtime produces an intermediate
 * tree of MdxNode objects. These are then flattened into FxtrCbor tuples
 * by a separate post-processing pass (see flatten.ts).
 *
 * This module is consumed two ways:
 * 1. Build mode: jsxImportSource points here, so compiled MDX imports jsx/jsxs/Fragment.
 * 2. Dynamic mode: passed directly to @mdx-js/mdx's run() as the runtime.
 */

import { type FxtrCbor, RawFxtrEntityMarkerID, RawStrongFxtrID, RawWeakFxtrID } from "@fxtr/core";

// ---- Intermediate node type (stage 1) ----

const MDX_NODE_BRAND = Symbol.for("fxtr.mdx.node");

/**
 * The intermediate representation produced by the JSX runtime.
 * Branded with a symbol so it can't be confused with plain objects or arrays.
 *
 * Props are typed as Record<string, FxtrCbor> — only serializable values are
 * allowed. Runtime-only values (fxtr_scope, children) are stripped before
 * constructing the node.
 */
export class MdxNode {
	readonly [MDX_NODE_BRAND] = true;
	constructor(
		readonly type: string,
		readonly props: Record<string, FxtrCbor>,
		readonly children: MdxChild[],
	) {}
}

export type MdxChild = MdxNode | string | number | boolean | null;

export function isMdxNode(value: unknown): value is MdxNode {
	return typeof value === "object" && value !== null && MDX_NODE_BRAND in value;
}

/**
 * A component function that can be used in MDX.
 * It receives all props (including children, fxtr_scope, etc.) as unknown,
 * and returns an intermediate subtree.
 */
export type Component = (props: Record<string, unknown>) => MdxChild | Promise<MdxChild>;

/**
 * Create a component that passes through to the client as a custom node.
 *
 * All props must be FxtrCbor-serializable — throws if any prop is not.
 * `children` is routed to the node's children array (normalized from MDX child format).
 *
 * Use this for custom components whose rendering is handled client-side
 * (e.g. by a tree renderer that recognizes the type name).
 */
export function passthroughCustomComponent(typeName: string): Component {
	return (props: Record<string, unknown>) => {
		const { children: rawChildren, ...rest } = props;
		const cborProps = assertFxtrCborProps(rest);
		const children = normalizeChildren(rawChildren);
		return new MdxNode(typeName, cborProps, children);
	};
}

// ---- FxtrCbor validation ----

export function isFxtrCbor(value: unknown): value is FxtrCbor {
	if (
		value === null ||
		typeof value === "string" ||
		typeof value === "number" ||
		typeof value === "boolean"
	) {
		return true;
	}
	if (value instanceof Uint8Array) return true;
	if (
		value instanceof RawWeakFxtrID ||
		value instanceof RawStrongFxtrID ||
		value instanceof RawFxtrEntityMarkerID
	) {
		return true;
	}
	if (Array.isArray(value)) return value.every(isFxtrCbor);
	if (typeof value === "object") {
		return Object.values(value).every(isFxtrCbor);
	}
	return false;
}

/**
 * Assert that all prop values are FxtrCbor. Throws if any value isn't serializable.
 * This should never fail for HTML element props — if it does, it's a bug
 * (e.g. fxtr_scope was passed to a raw element instead of a component).
 */
export function assertFxtrCborProps(props: Record<string, unknown>): Record<string, FxtrCbor> {
	for (const [key, value] of Object.entries(props)) {
		if (!isFxtrCbor(value)) {
			throw new Error(
				`Prop "${key}" is not a valid FxtrCbor value (got ${typeof value}). Non-serializable props like fxtr_scope should only be passed to component functions, not HTML elements.`,
			);
		}
	}
	return props as Record<string, FxtrCbor>;
}

// ---- Fragment sentinel ----

export const Fragment = Symbol.for("fxtr.mdx.fragment");

// ---- Core JSX factory ----

type ElementType = string | typeof Fragment | Component;

interface JsxProps {
	children?: MdxChild | MdxChild[];
	[key: string]: unknown;
}

function normalizeChildren(raw: unknown): MdxChild[] {
	if (raw == null || raw === false || raw === true) return [];
	if (isMdxNode(raw)) return [raw];
	if (Array.isArray(raw)) return raw.flatMap(normalizeChildren);
	return [raw as MdxChild];
}

function createNode(type: ElementType, props: JsxProps): MdxChild | Promise<MdxChild> {
	const { children: rawChildren, ...restProps } = props;
	const children = normalizeChildren(rawChildren);

	// Fragment: just return children (unwrap single child)
	if (type === Fragment) {
		if (children.length === 1) return children[0] as MdxChild;
		return new MdxNode("__fragment", {}, children);
	}

	// String type: built-in HTML element — validate and filter props to FxtrCbor
	if (typeof type === "string") {
		return new MdxNode(type, assertFxtrCborProps(restProps), children);
	}

	// Function type: custom component → call it as a server macro
	// Components receive all props (including non-serializable ones like fxtr_scope)
	if (typeof type === "function") {
		return type({ ...restProps, children });
	}

	throw new Error(`Unexpected JSX element type: ${String(type)}`);
}

/**
 * JSX automatic runtime entry point (single child).
 */
export function jsx(type: ElementType, props: JsxProps): MdxChild | Promise<MdxChild> {
	return createNode(type, props);
}

/**
 * JSX automatic runtime entry point (multiple static children).
 */
export function jsxs(type: ElementType, props: JsxProps): MdxChild | Promise<MdxChild> {
	return createNode(type, props);
}

/**
 * Development-mode JSX entry point. Vite uses this in dev mode.
 */
export function jsxDEV(type: ElementType, props: JsxProps): MdxChild | Promise<MdxChild> {
	return createNode(type, props);
}
