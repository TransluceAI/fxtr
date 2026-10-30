/**
 * useRenderer - Shared hook for fetching and compiling renderer modules
 *
 * Used by DynamicRenderer components to load custom renderers.
 */

import { useState, useEffect } from 'react';
import { useQuery } from 'urql';
import { getOrCompileModule, getRendererFromModule, type RendererResult } from './rendererCache';
import type { CompiledRendererSet } from './esbuildSetup';
import type { RendererReference, RendererModule } from '../../types';
import { GET_RENDERER_MODULE } from '../../graphql/queries';

export interface UseRendererResult {
  renderers: CompiledRendererSet | null;
  stylesheet: CSSStyleSheet | null;
  error: Error | null;
  loading: boolean;
}

export function useRenderer(rendererRef: RendererReference | null): UseRendererResult {
  // Fetch the renderer module if we have a reference
  const [queryResult] = useQuery({
    query: GET_RENDERER_MODULE,
    variables: { id: rendererRef?.moduleId ?? '' },
    pause: !rendererRef,
  });

  const [result, setResult] = useState<RendererResult | null>(null);
  const [compileError, setCompileError] = useState<Error | null>(null);
  const [compiling, setCompiling] = useState(false);

  // Get the module data from query result
  const moduleData = queryResult.data?.rendererModule as RendererModule | undefined;
  const queryError = queryResult.error;
  const queryLoading = queryResult.fetching;

  // Compile the module when we have the data
  useEffect(() => {
    if (!rendererRef || !moduleData) {
      setResult(null);
      setCompiling(false);
      return;
    }

    let cancelled = false;
    setCompiling(true);
    setCompileError(null);

    getOrCompileModule(moduleData)
      .then((compiledModule) => {
        if (cancelled) return;

        // Get the specific renderer for this key
        const rendererResult = getRendererFromModule(compiledModule, rendererRef.rendererKey);
        if (rendererResult) {
          setResult(rendererResult);
          console.log(`Loaded renderer module ${rendererRef.moduleId} for key ${rendererRef.rendererKey}: `, {rendererResult});
        } else {
          // Renderer key not found in module
          setCompileError(new Error(
            `Renderer key "${rendererRef.rendererKey}" not found in module.\n` +
            `Available keys: ${[...compiledModule.renderers.keys()].join(', ')}`
          ));
        }
        setCompiling(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setCompileError(err);
        setCompiling(false);
      });

    return () => {
      cancelled = true;
    };
  }, [rendererRef?.moduleId, rendererRef?.rendererKey, moduleData]);

  // Combine errors
  const error = queryError ? new Error(queryError.message) : compileError;
  const loading = queryLoading || compiling;

  return {
    renderers: result?.renderers ?? null,
    stylesheet: result?.stylesheet ?? null,
    error,
    loading,
  };
}
