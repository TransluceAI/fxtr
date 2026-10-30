/**
 * Runtime install of the externalized-module shim infrastructure used
 * by the dynamic dispatcher.
 *
 * See `projects/fxtr_workstation/plans/phase0/part3_frontend_ui/stage10b_dynamic_dispatch.md`
 * for the design context. The short version:
 *
 *  - A renderer entity's compiled bundle leaves certain bare-specifier
 *    imports (e.g. `import { useAtom } from "jotai"`) unbundled.
 *  - At runtime, those bare specifiers must resolve to the *same*
 *    module instances the surrounding host app uses (instance
 *    equality matters for React hooks, `instanceof` checks on stub
 *    classes, jotai's atom identity, etc.).
 *  - This file's `installFxtrExternals` is what wires the resolution
 *    up: at app startup, the host populates a `window.__FXTR_INJECTED__`
 *    record with its own module instances, and we generate one
 *    blob-URL shim per module that re-exports from that global. An
 *    import map is then injected that maps each bare specifier to its
 *    shim's blob URL. When a renderer is later dynamically imported,
 *    the browser resolves its bare-specifier imports through the
 *    import map, fetches the shim blobs, and ends up with the host's
 *    own module instances.
 *
 * The standard host call shape (per the design plan):
 *
 *     import * as React from "react";
 *     import * as ReactJsxRuntime from "react/jsx-runtime";
 *     // ...
 *     installFxtrExternals({
 *       "react": React,
 *       "react/jsx-runtime": ReactJsxRuntime,
 *       "jotai": Jotai,
 *       // ...
 *     });
 *
 * Hard precondition: `installFxtrExternals` must run **before any
 * dynamic dispatcher entry attempts to load a renderer**. Otherwise
 * the import map element is added after the first dynamic import, and
 * the shim resolution silently breaks. The function does not enforce
 * this; the host is responsible.
 */

declare global {
	interface Window {
		__FXTR_INJECTED__?: Record<string, unknown>;
	}
}

/**
 * Generate the JavaScript source for the shim module that bridges a
 * single externalized module name to its entry in
 * `window.__FXTR_INJECTED__`. Public for testability — the runtime
 * path generates this, blob-URLs it, and registers the URL in the
 * import map.
 *
 * `exportNames` is the list of named exports the *host's* module
 * instance offers at install time, enumerated via `Object.keys(...)`.
 * The shim re-exports them by destructuring, so dynamically-imported
 * renderer code sees the exact same value set the host has. Anything
 * not enumerated at install time is invisible to the renderer — this
 * is a deliberate snapshot, not a live proxy.
 */
export function buildShimSource(moduleName: string, exportNames: readonly string[]): string {
	// Filter `default` out of the named-export list: it goes through
	// the explicit `export default` line below. Trying to also include
	// it via destructuring is a parse error (`default` is a reserved
	// word in that position).
	const named = exportNames.filter((n) => n !== "default" && isValidJsIdentifier(n));
	const moduleNameLiteral = JSON.stringify(moduleName);
	const lines: string[] = [
		`const __mod = (typeof window !== "undefined" ? window.__FXTR_INJECTED__ : undefined)?.[${moduleNameLiteral}];`,
		"if (__mod === undefined) {",
		"  throw new Error(",
		`    "Module " + ${moduleNameLiteral} + " not found in window.__FXTR_INJECTED__. " +`,
		'    "Did the host call installFxtrExternals() before the dynamic dispatcher loaded a renderer?"',
		"  );",
		"}",
		// Default-export: if the module exposes its own `.default`,
		// surface that; otherwise expose the whole module record as
		// the default so `import X from "name"` works for plain
		// namespace-style modules.
		"const __default = __mod.default ?? __mod;",
		"export default __default;",
	];
	if (named.length > 0) {
		lines.push(`export const { ${named.join(", ")} } = __mod;`);
	}
	return `${lines.join("\n")}\n`;
}

/**
 * Permissive ASCII-identifier check — keeps the shim free of named
 * exports whose name would be a parse error in `export const { ... }`.
 * Used as defense in depth; the externalized modules we care about
 * (React, jotai, @fxtr-ui/*) all use plain ASCII identifiers.
 */
