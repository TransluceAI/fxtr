/**
 * @vitest-environment jsdom
 *
 * Tests for `installFxtrExternals` and `buildShimSource` from
 * `@fxtr-ui/react-dispatch-dynamic-shim`.
 *
 * Lives in this sibling tests-only package (not next to the source)
 * because of the documented vitest+jsdom loading issue under
 * `projects/fxtr_workstation/frontend_lib/`. See the README for the
 * full story.
 *
 * Three layers of coverage:
 *
 * 1. `buildShimSource` — pure string generation; verified by
 *    source-structure assertions.
 * 2. `installFxtrExternals` — DOM + global side effects; verified by
 *    inspecting `window.__FXTR_INJECTED__`, the injected import-map
 *    element, and the structure of the returned artefacts.
 * 3. Runtime resolution — fetch the generated blob URL and evaluate
 *    the shim source against a mock global to verify the shim
 *    correctly re-exposes the host's module instance. We don't
 *    exercise the actual import-map+dynamic-import resolution here
 *    because jsdom doesn't honour browser import maps; the live
 *    demo's browser-side integration test covers that
 *    (TODO(stage10b chunk 7): wire the demo + assertion).
 */

import { afterEach, describe, expect, test } from "vitest";

import { buildShimSource, installFxtrExternals } from "@fxtr-ui/react-dispatch-dynamic-shim";

describe("buildShimSource", () => {
	test("emits a guard, default export, and named-export destructuring", () => {
		const source = buildShimSource("my-mod", ["foo", "bar", "default"]);
		// Guard against missing __FXTR_INJECTED__.
		expect(source).toContain("__FXTR_INJECTED__");
		expect(source).toContain('"my-mod"');
		expect(source).toContain("throw new Error");
		// `default` is routed through `export default`, not the
		// destructuring (which would be a syntax error).
		expect(source).toContain("export default __default");
		expect(source).toContain("export const { foo, bar }");
		expect(source).not.toMatch(/export const \{[^}]*default[^}]*\}/);
	});

	test("module with no own .default still works (falls back to the module record)", () => {
		const source = buildShimSource("ns-only", ["a", "b"]);
		expect(source).toContain("const __default = __mod.default ?? __mod");
		expect(source).toContain("export const { a, b }");
	});

	test("module with no named exports omits the destructuring line", () => {
		const source = buildShimSource("default-only", ["default"]);
		expect(source).toContain("export default __default");
		// No `export const { ... }` line.
		expect(source).not.toMatch(/export const \{/);
	});

	test("filters out invalid JS identifier names (defense in depth)", () => {
		const source = buildShimSource("weird", [
			"valid",
			"with-dash",
			"123starts-with-digit",
			"default",
		]);
		expect(source).toContain("export const { valid }");
		expect(source).not.toContain("with-dash");
		expect(source).not.toContain("123starts-with-digit");
	});
});

