import { createContext, useContext } from 'react'

/**
 * How a view shows a workflow invocation's level in the trace (the centre), by its pathname: what
 * a workflow invocation's panel opens, and what double-clicking a workflow's row in a card's
 * invocations table does. `shown` is the pathname of the level on screen. Provided by `JobView`.
 */
export interface LevelOpener {
  open: (pathname: string) => void
  shown: string
}

export const OpenLevelContext = createContext<LevelOpener | null>(null)

/** The level opener, or null outside `JobView` (a test). */
export function useLevelOpener(): LevelOpener | null {
  return useContext(OpenLevelContext)
}
