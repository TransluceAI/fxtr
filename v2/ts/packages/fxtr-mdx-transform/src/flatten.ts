/**
 * Stage 2: Flatten the intermediate MdxNode tree into FxtrCbor tuples,
 * resolving any promises (from async components) along the way.
 *
 * Output format: [type, props, children] where:
 *   - type: string
 *   - props: { [key: string]: FxtrCbor } — may contain RawWeakFxtrID values
 *   - children: FxtrCbor[] — restricted to primitives and node tuples (no raw FxtrIDs)
 *
 * MdxNode { type, props, children } → readonly [string, {...}, [...]]
 */

import type { FxtrCbor } from "@fxtr/core";
import { type MdxChild, isMdxNode } from "./jsx-runtime.js";

async function flattenChild(child: MdxChild | Promise<MdxChild>): Promise<FxtrCbor> {
	const resolved = await child;

	if (isMdxNode(resolved)) {
		const children: FxtrCbor[] = await Promise.all(resolved.children.map(flattenChild));
		return [resolved.type, resolved.props, children];
	}

	// Primitive: string, number, boolean, null — all valid FxtrCbor
	return resolved;
}

/**
 * Flatten an MdxNode tree into FxtrCbor tuples, resolving async nodes.
 * Returns the raw flattened tree (may be a __fragment wrapper).
 */
export async function flatten(root: MdxChild | Promise<MdxChild>): Promise<FxtrCbor> {
	return flattenChild(root);
}

/**
 * Extract the children from a top-level __fragment node.
 * If the tree isn't a fragment, wraps it in a single-element array.
 *
 * MDX always compiles to a top-level fragment, so this gives you
 * the list of document-level nodes directly.
 */
export function extractFragmentChildren(tree: FxtrCbor): FxtrCbor[] {
	if (Array.isArray(tree) && tree[0] === "__fragment") {
		const children = tree[2];
		if (Array.isArray(children)) return children as FxtrCbor[];
	}
	return [tree];
}
