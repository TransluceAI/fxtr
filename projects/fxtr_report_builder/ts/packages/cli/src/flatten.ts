/**
 * Stage 2: flatten the intermediate `MdxNode` tree into
 * cbor-serializable tuples of the form `[type, props, children]`.
 *
 * During this pass, `RenderFigure` nodes additionally get a
 * stable `figureKey: "figure-<i>"` injected into their props
 * (counter scoped to one `flatten` call). The frontend tree
 * renderer uses that key to project per-figure storage out of
 * the report's `FullPanel` `ownStorage` atom.
 *
 * `RenderLink` nodes carry no state and don't get a key.
 */

import type { CborProp, MdxChild } from "./jsx_runtime.js";
import { isMdxNode } from "./jsx_runtime.js";

/** A flattened tuple. */
export type FlatTuple = readonly [string, Record<string, CborProp>, readonly FlatChild[]];

/** A leaf or sub-tuple in the flattened tree. */
export type FlatChild = FlatTuple | string | number | boolean | null;

interface FlattenContext {
	nextFigureCounter: number;
}

function injectFigureKey(
	type: string,
	props: Record<string, CborProp>,
	ctx: FlattenContext,
): Record<string, CborProp> {
	if (type !== "RenderFigure") return props;
	const key = `figure-${ctx.nextFigureCounter}`;
	ctx.nextFigureCounter += 1;
	// Honor an explicit key if the MDX source supplied one — the
	// auto-counter is the fallback for un-keyed figures. (Stage 1
	// scope: explicit naming isn't documented; this is a hatch for
	// future use.)
	if (typeof props.figureKey === "string") return props;
	return { ...props, figureKey: key };
}

async function flattenChild(
	child: MdxChild | Promise<MdxChild>,
	ctx: FlattenContext,
): Promise<FlatChild> {
	const resolved = await child;

	if (isMdxNode(resolved)) {
		const children: FlatChild[] = await Promise.all(
			resolved.children.map((c) => flattenChild(c, ctx)),
		);
		const props = injectFigureKey(resolved.type, resolved.props, ctx);
		return [resolved.type, props, children] as const;
	}

	return resolved;
}

/**
 * Flatten an `MdxNode` tree to its cbor-serializable form.
 * Resolves async components along the way.
 */
export async function flatten(root: MdxChild | Promise<MdxChild>): Promise<FlatChild> {
	const ctx: FlattenContext = { nextFigureCounter: 0 };
	return flattenChild(root, ctx);
}

/**
 * Unwrap the top-level `__fragment` (which MDX always wraps its
 * output in) so the returned value is always an array of
 * document-level children — never a single fragment node.
 *
 * - Fragment input → its children array.
 * - Non-fragment input → wrapped in a single-element array.
 *
 * Encoding this shape means downstream consumers (Python, the
 * frontend tree renderer) can treat the entity's body field as a
 * plain `readonly unknown[]` without runtime pattern-matching on
 * fragment-shape.
 */
export function extractFragmentChildren(tree: FlatChild): FlatChild[] {
	if (Array.isArray(tree) && tree[0] === "__fragment") {
		const children = tree[2];
		if (Array.isArray(children)) {
			return [...children];
		}
	}
	return [tree];
}
