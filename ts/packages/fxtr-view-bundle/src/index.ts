// The Vite preset for a project's renderer package: `vite build --configLoader runner` writes
// the package's bundle (see fxtr-view's `view_bundle.ts` for the contract). A renderer package's
// `vite.config.ts` is one line:
//
//     export default defineViewBundleConfig({ entry: "src/index.ts" });
//
// where the entry's default export is the package's `ViewBundle`. The bundle is built into the
// package's `dist/` (`VIEW_BUNDLE_OUT_DIR`), which is where whatever serves it reads it, or into
// the directory `VIEW_BUNDLE_OUT_DIR_ENV` names: `fxtr views build` builds into a scratch directory
// that way and moves the result into `dist/` only once it checks out.
//
// How the factory is made. Vite (Rollup) builds the entry as an IIFE whose arguments are the
// shared modules, named by `output.globals`. This preset's plugin then wraps the emitted code in the
// default-exported factory, which binds those names from its `shared` argument and returns the
// entry's default export, so the IIFE's references to what would be globals resolve to the
// factory's locals and the result is an ordinary ES module the shell `import()`s and calls with its
// own instances. (Rollup's `banner` and `footer` cannot do the wrapping: Vite places a lib IIFE's
// banner inside the function.) The stylesheet is whatever CSS the package imports, extracted by
// Vite; the plugin writes the manifest last, naming the files emitted.

import {
  SHARED_MODULES,
  VIEW_API_VERSION,
  VIEW_BUNDLE_MANIFEST_FILENAME,
  VIEW_BUNDLE_OUT_DIR,
  type SharedModuleName,
  type ViewBundleManifest,
} from "fxtr-view/view_bundle";
import type { Plugin, UserConfig } from "vite";

export const BUNDLE_MODULE = "bundle.js";
/** The environment variable that redirects the build from `VIEW_BUNDLE_OUT_DIR`; fxtr's
 *  `project/views.py` mirrors it. */
export const VIEW_BUNDLE_OUT_DIR_ENV = "FXTR_VIEW_BUNDLE_OUT_DIR";
export const BUNDLE_STYLESHEET = "bundle.css";

/** The IIFE's variable and the identifier each shared module is bound to inside the factory. */
const RESULT = "__fxtrViews";
const identifier = (name: SharedModuleName): string => `__fxtrShared_${name.replace(/[^A-Za-z0-9]/g, "_")}`;

export interface ViewBundleOptions {
  /** The module whose default export is the package's `ViewBundle`, relative to the package. */
  readonly entry: string;
}

/** The Vite config that builds a renderer package into a bundle, in its `dist/` (or where
 *  `VIEW_BUNDLE_OUT_DIR_ENV` says). */
export function defineViewBundleConfig({ entry }: ViewBundleOptions): UserConfig {
  const globals = Object.fromEntries(SHARED_MODULES.map((name) => [name, identifier(name)]));
  return {
    // JSX through the shared `react/jsx-runtime`, whatever the package's tsconfig says.
    esbuild: { jsx: "automatic" },
    build: {
      outDir: process.env[VIEW_BUNDLE_OUT_DIR_ENV] || VIEW_BUNDLE_OUT_DIR,
      emptyOutDir: true,
      // Readable output, and nothing reorders the code around the factory wrapper.
      minify: false,
      lib: {
        entry,
        formats: ["iife"],
        name: RESULT,
        fileName: () => BUNDLE_MODULE,
        cssFileName: BUNDLE_STYLESHEET.replace(/\.css$/, ""),
      },
      rollupOptions: {
        external: [...SHARED_MODULES],
        output: {
          globals,
          exports: "named",
        },
      },
    },
    plugins: [bundlePlugin()],
  };
}

/** Wraps the module in the factory and writes the manifest, after every other plugin. */
function bundlePlugin(): Plugin {
  const bindings = SHARED_MODULES.map((name) => `  const ${identifier(name)} = shared[${JSON.stringify(name)}];`);
  return {
    name: "fxtr-view-bundle",
    enforce: "post",
    generateBundle(_options, bundle) {
      const module = bundle[BUNDLE_MODULE];
      if (module?.type !== "chunk") this.error(`the build emitted no ${BUNDLE_MODULE}`);
      module.code = [
        "export default function createViews(shared) {",
        ...bindings,
        module.code,
        `  return ${RESULT}.default;`,
        "}",
        "",
      ].join("\n");
      const files = new Set(Object.keys(bundle));
      const written: ViewBundleManifest = {
        apiVersion: VIEW_API_VERSION,
        module: BUNDLE_MODULE,
        ...(files.has(BUNDLE_STYLESHEET) ? { stylesheet: BUNDLE_STYLESHEET } : {}),
      };
      this.emitFile({ type: "asset", fileName: VIEW_BUNDLE_MANIFEST_FILENAME, source: `${JSON.stringify(written, null, 2)}\n` });
    },
  };
}
