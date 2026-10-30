/**
 * Compile + run + flatten an MDX source string.
 *
 * Forked from `@fxtr/mdx-transform/evaluate.ts`. Uses `@mdx-js/mdx`'s
 * `compile` + `run` against our custom JSX runtime to produce
 * `MdxNode`s, then `flatten()` reduces them to cbor-serializable
 * tuples (with `figureKey` assignment for `RenderFigure` nodes).
 */

import { compile, run } from "@mdx-js/mdx";
import type { FlatChild } from "./flatten.js";
import { extractFragmentChildren, flatten } from "./flatten.js";
import type { Component } from "./jsx_runtime.js";
import * as jsxRuntime from "./jsx_runtime.js";

export interface EvaluateOptions {
	/** Custom JSX components — typically the passthrough ones. */
	components?: Record<string, Component>;
	/** Extra props passed to the MDX content function, accessible as
	 *  `props.<name>` inside the MDX source. Our convention is to
	 *  put ref-typed inputs under `props.fxtr_inputs.<name>`. */
	props?: Record<string, unknown>;
}

/** Compile + run + flatten + extract fragment children. Returns the
 *  document-level children as a flat array. The top-level
 *  `__fragment` wrapper that MDX always emits is stripped so
 *  consumers receive a uniform array-of-nodes shape. */
export async function evaluateMdx(
	source: string,
	options: EvaluateOptions = {},
): Promise<FlatChild[]> {
	const compiled = await compile(source, {
		outputFormat: "function-body",
	});

	const runtime = jsxRuntime as unknown as Parameters<typeof run>[1];
	const { default: MdxContent } = await run(String(compiled), {
		...runtime,
		// Placeholder URL — we don't resolve imports inside MDX, so
		// this is just a value that satisfies @mdx-js/mdx's signature.
		baseUrl: "file:///fxtr-report-builder-cli/",
	});

	const rawTree = MdxContent({
		...options.props,
		components: options.components ?? {},
	});
	return extractFragmentChildren(await flatten(rawTree));
}
