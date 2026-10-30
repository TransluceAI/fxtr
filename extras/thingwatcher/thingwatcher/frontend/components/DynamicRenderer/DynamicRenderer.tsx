/**
 * DynamicRenderer - Compiles and renders custom TSX renderers
 *
 * Uses esbuild-wasm to compile TSX code in the browser, then renders
 * the compiled FullRenderer or RefRenderer component inside a shadow DOM
 * for style isolation. CSS modules are attached via adoptedStyleSheets.
 *
 * All renderers (custom and fallback) receive opaque handles and use hooks
 * to access thing data.
 */

import React, { Component, type ReactNode, useMemo } from 'react';
import type {
  FullRendererHandle,
  RefRendererHandle,
  PreviewRendererHandle,
} from '../../lib/handleFactory';
import { useRenderer } from './useRenderer';
import { FallbackFullRenderer, FallbackRefRenderer, FallbackInlineRefRenderer, FallbackPreviewRenderer } from './FallbackRenderers';
import { ShadowRootDiv } from '../../lib/ShadowDOM';
import type { RendererReference } from '../../types';
import styles from './DynamicRenderer.module.css';

// =============================================================================
// Error Boundary
// =============================================================================

interface ErrorBoundaryProps {
  children: ReactNode;
  fallback: (error: Error) => ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  render() {
    if (this.state.error) {
      return this.props.fallback(this.state.error);
    }
    return this.props.children;
  }
}

// =============================================================================
// DynamicFullRenderer Component
// =============================================================================

interface DynamicFullRendererProps {
  /** The thing type (used for fallback display) */
  thingType: string;
  /** Renderer reference (null means use fallback) */
  rendererRef: RendererReference | null;
  /** Handle to pass to the renderer */
  handle: FullRendererHandle;
  /** Args to pass to the renderer */
  args: Record<string, unknown>;
  /** Whether this is a top-level panel view */
  isTopLevel: boolean;
}

export function DynamicFullRenderer({
  thingType,
  rendererRef,
  handle,
  args,
  isTopLevel,
}: DynamicFullRendererProps) {
  const { renderers, stylesheet, error, loading } = useRenderer(rendererRef);

  // Memoize stylesheet array to avoid unnecessary re-renders
  const stylesheets = useMemo(
    () => stylesheet ? [stylesheet] : undefined,
    [stylesheet]
  );

  // Build renderer props
  const rendererProps = useMemo(
    () => ({ handle, args, isTopLevel }),
    [handle, args, isTopLevel]
  );

  if (loading && rendererRef) {
    return <div className={styles.loading}>Compiling renderer...</div>;
  }

  if (error) {
    return (
      <>
        <div className={styles.error}>
          <div className={styles.errorHeader}>Compile Error</div>
          <pre className={styles.errorMessage}>{error.message}</pre>
        </div>
        <div className={styles.fallback}>
          <div className={styles.fallbackHeader}>Fallback View</div>
          <FallbackFullRenderer
            handle={handle}
            args={args}
            isTopLevel={isTopLevel}
          />
        </div>
      </>
    );
  }

  // Use custom renderer if available, otherwise fallback
  const FullRenderer = renderers?.full;
  // console.log(`FullRenderer for ${rendererRef?.rendererKey} in module ${rendererRef?.moduleId} is: `, FullRenderer);
  if (FullRenderer) {
    return (
      <ErrorBoundary
        fallback={(err) => (
          <>
            <div className={styles.error}>
              <div className={styles.errorHeader}>Render Error</div>
              <pre className={styles.errorMessage}>{err.message}</pre>
            </div>
            <div className={styles.fallback}>
              <div className={styles.fallbackHeader}>Fallback View</div>
              <FallbackFullRenderer
                handle={handle}
                args={args}
                isTopLevel={isTopLevel}
              />
            </div>
          </>
        )}
      >
        <ShadowRootDiv stylesheets={stylesheets}>
          <FullRenderer {...rendererProps} key={`${rendererRef?.moduleId}:${rendererRef?.rendererKey}`} />
        </ShadowRootDiv>
      </ErrorBoundary>
    );
  }

  // No custom FullRenderer - use fallback (no shadow DOM needed)
  return (
    <FallbackFullRenderer
      handle={handle}
      args={args}
      isTopLevel={isTopLevel}
    />
  );
}

// =============================================================================
// DynamicRefRenderer Component
// =============================================================================

interface DynamicRefRendererProps {
  /** The thing type (used for fallback display) */
  thingType: string;
  /** Renderer reference (null means use fallback) */
  rendererRef: RendererReference | null;
  /** Handle to pass to the renderer */
  handle: RefRendererHandle;
  /** Args to pass to the renderer */
  args: Record<string, unknown>;
}

