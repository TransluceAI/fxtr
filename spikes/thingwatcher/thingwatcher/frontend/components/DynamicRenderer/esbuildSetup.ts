/**
 * esbuild-wasm setup for dynamic TSX compilation
 *
 * Dynamic renderers are compiled in the browser using esbuild-wasm.
 * - react and react/jsx-runtime are injected via virtual modules (same instance as main app)
 * - thingwatcher-bindings is external (resolved via browser import map)
 * - Bare imports are rewritten to esm.sh with React externalized
 */

import * as esbuild from 'esbuild-wasm';
import * as React from 'react';
import * as ReactJsxRuntime from 'react/jsx-runtime';
import * as ThingwatcherBindings from '../../lib/ThingWatcherBindingsExternal';
import type { FullRendererProps, RefRendererProps, PreviewRendererProps, GraphNodeRendererProps } from '../../lib/ThingRenderLib';
import type { RendererModule } from '../../types';

// Re-export RendererModule for convenience
export type { RendererModule };

// =============================================================================
// Types
// =============================================================================

/** Compiled renderers for a single renderer key within a module */
export interface CompiledRendererSet {
  full: React.ComponentType<FullRendererProps> | null;
  ref: React.ComponentType<RefRendererProps> | null;
  inline: React.ComponentType<RefRendererProps> | null;  // Same props as ref, different display
  preview: React.ComponentType<PreviewRendererProps> | null;  // Compact preview for hover cards
  graph: React.ComponentType<GraphNodeRendererProps> | null;  // Custom graph node renderer
}

/** A compiled renderer module containing all renderer sets and shared stylesheet */
export interface CompiledModule {
  stylesheet: CSSStyleSheet | null;  // Hoisted: shared across all renderers in module
  renderers: Map<string, CompiledRendererSet>;  // renderer_key -> compiled set
}

// =============================================================================
// Injected Dependencies (React - shared with main app)
// =============================================================================

// React modules injected into dynamic renderers (same instance as main app)
const INJECTED_MODULES: Record<string, unknown> = {
  'react': React,
  'react/jsx-runtime': ReactJsxRuntime,
  'thingwatcher-bindings': ThingwatcherBindings,
};

// Make modules available globally for compiled code
declare global {
  interface Window {
    __THINGWATCHER_INJECTED__: Record<string, unknown>;
  }
}

if (typeof window !== 'undefined') {
  window.__THINGWATCHER_INJECTED__ = INJECTED_MODULES;
}

function generateVirtualModuleCode(moduleName: string): string {
  const mod = INJECTED_MODULES[moduleName] as Record<string, unknown>;
  if (!mod) return '';

  const exports = Object.keys(mod).filter((k) => k !== '__esModule');
  if (exports.length === 0) return '';

  return `const __mod = window.__THINGWATCHER_INJECTED__["${moduleName}"];\nexport const { ${exports.join(', ')} } = __mod;`;
}

// =============================================================================
// esbuild Initialization
// =============================================================================

let esbuildInitialized = false;
let esbuildInitPromise: Promise<void> | null = null;

export async function initEsbuild(): Promise<void> {
  if (esbuildInitialized) return;
  if (esbuildInitPromise) return esbuildInitPromise;

  esbuildInitPromise = (async () => {
    // IMPORTANT: This version must match the esbuild-wasm version in package.json exactly.
    // The JS API and wasm binary use a versioned protocol - mismatched versions will fail
    // with "Host version X does not match binary version Y".
    await esbuild.initialize({
      wasmURL: 'https://esm.sh/esbuild-wasm@0.24.2/esbuild.wasm',
    });
    esbuildInitialized = true;
  })();

  return esbuildInitPromise;
}

// =============================================================================
// Plugins
// =============================================================================

/**
 * Plugin to inject React modules (shares same instance as main app).
 * Also handles thingwatcher-bindings as external (resolved via import map).
 */
