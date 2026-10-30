/**
 * useThingDropRegion - Hook for creating drop zones in custom renderers
 *
 * This hook provides:
 * - ThingDropRegion: A component that suppresses dockview drop handling
 * - Mime type helpers for filtering which things to accept
 * - extractDroppedThing: Function to extract metadata from a drop
 *
 * Usage:
 * ```tsx
 * function MyRenderer({ handle }: FullRendererProps) {
 *   const {
 *     ThingDropRegion,
 *     mimeTypeForAnyThing,
 *     mimeTypeForThingType,
 *     extractDroppedThing,
 *   } = useThingDropRegion(handle);
 *
 *   const [isDragOver, setIsDragOver] = useState(false);
 *   const [droppedThing, setDroppedThing] = usePersistentThingState(handle, 'dropped');
 *
 *   return (
 *     <ThingDropRegion>
 *       <div
 *         style={{ border: isDragOver ? '2px dashed #4a9' : '2px dashed #333' }}
 *         onDragOver={(e) => {
 *           if (e.dataTransfer.types.includes(mimeTypeForAnyThing)) {
 *             e.preventDefault();
 *             setIsDragOver(true);
 *           }
 *         }}
 *         onDragLeave={() => setIsDragOver(false)}
 *         onDrop={(e) => {
 *           e.preventDefault();
 *           setIsDragOver(false);
 *           const info = extractDroppedThing(e.dataTransfer);
 *           if (info) setDroppedThing(info);
 *         }}
 *       >
 *         Drop zone
 *       </div>
 *     </ThingDropRegion>
 *   );
 * }
 * ```
 */

import React, { useCallback, useRef, useMemo, useEffect } from 'react';
import type { FC, ReactNode, CSSProperties } from 'react';
import { useThingDropRegionContext } from './ThingDropRegionContext';
import {
  DRAG_MIME_THING,
  DRAG_MIME_THING_TYPE_PREFIX,
  DRAG_MIME_THING_KEY_PREFIX,
  hexEncode,
  parseThingDragData,
  type ThingDropInfo,
} from './dragConstants';
import type { FullRendererHandle } from './handleFactory';

// Re-export ThingDropInfo for convenience
export type { ThingDropInfo } from './dragConstants';

/**
 * Props for ThingDropRegion component.
 */
export interface ThingDropRegionProps {
  children: ReactNode;
  /** Optional className for the wrapper div */
  className?: string;
  /** Optional style for the wrapper div */
  style?: CSSProperties;
}

/**
 * Result of useThingDropRegion hook.
 */
export interface UseThingDropRegionResult {
  /**
   * Component that wraps children and suppresses dockview drop handling within its bounds.
   * Does NOT handle the actual drop - use standard onDragOver/onDrop on inner elements.
   * Accepts an optional `style` prop for the wrapper div.
   */
  ThingDropRegion: FC<ThingDropRegionProps>;

  /** Mime type that matches any thing (use with dataTransfer.types.includes()) */
  mimeTypeForAnyThing: string;

  /** Returns mime type for things of a specific type */
  mimeTypeForThingType: (thingType: string) => string;

  /** Returns mime type for a specific thing key */
  mimeTypeForSpecificThing: (thingKey: string) => string;

  /** Extract dropped thing metadata from DataTransfer, or null if not a thing */
  extractDroppedThing: (dataTransfer: DataTransfer) => ThingDropInfo | null;
}

/**
 * Hook for creating drop zones in custom renderers.
 *
 * @param _handle - FullRendererHandle (required to ensure this is used in FullRenderer context)
 * @returns Object with ThingDropRegion component and helper functions
 */
export function useThingDropRegion(_handle: FullRendererHandle): UseThingDropRegionResult {
  const dropRegionContext = useThingDropRegionContext();

  // Extract stable callbacks from context - these don't change when counter changes
  const incrementDropRegion = dropRegionContext?.incrementDropRegion;
  const decrementDropRegion = dropRegionContext?.decrementDropRegion;

  // Create ThingDropRegion component
  // Only depends on the stable callbacks, not the whole context (which includes isDropRegionActive)
  const ThingDropRegion = useMemo(() => {
    const Component: FC<ThingDropRegionProps> = ({ children, className, style }) => {
      // Counter for tracking dragenter/dragleave across nested elements
      // Drag events fire for every nested element: entering a child triggers dragleave on parent
      // then dragenter bubbles up. Counter approach: when counter > 0, we're inside.
      const counterRef = useRef(0);

      const handleDragEnter = useCallback(() => {
        const prev = counterRef.current;
        counterRef.current += 1;
        if (prev === 0) {
          incrementDropRegion?.();
        }
      }, []);

      const handleDragLeave = useCallback(() => {
        counterRef.current -= 1;
        if (counterRef.current === 0) {
          decrementDropRegion?.();
        }
      }, []);

      const handleDrop = useCallback(() => {
        if (counterRef.current > 0) {
          counterRef.current = 0;
          decrementDropRegion?.();
        }
      }, []);

      // Cleanup: if component unmounts while drag is inside, decrement
      useEffect(() => {
        return () => {
          if (counterRef.current > 0) {
            decrementDropRegion?.();
          }
        };
      }, []);

      return (
        <div
          className={className}
          onDragEnter={handleDragEnter}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
          style={style}
        >
          {children}
        </div>
      );
    };

    Component.displayName = 'ThingDropRegion';
    return Component;
  }, [incrementDropRegion, decrementDropRegion]);

  // Mime type helpers (pure functions, no dependencies)
  // Note: Browsers lowercase MIME types, so we hex-encode to preserve case sensitivity
  const mimeTypeForThingType = useCallback((thingType: string): string => {
    return DRAG_MIME_THING_TYPE_PREFIX + hexEncode(thingType);
  }, []);

  const mimeTypeForSpecificThing = useCallback((thingKey: string): string => {
    // Hex-encode because browsers lowercase MIME types
    return DRAG_MIME_THING_KEY_PREFIX + hexEncode(thingKey);
  }, []);

  return {
    ThingDropRegion,
    mimeTypeForAnyThing: DRAG_MIME_THING,
    mimeTypeForThingType,
    mimeTypeForSpecificThing,
    extractDroppedThing: parseThingDragData,
  };
}
