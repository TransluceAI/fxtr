/**
 * ShadowDOM - React components for rendering inside shadow DOM with slot support
 *
 * This module provides components to:
 * - Wrap content in a shadow root for style isolation
 * - Use slots to project selected children into light DOM (for app styling/context)
 * - Attach stylesheets via adoptedStyleSheets for CSS modules
 *
 * Based on sketch: /Users/daniel/tmp/portaldemo.html
 */

import React, {
  useRef,
  useEffect,
  useState,
  useId,
  useMemo,
  createContext,
  useContext,
  type ReactNode,
} from 'react';
import { createPortal } from 'react-dom';

// =============================================================================
// Context
// =============================================================================

/**
 * Context that passes the shadow host element reference to ShadowSlot children.
 * When inside a shadow DOM (context is non-null), ShadowSlot will portal
 * its children to the host element's light DOM.
 */
export const ShadowHostContext = createContext<HTMLElement | null>(null);

/**
 * Context for child components to register stylesheets to be adopted into the shadow root.
 * Used by AdoptStyleSheet to declaratively add stylesheets without imperative mutation.
 */
interface AdoptedStyleSheetContextValue {
  registerSheet: (id: string, sheet: CSSStyleSheet) => void;
  unregisterSheet: (id: string) => void;
}

const AdoptedStyleSheetContext = createContext<AdoptedStyleSheetContextValue | null>(null);

// =============================================================================
// ShadowSlot
// =============================================================================

export interface ShadowSlotProps {
  children: ReactNode;
}

/**
 * ShadowSlot - Renders children in light DOM while maintaining shadow DOM position.
 *
 * When used inside a ShadowRootDiv:
 * - Renders a <slot> element at the current position in shadow DOM
 * - Portals children to the host element as light DOM with matching slot attribute
 * - Browser's native slot projection displays children where the <slot> appears
 *
 * This allows components like NodeRef to:
 * - Be positioned within the custom renderer's layout (via slot)
 * - Inherit app styles (since they're in light DOM)
 * - Participate in app context (selection, events, etc.)
 *
 * When used outside a shadow DOM, just renders children directly.
 */
export function ShadowSlot({ children }: ShadowSlotProps) {
  const slotId = useId();
  const hostElement = useContext(ShadowHostContext);

  // If not inside a shadow DOM, just render children directly
  if (!hostElement) {
    console.warn('ShadowSlot used outside a ShadowRootDiv!');
    return <>{children}</>;
  }

  return (
    <>
      {/* This <slot> appears in the shadow DOM where ShadowSlot is placed in JSX */}
      <slot name={slotId} />
      {/* Children are portaled to light DOM (direct children of host element) */}
      {createPortal(
        // Reset context to null so nested ShadowSlots don't accidentally use the outer host
        <ShadowHostContext.Provider value={null}>
          <span slot={slotId} style={{ display: 'contents' }}>{children}</span>
        </ShadowHostContext.Provider>,
        hostElement
      )}
    </>
  );
}

// =============================================================================
// ShadowRootDiv
// =============================================================================

export interface ShadowRootDivProps {
  children: ReactNode;
  /** Optional CSS stylesheets to attach via adoptedStyleSheets */
  stylesheets?: CSSStyleSheet[];
  /** Optional className for the host element */
  className?: string;
  /** Optional inline styles for the host element */
  style?: React.CSSProperties;
}

/**
 * ShadowRootDiv - Creates a shadow root and renders children inside it.
 *
 * Features:
 * - Attaches a shadow root to the host div element
 * - Portals children into the shadow root for style isolation
 * - Provides ShadowHostContext so children can use ShadowSlot
 * - Supports adoptedStyleSheets for efficient CSS modules
 * - Supports AdoptStyleSheet for child components to declaratively add stylesheets
 *
 * Children rendered inside the shadow root are isolated from document styles.
 * Use ShadowSlot to project specific children back into light DOM.
 */
export function ShadowRootDiv({
  children,
  stylesheets,
  className,
  style,
}: ShadowRootDivProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [shadowRoot, setShadowRoot] = useState<ShadowRoot | null>(null);

  // Track child-adopted stylesheets (from AdoptStyleSheet components)
  const [childSheets, setChildSheets] = useState<Map<string, CSSStyleSheet>>(new Map());

  // Context value for children to register/unregister sheets
  const adoptedCtxValue = useMemo<AdoptedStyleSheetContextValue>(() => ({
    registerSheet: (id, sheet) => {
      setChildSheets(prev => new Map(prev).set(id, sheet));
    },
    unregisterSheet: (id) => {
      setChildSheets(prev => {
        const next = new Map(prev);
        next.delete(id);
        return next;
      });
    },
  }), []);

  // Attach shadow root on mount
  useEffect(() => {
    if (hostRef.current && !hostRef.current.shadowRoot) {
      const shadow = hostRef.current.attachShadow({ mode: 'open' });
      setShadowRoot(shadow);
    }
  }, []);

  // Combine prop stylesheets with child-adopted stylesheets
  useEffect(() => {
    if (shadowRoot) {
      const allSheets = [
        ...(stylesheets ?? []),
        ...childSheets.values(),
      ];
      shadowRoot.adoptedStyleSheets = allSheets;
    }
  }, [shadowRoot, stylesheets, childSheets]);

  return (
    <AdoptedStyleSheetContext.Provider value={adoptedCtxValue}>
      <ShadowHostContext.Provider value={hostRef.current}>
        <div ref={hostRef} className={className} style={style}>
          {/* Portal children into the shadow root */}
          {shadowRoot && createPortal(children, shadowRoot)}
        </div>
      </ShadowHostContext.Provider>
    </AdoptedStyleSheetContext.Provider>
  );
}

// =============================================================================
// useShadowHost hook
// =============================================================================

/**
 * Hook to check if currently inside a shadow DOM.
 * Returns the host element if inside a ShadowRootDiv, null otherwise.
 */
export function useShadowHost(): HTMLElement | null {
  return useContext(ShadowHostContext);
}

// =============================================================================
// AdoptStyleSheet
// =============================================================================

export interface AdoptStyleSheetProps {
  /** The CSSStyleSheet to adopt into the shadow root */
  sheet: CSSStyleSheet;
}

/**
 * AdoptStyleSheet - Declaratively adopts a stylesheet into the containing shadow root.
 *
 * Use this inside a ShadowRootDiv to add additional stylesheets (e.g., from twind)
 * without imperatively modifying adoptedStyleSheets. This avoids race conditions
 * when multiple sources need to add stylesheets.
 *
 * Renders nothing - just registers the sheet with the parent ShadowRootDiv.
 *
 * @example
 * ```tsx
 * const [twindState] = usePersistentGlobalState<TwindState>(handle, 'twind');
 * return (
 *   <div>
 *     {twindState && <AdoptStyleSheet sheet={twindState.sheet} />}
 *     <div className="p-4 bg-slate-800">...</div>
 *   </div>
 * );
 * ```
 */
export function AdoptStyleSheet({ sheet }: AdoptStyleSheetProps) {
  const id = useId();
  const ctx = useContext(AdoptedStyleSheetContext);

  useEffect(() => {
    if (!ctx) {
      console.warn('AdoptStyleSheet used outside a ShadowRootDiv');
      return;
    }
    ctx.registerSheet(id, sheet);
    return () => ctx.unregisterSheet(id);
  }, [ctx, id, sheet]);

  return null; // Renders nothing
}