export function DynamicRefRenderer({
  thingType,
  rendererRef,
  handle,
  args,
}: DynamicRefRendererProps) {
  const { renderers, stylesheet, error, loading } = useRenderer(rendererRef);

  // Memoize stylesheet array
  const stylesheets = useMemo(
    () => stylesheet ? [stylesheet] : undefined,
    [stylesheet]
  );

  // Build renderer props
  const rendererProps = useMemo(
    () => ({ handle, args }),
    [handle, args]
  );

  if (loading && rendererRef) {
    return <span className={styles.loadingInline}>...</span>;
  }

  if (error) {
    // On error, just show fallback (no error UI for compact refs)
    return (
      <FallbackRefRenderer
        handle={handle}
        args={args}
      />
    );
  }

  // Use custom renderer if available, otherwise fallback
  const RefRenderer = renderers?.ref;
  if (RefRenderer) {
    return (
      <ErrorBoundary
        fallback={() => (
          <FallbackRefRenderer
            handle={handle}
            args={args}
          />
        )}
      >
        <ShadowRootDiv stylesheets={stylesheets} style={{ display: 'contents' }}>
          <RefRenderer {...rendererProps} />
        </ShadowRootDiv>
      </ErrorBoundary>
    );
  }

  // No custom RefRenderer - use fallback (no shadow DOM needed)
  return (
    <FallbackRefRenderer
      handle={handle}
      args={args}
    />
  );
}

// =============================================================================
// DynamicInlineRefRenderer Component
// =============================================================================

interface DynamicInlineRefRendererProps {
  /** The thing type (used for fallback display) */
  thingType: string;
  /** Renderer reference (null means use fallback) */
  rendererRef: RendererReference | null;
  /** Handle to pass to the renderer */
  handle: RefRendererHandle;
  /** Args to pass to the renderer */
  args: Record<string, unknown>;
}

export function DynamicInlineRefRenderer({
  thingType,
  rendererRef,
  handle,
  args,
}: DynamicInlineRefRendererProps) {
  const { renderers, stylesheet, error, loading } = useRenderer(rendererRef);

  // Memoize stylesheet array
  const stylesheets = useMemo(
    () => stylesheet ? [stylesheet] : undefined,
    [stylesheet]
  );

  // Build renderer props
  const rendererProps = useMemo(
    () => ({ handle, args }),
    [handle, args]
  );

  if (loading && rendererRef) {
    return <span className={styles.loadingInline}>...</span>;
  }

  if (error) {
    // On error, just show fallback (no error UI for compact inline refs)
    return (
      <FallbackInlineRefRenderer
        handle={handle}
        args={args}
      />
    );
  }

  // Use custom inline renderer if available, otherwise fallback
  const InlineRenderer = renderers?.inline;
  if (InlineRenderer) {
    return (
      <ErrorBoundary
        fallback={() => (
          <FallbackInlineRefRenderer
            handle={handle}
            args={args}
          />
        )}
      >
        <ShadowRootDiv stylesheets={stylesheets} style={{ display: 'contents' }}>
          <InlineRenderer {...rendererProps} />
        </ShadowRootDiv>
      </ErrorBoundary>
    );
  }

  // No custom InlineRefRenderer - use fallback (no shadow DOM needed)
  return (
    <FallbackInlineRefRenderer
      handle={handle}
      args={args}
    />
  );
}

// =============================================================================
// DynamicPreviewRenderer Component
// =============================================================================

interface DynamicPreviewRendererProps {
  /** The thing type (used for fallback display) */
  thingType: string;
  /** Renderer reference (null means use fallback) */
  rendererRef: RendererReference | null;
  /** Handle to pass to the renderer */
  handle: PreviewRendererHandle;
}

export function DynamicPreviewRenderer({
  thingType,
  rendererRef,
  handle,
}: DynamicPreviewRendererProps) {
  const { renderers, stylesheet, error, loading } = useRenderer(rendererRef);

  // Memoize stylesheet array
  const stylesheets = useMemo(
    () => stylesheet ? [stylesheet] : undefined,
    [stylesheet]
  );

  // Build renderer props
  const rendererProps = useMemo(
    () => ({ handle }),
    [handle]
  );

  if (loading && rendererRef) {
    return <span className={styles.loadingInline}>...</span>;
  }

  if (error) {
    // On error, just show fallback (no error UI for compact previews)
    return <FallbackPreviewRenderer handle={handle} />;
  }

  // Use custom preview renderer if available, otherwise fallback
  const PreviewRenderer = renderers?.preview;
  if (PreviewRenderer) {
    return (
      <ErrorBoundary
        fallback={() => <FallbackPreviewRenderer handle={handle} />}
      >
        <ShadowRootDiv stylesheets={stylesheets} style={{ display: 'contents' }}>
          <PreviewRenderer {...rendererProps} />
        </ShadowRootDiv>
      </ErrorBoundary>
    );
  }

  // No custom PreviewRenderer - use fallback (no shadow DOM needed)
  return <FallbackPreviewRenderer handle={handle} />;
}
