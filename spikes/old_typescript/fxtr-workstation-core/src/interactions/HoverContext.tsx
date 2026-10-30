/**
 * Shared hover-card coordination contexts.
 *
 * `HoverKeptOpenContext` — a hover-card popup publishes a setter that
 * nested hover-cards inside the popup invoke with `true` when they open
 * and `false` when they close. The outer card uses that flag to stay
 * open while a child is in focus; without it, moving the cursor into a
 * nested popup would close the parent.
 *
 * `HoverCloseContext` — collects "close me" callbacks from every open
 * popup. `navigate()` (or any imperative action that moves the user
 * elsewhere) calls `closeAllHovers()` so stale popups snap shut.
 *
 * Ported from thingwatcher's `frontend/lib/HoverContext.tsx`; renamed
 * slightly for workstation but semantically identical.
 */

import { createContext, useContext } from "react";

/**
 * Provided by each hover-card popup. Nested popups call the function
 * from inside it with `true` while they're open, keeping the parent
 * from auto-closing.
 */
export const HoverKeptOpenContext = createContext<((open: boolean) => void) | null>(null);

/**
 * Nested-popup side — read the parent's setter (if any).
 */
export function useSetKeptOpen(): ((open: boolean) => void) | null {
	return useContext(HoverKeptOpenContext);
}

/**
 * Provided by `WorkstationShell`. `registerCloseListener` returns an
 * unsubscribe. `closeAllHovers` runs every registered callback.
 */
export interface HoverCloseContextValue {
	registerCloseListener: (cb: () => void) => () => void;
	closeAllHovers: () => void;
}

export const HoverCloseContext = createContext<HoverCloseContextValue | null>(null);

export function useHoverClose(): HoverCloseContextValue | null {
	return useContext(HoverCloseContext);
}

/** No-op-safe variant for call sites that don't care whether a provider exists. */
export function useCloseAllHovers(): () => void {
	const ctx = useContext(HoverCloseContext);
	return ctx?.closeAllHovers ?? noop;
}

function noop() {}
