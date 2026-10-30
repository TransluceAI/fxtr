/**
 * Cache for compiled renderer modules
 *
 * Caches compiled modules by moduleId (content-addressed) to avoid
 * recompiling on every render. Multiple thing types can share the same
 * module if they use the same renderer bundle.
 */

import { compileModule, type CompiledModule, type CompiledRendererSet, type RendererModule } from './esbuildSetup';

// Cache: moduleId -> CompiledModule
const moduleCache = new Map<string, CompiledModule>();

// Pending compilations to avoid duplicate work
const pendingCompilations = new Map<string, Promise<CompiledModule>>();

/**
 * Result type for getting a renderer for a specific thing type.
 * Includes both the renderer set and the shared stylesheet.
 */
export interface RendererResult {
  renderers: CompiledRendererSet;
  stylesheet: CSSStyleSheet | null;
}

/**
 * Get or compile a renderer module by its ID.
 *
 * Returns the cached module if available, otherwise compiles it.
 *
 * @param module - The renderer module source (main TSX + imports)
 * @returns Compiled module with stylesheet and all renderer sets
 */
export async function getOrCompileModule(
  module: RendererModule
): Promise<CompiledModule> {
  // Check cache
  const cached = moduleCache.get(module.id);
  if (cached) {
    return cached;
  }

  // Check if already compiling
  const pending = pendingCompilations.get(module.id);
  if (pending) {
    return pending;
  }

  // Compile
  const compilationPromise = compileModule(module).then((compiled) => {
    // Store in cache
    moduleCache.set(module.id, compiled);
    pendingCompilations.delete(module.id);
    return compiled;
  });

  pendingCompilations.set(module.id, compilationPromise);
  return compilationPromise;
}

/**
 * Get a specific renderer set from a compiled module.
 *
 * @param compiledModule - The compiled module
 * @param rendererKey - The renderer key to look up
 * @returns Renderer set and shared stylesheet, or null if key not found
 */
export function getRendererFromModule(
  compiledModule: CompiledModule,
  rendererKey: string
): RendererResult | null {
  const renderers = compiledModule.renderers.get(rendererKey);
  if (!renderers) {
    return null;
  }
  return {
    renderers,
    stylesheet: compiledModule.stylesheet,
  };
}

/**
 * Clear the renderer cache. Useful for testing or hot reload.
 */
export function clearRendererCache(): void {
  moduleCache.clear();
  pendingCompilations.clear();
}