describe("installFxtrExternals", () => {
	const installedDocs: Document[] = [];
	let blobUrlCounter = 0;
	// Deterministic stub for URL.createObjectURL — jsdom doesn't
	// implement it, and we don't need the real thing in unit tests
	// (the blob-URL → dynamic-import dance is browser-only and
	// belongs in the live demo's integration test —
	// TODO(stage10b chunk 7) wire that up).
	const stubCreateObjectURL = (_blob: Blob): string => `blob:stub-${blobUrlCounter++}`;

	afterEach(() => {
		for (const doc of installedDocs) {
			for (const el of Array.from(doc.querySelectorAll("script[data-fxtr-externals-shim]"))) {
				el.remove();
			}
		}
		installedDocs.length = 0;
		// Reset the global via a loose cast — assigning through the
		// strict optional type would violate exactOptionalPropertyTypes;
		// `delete` would trip biome's noDelete; this gets through both.
		(globalThis as Record<string, unknown>).__FXTR_INJECTED__ = undefined;
		blobUrlCounter = 0;
	});

	test("populates the global with the supplied modules", () => {
		installedDocs.push(document);
		const artifacts = installFxtrExternals(
			{ "my-pkg": { foo: 1, bar: 2 } },
			{ createObjectURL: stubCreateObjectURL },
		);
		const injected = (globalThis as { __FXTR_INJECTED__?: Record<string, unknown> })
			.__FXTR_INJECTED__;
		expect(injected).toBeDefined();
		expect(injected?.["my-pkg"]).toEqual({ foo: 1, bar: 2 });
		expect(Object.keys(artifacts.importMap)).toEqual(["my-pkg"]);
	});

	test("injects a single <script type='importmap'> mapping each module to a blob URL", () => {
		installedDocs.push(document);
		installFxtrExternals(
			{ "pkg-a": { x: 1 }, "pkg-b": { y: 2 } },
			{ createObjectURL: stubCreateObjectURL },
		);
		const scripts = Array.from(
			document.querySelectorAll<HTMLScriptElement>("script[data-fxtr-externals-shim]"),
		);
		expect(scripts).toHaveLength(1);
		const scriptEl = scripts[0];
		if (scriptEl === undefined) {
			throw new Error("unreachable: length asserted above");
		}
		const importMap = JSON.parse(scriptEl.textContent ?? "{}");
		expect(Object.keys(importMap.imports).sort()).toEqual(["pkg-a", "pkg-b"]);
		// Each entry should look like a blob URL from our stub.
		for (const url of Object.values(importMap.imports) as string[]) {
			expect(url.startsWith("blob:")).toBe(true);
		}
	});

	test("shim source re-exports the host's module instance when evaluated", () => {
		installedDocs.push(document);
		const hostModule = { foo: 42, default: "host-default" };
		const artifacts = installFxtrExternals(
			{ "my-pkg": hostModule },
			{ createObjectURL: stubCreateObjectURL },
		);
		const text = artifacts.shimSources["my-pkg"];
		if (text === undefined) {
			throw new Error("unreachable: install put my-pkg in shimSources");
		}

		// Evaluate the shim manually with a stand-in window — we
		// can't `import(blobUrl)` in jsdom (no native ESM loader for
		// blob: URLs), but executing the source against an injected
		// window proves the re-export logic itself is right.
		const fakeWindow = { __FXTR_INJECTED__: { "my-pkg": hostModule } };
		// The shim uses `window.__FXTR_INJECTED__`; rewrite to read
		// the variable we want to inject. This is brittle by design —
		// the test would catch a refactor that changes the shim's
		// public read-shape.
		// Pull the named-export name list out of the source so we
		// build a stringly-keyed lookup rather than treating the names
		// as references to JS variables.
		const namedMatch = text.match(/export const \{ ([^}]+) \} = __mod;/);
		const namedExportListLiteral = JSON.stringify(
			(namedMatch?.[1] ?? "").split(",").map((s) => s.trim()),
		);
		const callable = new Function(
			"window",
			`
			const _exports = {};
			let _default;
			${text
				.replace(/export default __default;/, "_default = __default;")
				.replace(
					/export const \{ ([^}]+) \} = __mod;/,
					`for (const name of ${namedExportListLiteral}) { _exports[name] = __mod[name]; }`,
				)}
			return { _default, ..._exports };
			`,
		);
		const result = callable(fakeWindow);
		expect(result._default).toBe("host-default");
		expect(result.foo).toBe(42);
	});

	test("missing __FXTR_INJECTED__ at shim eval time throws a clear error", () => {
		installedDocs.push(document);
		const artifacts = installFxtrExternals(
			{ "my-pkg": { foo: 1 } },
			{ createObjectURL: stubCreateObjectURL },
		);
		const text = artifacts.shimSources["my-pkg"];
		if (text === undefined) {
			throw new Error("unreachable: install put my-pkg in shimSources");
		}
		// Evaluate against a window with NO __FXTR_INJECTED__ — should throw.
		const callable = new Function(
			"window",
			text.replace(/export default __default;/, "").replace(/export const \{[^}]+\} = __mod;/, ""),
		);
		// The error message reads e.g. `Module my-pkg not found ...`
		// — string-concatenated at runtime, so the JSON-quoted source
		// literal doesn't end up surrounded by quotes in the rendered
		// message.
		expect(() => callable({})).toThrow(/Module my-pkg not found/);
	});

	test("merges into an existing __FXTR_INJECTED__ rather than replacing it", () => {
		installedDocs.push(document);
		(globalThis as { __FXTR_INJECTED__?: Record<string, unknown> }).__FXTR_INJECTED__ = {
			"pre-existing": { z: 99 },
		};
		installFxtrExternals({ "new-pkg": { a: 1 } }, { createObjectURL: stubCreateObjectURL });
		const injected = (globalThis as { __FXTR_INJECTED__?: Record<string, unknown> })
			.__FXTR_INJECTED__;
		expect(injected?.["pre-existing"]).toEqual({ z: 99 });
		expect(injected?.["new-pkg"]).toEqual({ a: 1 });
	});
});