function isValidJsIdentifier(name: string): boolean {
	return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name);
}

/**
 * Internal type carrying the assembled artefacts of a single install,
 * exported only for testing: the import-map record (bare-specifier
 * → blob URL) and the per-module shim source strings (keyed by
 * specifier).
 */
export interface FxtrExternalsInstallArtifacts {
	importMap: Record<string, string>;
	shimSources: Record<string, string>;
	blobUrls: Record<string, string>;
}

/**
 * Optional knobs for `installFxtrExternals`.
 *
 * `document` defaults to the global `document`; tests inject a stand-in.
 * `globalTarget` defaults to `window`; tests inject `{}` so the global
 *   doesn't bleed across cases.
 * `createObjectURL` defaults to `URL.createObjectURL`. Surfaced as an
 *   option because jsdom (the test env) doesn't implement it; tests
 *   inject a deterministic stub. Real browsers always have it.
 */
export interface InstallFxtrExternalsOptions {
	readonly document?: Document;
	readonly globalTarget?: { __FXTR_INJECTED__?: Record<string, unknown> };
	readonly createObjectURL?: (blob: Blob) => string;
}

/**
 * Populate `window.__FXTR_INJECTED__`, generate one blob-URL shim per
 * externalized module, and inject a single `<script type="importmap">`
 * element wiring each bare specifier to its shim's blob URL.
 *
 * Returns the installed artefacts so the host (and tests) can inspect
 * them. Once this returns, dynamic-import of code with bare-specifier
 * imports for any of the registered modules will resolve through the
 * shims.
 *
 * **Idempotency / re-install.** Calling twice with the same `modules`
 * mapping is supported in the sense that nothing crashes — but the
 * browser's import-map semantics mean later import-map additions for
 * already-resolved specifiers may be ignored, depending on the
 * browser's multi-import-map support. The recommendation is one
 * install per page load.
 */
export function installFxtrExternals(
	modules: Record<string, unknown>,
	opts: InstallFxtrExternalsOptions = {},
): FxtrExternalsInstallArtifacts {
	const doc = opts.document ?? document;
	const globalTarget =
		opts.globalTarget ?? (globalThis as unknown as { __FXTR_INJECTED__?: Record<string, unknown> });
	const createObjectURL = opts.createObjectURL ?? ((blob) => URL.createObjectURL(blob));

	// 1. Populate the global. Subsequent installs merge their entries
	// in rather than nuking the prior set — handy if the host wants to
	// expand the available externals after initial setup, though the
	// import-map caveat above still applies.
	const existing = globalTarget.__FXTR_INJECTED__ ?? {};
	const merged = { ...existing, ...modules };
	globalTarget.__FXTR_INJECTED__ = merged;

	// 2. For each module, snapshot its keys, build a shim source, and
	// turn that into a blob URL. The snapshot point is now — any
	// later mutations on the module record don't propagate.
	const importMap: Record<string, string> = {};
	const shimSources: Record<string, string> = {};
	const blobUrls: Record<string, string> = {};
	for (const [name, mod] of Object.entries(modules)) {
		const exportNames = mod !== null && typeof mod === "object" ? Object.keys(mod) : [];
		const source = buildShimSource(name, exportNames);
		shimSources[name] = source;
		const blob = new Blob([source], { type: "application/javascript" });
		const url = createObjectURL(blob);
		blobUrls[name] = url;
		importMap[name] = url;
	}

	// 3. Inject the import map. Browsers accept `<script type="importmap">`
	// only if it's added to the document before the first module that
	// would consume it. We append to <head> rather than <body> so an
	// install that runs during HTML parsing (e.g. via a sync script in
	// <head>) lands before any module evaluates.
	const scriptEl = doc.createElement("script");
	scriptEl.type = "importmap";
	scriptEl.textContent = JSON.stringify({ imports: importMap });
	scriptEl.dataset.fxtrExternalsShim = "true";
	(doc.head ?? doc.documentElement).appendChild(scriptEl);

	return { importMap, shimSources, blobUrls };
}
