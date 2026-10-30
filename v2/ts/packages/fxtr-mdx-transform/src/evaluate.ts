/**
 * Dynamic MDX evaluation — the non-vite path.
 *
 * Compiles MDX source at runtime using @mdx-js/mdx's compile + run,
 * then executes the result with our custom JSX runtime to produce FxtrCbor nodes.
 */

import type { FxtrCbor } from "@fxtr/core";
import { compile, run } from "@mdx-js/mdx";
import { extractFragmentChildren, flatten } from "./flatten.js";
import type { Component } from "./jsx-runtime.js";
import * as jsxRuntime from "./jsx-runtime.js";

export interface EvaluateOptions {
	/** Custom components to make available in the MDX */
	components?: Record<string, Component>;
	/** Additional props passed to the MDX content function, accessible as `props.xxx` in MDX.
	 *  Pass `fxtr_scope` here if your components need scope access. */
	props?: Record<string, unknown>;
}

/**
 * Compile and evaluate an MDX string, returning a list of FxtrCbor document nodes.
 * The top-level MDX fragment is automatically unwrapped.
 */
export async function evaluateMdx(source: string, options: EvaluateOptions): Promise<FxtrCbor[]> {
	const compiled = await compile(source, {
		outputFormat: "function-body",
	});

	const { default: MdxContent } = await run(String(compiled), {
		...jsxRuntime,
		baseUrl: import.meta.url,
	});

	const rawTree = MdxContent({
		...options.props,
		components: options.components ?? {},
	});
	const flattened = await flatten(rawTree);
	return extractFragmentChildren(flattened);
}
