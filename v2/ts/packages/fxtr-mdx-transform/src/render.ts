/**
 * Shared rendering logic: takes a compiled MDX module's default export,
 * calls it with components and additional props, and flattens to FxtrCbor.
 *
 * Props are passed directly to the MDX content function and accessible as
 * `props.xxx` in MDX. Pass `fxtr_scope` here if your components need scope access.
 */

import type { FxtrCbor } from "@fxtr/core";
import { extractFragmentChildren, flatten } from "./flatten.js";
import type { Component, MdxChild } from "./jsx-runtime.js";

/**
 * An MDX module's default export — a function that takes props and returns a tree.
 */
export type MdxContentFn = (props: Record<string, unknown>) => MdxChild | Promise<MdxChild>;

/**
 * Render an MDX content function to a list of FxtrCbor document nodes.
 * The top-level MDX fragment is automatically unwrapped.
 */
export async function renderMdxToFxtr(
	Content: MdxContentFn,
	options?: {
		components?: Record<string, Component>;
		props?: Record<string, unknown>;
	},
): Promise<FxtrCbor[]> {
	const rawTree = Content({
		...options?.props,
		components: options?.components ?? {},
	});
	const flattened = await flatten(rawTree);
	return extractFragmentChildren(flattened);
}
