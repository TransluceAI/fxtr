/**
 * ``RefDropRegion`` — suppress dockview's drop-overlay while the
 * cursor is inside a renderer-owned drop target.
 *
 * The shell's ``DockviewLayout`` accepts any ``DRAG_MIME_FXTR_REF``
 * drag by default and shows dockview's "open a panel" overlay. When
 * a renderer wants to accept a ref drag onto its own surface (e.g.
 * the agent panel's send-box), it needs a way to tell the shell
 * "don't show your overlay — I'll handle this drop". This module is
 * the indirection.
 *
 * Architecture (mirrors thingwatcher's pattern):
 *
 *   - A shared counter lives in ``RefDropRegionContext``, mounted
 *     near the top of the shell tree by ``<RefDropRegionProvider>``.
 *   - Renderers use ``useRefDropRegion()`` to get
 *     ``{regionProps: {onDragEnter, onDragLeave, onDrop}}`` they
 *     spread onto a wrapper element. The hook also tracks a
 *     per-region nested-enter counter so the shared counter only
 *     bumps on true enter/leave transitions, not on every child-
 *     traversal (drag events fire for each child element).
 *   - The shell's ``DockviewLayout`` reads ``isActive`` via context
 *     and skips ``e.accept()`` in its ``onUnhandledDragOverEvent``
 *     handler whenever ``isActive === true``.
 *
 * If a region component unmounts mid-drag (with its local counter >
 * 0), the cleanup ``useEffect`` decrements the shared counter to
 * keep it consistent.
 *
 * Owned by ``@fxtr-ui/workstation-dispatch`` so externalised
 * renderer modules and the shell agree on a single ``isActive``
 * source per shell mount.
 */

import {
	type DragEvent as ReactDragEvent,
	type ReactNode,
	createContext,
	useCallback,
	useContext,
	useEffect,
	useMemo,
	useRef,
	useState,
} from "react";

export interface RefDropRegionContextValue {
	/** Whether any ``RefDropRegion`` is currently being dragged over. */
	readonly isActive: boolean;
	/** Bump on enter from outside any region (first true-enter). */
	readonly increment: () => void;
	/** Bump on leave / drop (last true-leave). */
	readonly decrement: () => void;
}

/**
 * Null default: when no ``<RefDropRegionProvider>`` is mounted,
 * ``useRefDropRegion`` becomes a no-op (returns drag handlers that
 * don't bump anything). Lets renderers be exercised in tests /
 * standalone contexts without a shell.
 */
export const RefDropRegionContext = createContext<RefDropRegionContextValue | null>(null);

/**
 * Provider for ``RefDropRegionContext``. Mount near the top of the
 * shell tree, above both ``DockviewLayout`` (which reads the
 * ``isActive`` flag) and any panel content (where renderers install
 * drop regions).
 */
export function RefDropRegionProvider({ children }: { readonly children: ReactNode }) {
	const [counter, setCounter] = useState(0);
	const increment = useCallback(() => {
		setCounter((c) => c + 1);
	}, []);
	const decrement = useCallback(() => {
		setCounter((c) => Math.max(0, c - 1));
	}, []);
	const value = useMemo<RefDropRegionContextValue>(
		() => ({ isActive: counter > 0, increment, decrement }),
		[counter, increment, decrement],
	);
	return <RefDropRegionContext.Provider value={value}>{children}</RefDropRegionContext.Provider>;
}

export interface RefDropRegionProps {
	readonly onDragEnter: (e: ReactDragEvent) => void;
	readonly onDragLeave: (e: ReactDragEvent) => void;
	readonly onDrop: (e: ReactDragEvent) => void;
}

export interface UseRefDropRegionResult {
	/**
	 * Spread onto the wrapper element of your drop target. Bumps the
	 * shared counter on true enter/leave and on drop; safe to compose
	 * with the consumer's own ``onDragEnter`` / ``onDragLeave`` /
	 * ``onDrop`` handlers (call your own first, then forward the
	 * event to these — or just spread these and let the bubbling
	 * order do the right thing).
	 */
	readonly regionProps: RefDropRegionProps;
}

/**
 * Returns drag-region handlers that bump the shared
 * ``RefDropRegionContext`` counter when a drag enters / leaves the
 * region. Outside a ``<RefDropRegionProvider>``, the handlers are
 * no-ops (the renderer can still install its own drop logic; only
 * the dockview-suppression coordination is unavailable).
 *
 * Tracks a per-region nested-enter counter so child-element
 * traversal doesn't flip the shared flag rapidly. The counter only
 * informs the shared context on transitions between 0 and 1.
 */
export function useRefDropRegion(): UseRefDropRegionResult {
	const ctx = useContext(RefDropRegionContext);
	const increment = ctx?.increment;
	const decrement = ctx?.decrement;
	const counterRef = useRef(0);

	const onDragEnter = useCallback(
		(_e: ReactDragEvent) => {
			const prev = counterRef.current;
			counterRef.current = prev + 1;
			if (prev === 0) increment?.();
		},
		[increment],
	);

	const onDragLeave = useCallback(
		(_e: ReactDragEvent) => {
			counterRef.current = Math.max(0, counterRef.current - 1);
			if (counterRef.current === 0) decrement?.();
		},
		[decrement],
	);

	const onDrop = useCallback(
		(_e: ReactDragEvent) => {
			if (counterRef.current > 0) {
				counterRef.current = 0;
				decrement?.();
			}
		},
		[decrement],
	);

	// Cleanup: if the region unmounts while a drag is mid-enter,
	// release the shared counter so we don't leak a stuck "active"
	// state into the dockview-suppression coordination.
	useEffect(() => {
		return () => {
			if (counterRef.current > 0) {
				counterRef.current = 0;
				decrement?.();
			}
		};
	}, [decrement]);

	const regionProps = useMemo<RefDropRegionProps>(
		() => ({ onDragEnter, onDragLeave, onDrop }),
		[onDragEnter, onDragLeave, onDrop],
	);
	return { regionProps };
}
