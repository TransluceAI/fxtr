/**
 * Custom JSX runtime for MDX-to-FxtrCbor compilation.
 *
 * Forked-down from `@fxtr/mdx-transform/jsx-runtime.ts`. The fork
 * strips the dependency on `@fxtr/core` (and the
 * `RawWeakFxtrID` / `RawStrongFxtrID` / `RawFxtrEntityMarkerID`
 * wrappers) and replaces them with `cbor-x` `Tag` instances —
 * which is how Python ships ref-typed inputs across the
 * subprocess boundary as opaque CBOR-tagged values.
 *
 * The runtime produces `MdxNode` instances which a subsequent
 * flatten pass converts to `[type, props, children]` tuples.
 *
 * Two entry points (`jsx`, `jsxs`) match `@mdx-js/mdx`'s
 * automatic runtime contract. A third (`jsxDEV`) covers the
 * dev-mode form.
 */

import { Tag } from "cbor-x";

const MDX_NODE_BRAND = Symbol.for("fxtr.report_builder.mdx.node");

/** Any value the JSX runtime considers serializable as a prop. */
export type CborProp =
	| null
	| boolean
	| number
	| string
	| Uint8Array
	| Tag
	| readonly CborProp[]
	| { readonly [key: string]: CborProp };

/**
 * The intermediate representation produced by the JSX runtime.
 * Branded so it can't be confused with plain values.
 */
export class MdxNode {
	readonly [MDX_NODE_BRAND] = true;
	constructor(
		readonly type: string,
		readonly props: Record<string, CborProp>,
		readonly children: MdxChild[],
	) {}
}

export type MdxChild = MdxNode | string | number | boolean | null;

export function isMdxNode(value: unknown): value is MdxNode {
	return typeof value === "object" && value !== null && MDX_NODE_BRAND in value;
}

/**
 * Component function — receives all props (including children +
 * any non-cbor runtime values supplied via `evaluateMdx`'s
 * `props`) and returns an `MdxChild` subtree.
 */
export type Component = (props: Record<string, unknown>) => MdxChild | Promise<MdxChild>;

/**
 * Create a component that passes through to the output tree as a
 * custom node. Used for `RenderFigure` / `RenderLink` (and any
 * other dispatch-shaped names the caller wires up).
 *
 * All props must be cbor-serializable — throws otherwise.
 * `children` is routed to the node's children array.
 */
export function passthroughCustomComponent(typeName: string): Component {
	return (props: Record<string, unknown>) => {
		const { children: rawChildren, ...rest } = props;
		const cborProps = assertCborProps(rest);
		const children = normalizeChildren(rawChildren);
		return new MdxNode(typeName, cborProps, children);
	};
}

// ---- Cbor-serializable validation ----

export function isCborSerializable(value: unknown): value is CborProp {
	if (
		value === null ||
		typeof value === "string" ||
		typeof value === "number" ||
		typeof value === "boolean"
	) {
		return true;
	}
	if (value instanceof Uint8Array) return true;
	if (value instanceof Tag) return true;
	if (Array.isArray(value)) return value.every(isCborSerializable);
	if (typeof value === "object") {
		return Object.values(value as Record<string, unknown>).every(isCborSerializable);
	}
	return false;
}

export function assertCborProps(props: Record<string, unknown>): Record<string, CborProp> {
	for (const [key, value] of Object.entries(props)) {
		if (!isCborSerializable(value)) {
			throw new Error(
				`Prop "${key}" is not a valid cbor-serializable value (got ${typeof value}). ` +
					"Non-serializable props should only be passed to component functions, not HTML elements.",
			);
		}
	}
	return props as Record<string, CborProp>;
}

// ---- Fragment sentinel + JSX factory ----

export const Fragment = Symbol.for("fxtr.report_builder.mdx.fragment");

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

	if (type === Fragment) {
		if (children.length === 1) return children[0] as MdxChild;
		return new MdxNode("__fragment", {}, children);
	}

	if (typeof type === "string") {
		return new MdxNode(type, assertCborProps(restProps), children);
	}

	if (typeof type === "function") {
		return type({ ...restProps, children });
	}

	throw new Error(`Unexpected JSX element type: ${String(type)}`);
}

export function jsx(type: ElementType, props: JsxProps): MdxChild | Promise<MdxChild> {
	return createNode(type, props);
}

export function jsxs(type: ElementType, props: JsxProps): MdxChild | Promise<MdxChild> {
	return createNode(type, props);
}

export function jsxDEV(type: ElementType, props: JsxProps): MdxChild | Promise<MdxChild> {
	return createNode(type, props);
}