function createInjectedDepsPlugin(): esbuild.Plugin {
  const injectedModuleNames = Object.keys(INJECTED_MODULES);
  // Escape special regex characters in module names (like the / in react/jsx-runtime)
  const escapedNames = injectedModuleNames.map(name => name.replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&'));
  const injectedPattern = new RegExp(`^(${escapedNames.join('|')})$`);

  // esm.sh external param to prevent bundling React (will be resolved via import map)
  const esmShExternals = 'external=' + encodeURIComponent('react');

  return {
    name: 'thingwatcher-injected-deps',
    setup(build) {
      // Handle injected React modules - resolve to virtual namespace
      build.onResolve({ filter: injectedPattern }, (args) => ({
        path: args.path,
        namespace: 'injected',
      }));

      // Provide virtual modules for injected deps
      build.onLoad({ filter: /.*/, namespace: 'injected' }, (args) => {
        const code = generateVirtualModuleCode(args.path);
        if (code) {
          return { contents: code, loader: 'js' };
        }
        return null;
      });

      // thingwatcher-bindings is external (resolved via import map)
      build.onResolve({ filter: /^thingwatcher-bindings$/ }, (args) => ({
        path: args.path,
        external: true,
      }));

      // Handle other bare imports - rewrite to esm.sh
      build.onResolve({ filter: /^[^./]/ }, (args) => {
        // Skip injected modules (handled above)
        if (args.namespace === 'injected') return null;
        if (injectedModuleNames.includes(args.path)) return null;
        // Skip thingwatcher-bindings (handled above)
        if (args.path === 'thingwatcher-bindings') return null;

        // Full URLs
        if (args.path.startsWith('http://') || args.path.startsWith('https://')) {
          // CSS URLs - fetch and bundle instead of external
          if (args.path.endsWith('.module.css') || args.path.endsWith('.css')) {
            return { path: args.path, namespace: 'remote-css' };
          }

          // Non-CSS URLs - pass through, adding external param for esm.sh
          let url = args.path;
          if (url.includes('esm.sh/') && !url.includes('external=')) {
            url += (url.includes('?') ? '&' : '?') + esmShExternals;
          }
          return { path: url, external: true };
        }

        // Bare imports - rewrite to esm.sh with React externalized
        return {
          path: `https://esm.sh/${args.path}?${esmShExternals}`,
          external: true,
        };
      });

      // Fetch remote CSS and bundle it
      build.onLoad({ filter: /.*/, namespace: 'remote-css' }, async (args) => {
        const response = await fetch(args.path);
        if (!response.ok) {
          throw new Error(`Failed to fetch CSS: ${args.path} (${response.status} ${response.statusText})`);
        }
        const contents = await response.text();
        const loader: esbuild.Loader = args.path.endsWith('.module.css') ? 'local-css' : 'css';
        return { contents, loader };
      });
    },
  };
}

// =============================================================================
// Virtual Filesystem Plugin (for user-provided imports like ./utils.tsx, ./styles.module.css)
// =============================================================================

function createVirtualFsPlugin(imports: Record<string, string>): esbuild.Plugin {
  return {
    name: 'thingwatcher-virtual-fs',
    setup(build) {
      // Resolve relative imports to virtual namespace if they exist in imports
      build.onResolve({ filter: /^\./ }, (args) => {
        const resolved = args.path;
        if (imports[resolved]) {
          return { path: resolved, namespace: 'virtual-fs' };
        }
        // Let esbuild handle resolution failure for missing imports
        return null;
      });

      // Load virtual modules from imports dict
      build.onLoad({ filter: /.*/, namespace: 'virtual-fs' }, (args) => {
        const source = imports[args.path];
        if (source !== undefined) {
          // Determine loader based on file extension
          let loader: esbuild.Loader = 'tsx';
          if (args.path.endsWith('.module.css')) {
            loader = 'local-css';  // CSS modules (scoped class names)
          } else if (args.path.endsWith('.css')) {
            loader = 'css';  // Global CSS
          } else if (args.path.endsWith('.ts')) {
            loader = 'ts';
          } else if (args.path.endsWith('.js')) {
            loader = 'js';
          } else if (args.path.endsWith('.jsx')) {
            loader = 'jsx';
          }
          return { contents: source, loader };
        }
        return null;
      });
    },
  };
}

// =============================================================================
// Compilation
// =============================================================================

/**
 * Runtime representation of ThingWatcherRenderConfig (the default export)
 */
interface RawRendererConfig {
  dialect: string;
  renderers: Record<string, {
    full?: React.ComponentType<FullRendererProps>;
    ref?: React.ComponentType<RefRendererProps>;
    inline?: React.ComponentType<RefRendererProps>;
    preview?: React.ComponentType<PreviewRendererProps>;
    graph?: React.ComponentType<GraphNodeRendererProps>;
  }>;
}

/**
 * Compile a renderer module (main TSX + optional imports) and extract all renderer sets.
 *
 * Expects the module to have a default export of type ThingWatcherRenderConfig:
 *   export default { dialect: "0.1", renderers: { ... } } as ThingWatcherRenderConfig;
 *
 * Strict output validation: only accepts out/main.js and optionally out/main.css.
 * Any other output files are treated as a compilation error.
 */
export async function compileModule(
  module: RendererModule
): Promise<CompiledModule> {
  await initEsbuild();

  const result = await esbuild.build({
    stdin: {
      contents: module.main,
      loader: 'tsx',
      sourcefile: 'main.tsx',  // Explicit name for predictable output
    },
    outdir: 'out',  // Required for multiple outputs (JS + CSS)
    bundle: true,
    format: 'esm',
    write: false as const,
    // Use automatic JSX transform (React 17+) - no need for `import React` in source files
    jsx: 'automatic',
    jsxImportSource: 'react',
    plugins: [
      createInjectedDepsPlugin(),
      createVirtualFsPlugin(module.imports),
    ],
  });

  // Check for build errors
  if (result.errors.length > 0) {
    const errorMessages = result.errors.map(e => {
      const loc = e.location ? `${e.location.file}:${e.location.line}:${e.location.column}: ` : '';
      return `${loc}${e.text}`;
    }).join('\n');
    throw new Error(`Build failed:\n${errorMessages}`);
  }

  // Strict output file validation
  // We expect: one JS file and optionally one CSS file
  // esbuild-wasm uses stdin.js/stdin.css for stdin input despite sourcefile setting
  const jsFiles: Array<{ path: string; text: string }> = [];
  const cssFiles: Array<{ path: string; text: string }> = [];
  const unexpectedFiles: string[] = [];

  // outputFiles is always present when write: false (enforced by `as const` above)
  for (const file of result.outputFiles) {
    // Normalize path - esbuild may use different separators
    const normalizedPath = file.path.replace(/\\/g, '/');

    if (normalizedPath.endsWith('.js')) {
      jsFiles.push({ path: file.path, text: file.text });
    } else if (normalizedPath.endsWith('.css')) {
      cssFiles.push({ path: file.path, text: file.text });
    } else {
      unexpectedFiles.push(file.path);
    }
  }

  // Validate and throw errors with all files listed
  if (jsFiles.length === 0) {
    throw new Error('esbuild did not produce expected JS output');
  }
  if (jsFiles.length > 1) {
    const allJsPaths = jsFiles.map(f => f.path).join(', ');
    throw new Error(`Multiple JS output files from esbuild: ${allJsPaths}`);
  }
  if (cssFiles.length > 1) {
    const allCssPaths = cssFiles.map(f => f.path).join(', ');
    throw new Error(`Multiple CSS output files from esbuild: ${allCssPaths}`);
  }
  if (unexpectedFiles.length > 0) {
    const allUnexpectedPaths = unexpectedFiles.join(', ');
    throw new Error(`Unexpected output files from esbuild: ${allUnexpectedPaths}. Expected only .js and .css files.`);
  }

  const jsCode = jsFiles[0].text;
  const cssCode = cssFiles.length > 0 ? cssFiles[0].text : null;

  // Create stylesheet from CSS if present (hoisted: shared across all renderers)
  let stylesheet: CSSStyleSheet | null = null;
  if (cssCode) {
    stylesheet = new CSSStyleSheet();
    stylesheet.replaceSync(cssCode);
  }

  // Load the compiled JS module
  const blob = new Blob([jsCode], { type: 'application/javascript' });
  const url = URL.createObjectURL(blob);

  try {
    let esModule: Record<string, unknown>;
    try {
      esModule = await import(/* @vite-ignore */ url);
    } catch (importErr) {
      console.error(importErr);
      // Provide more context about what might have failed
      const message = importErr instanceof Error ? importErr.message : String(importErr);
      throw new Error(
        `Failed to load compiled renderer module.\n\n` +
        `This usually means an external import (from esm.sh or another URL) failed to load.\n` +
        `Check the browser console for failed network requests.\n\n` +
        `Original error: ${message}`
      );
    }

    // Get the default export (ThingWatcherRenderConfig)
    const config = esModule.default as RawRendererConfig | undefined;

    if (!config) {
      throw new Error(
        `Renderer module must have a default export of type ThingWatcherRenderConfig.\n\n` +
        `Example:\n` +
        `  export default {\n` +
        `    dialect: "0.1",\n` +
        `    renderers: { "MyType": { full: MyFullRenderer } }\n` +
        `  } as ThingWatcherRenderConfig;\n`
      );
    }

    // Validate dialect version
    const expectedDialect = '0.1';

    if (config.dialect === undefined) {
      throw new Error(
        `Renderer config must include dialect: "${expectedDialect}".\n`
      );
    }

    if (config.dialect !== expectedDialect) {
      throw new Error(
        `Renderer has unsupported dialect version "${config.dialect}".\n` +
        `Expected: "${expectedDialect}"\n` +
        `Update your renderer to use the current API.`
      );
    }

    if (!config.renderers || typeof config.renderers !== 'object') {
      throw new Error(
        `Renderer config must include a "renderers" object.\n`
      );
    }

    // Extract all renderer sets
    const renderers = new Map<string, CompiledRendererSet>();
    for (const [key, set] of Object.entries(config.renderers)) {
      renderers.set(key, {
        full: set.full ?? null,
        ref: set.ref ?? null,
        inline: set.inline ?? null,
        preview: set.preview ?? null,
        graph: set.graph ?? null,
      });
    }

    return {
      stylesheet,
      renderers,
    };
  } finally {
    URL.revokeObjectURL(url);
  }
}
