// The bundle contract: how a project's renderers reach a shell that was not compiled with them.
// A project builds its renderer package into a *bundle*, a directory holding an ES module, an
// optional stylesheet, and a manifest (`fxtr-view-bundle`'s preset writes all three). The module's
// default export is a `ViewBundleFactory`: the shell calls it with its own instances of the modules
// renderer code must share with it (hook state, atoms, and the shell context live in them) and gets
// back the bundle's `ViewBundle`, what it contributes. Everything else a renderer package uses is
// inside its bundle.
//
// `VIEW_API_VERSION` names this contract, which includes more than these types: the shared module
// set, the renderer and slot declarations of this library, the design tokens (the CSS custom
// properties a renderer's styles read), and this library's own class names. Bump it whenever any
// of them changes incompatibly; a shell loads only a bundle built at exactly its own version.

import type { AnyRenderer } from "./renderer";

/** The version of the bundle contract, stamped into a bundle's manifest by the build. */
export const VIEW_API_VERSION = 3;

/** What a bundle contributes to a shell. A shell adds `renderers` after its own defaults; later
 *  contributions are new fields. */
export interface ViewBundle {
  readonly renderers: readonly AnyRenderer[];
}

/** The module specifiers a bundle leaves to the shell, which passes its instances in. */
export const SHARED_MODULES = ["react", "react/jsx-runtime", "jotai", "fxtr-view"] as const;

export type SharedModuleName = (typeof SHARED_MODULES)[number];

/** The shell's instances of the shared modules, by specifier. Typed loosely: the bundle was
 *  compiled against these modules' types, and `VIEW_API_VERSION` is what says they agree. */
export type SharedModules = Readonly<Record<SharedModuleName, unknown>>;

/** A bundle module's default export. */
export type ViewBundleFactory = (shared: SharedModules) => ViewBundle;

/** The file naming a bundle's parts, at the root of its directory. */
export const VIEW_BUNDLE_MANIFEST_FILENAME = "fxtr-view-bundle-manifest.json";

/** Where a renderer package's bundle is built, relative to the package: the build writes there
 *  and whatever serves the bundle reads there. */
export const VIEW_BUNDLE_OUT_DIR = "dist";

/** A bundle's manifest: the contract version it was built at and its files, relative to the
 *  manifest. No stylesheet for a package without styles. */
export interface ViewBundleManifest {
  readonly apiVersion: number;
  readonly module: string;
  readonly stylesheet?: string;
}
