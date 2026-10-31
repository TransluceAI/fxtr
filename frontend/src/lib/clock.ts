/**
 * One shared clock for everything that ticks (a running invocation's elapsed time, "updated 2s
 * ago"): a single interval, running only while something reads it, so every timer on screen
 * advances together.
 */

import { useSyncExternalStore } from 'react'

const TICK_MS = 1000

let now = Date.now()
let timer: number | null = null
const listeners = new Set<() => void>()

function subscribe(listener: () => void) {
  listeners.add(listener)
  if (timer === null) {
    now = Date.now()
    timer = window.setInterval(() => {
      now = Date.now()
      for (const each of [...listeners]) each()
    }, TICK_MS)
  }
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0 && timer !== null) {
      window.clearInterval(timer)
      timer = null
    }
  }
}

/** The current time (`Date.now()`), re-rendering the caller once a second. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, () => now)
}
