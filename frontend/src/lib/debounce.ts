import { useEffect, useState } from 'react'

/** How long typing must pause before what was typed applies. */
const TYPING_PAUSE_MS = 250

/** What is typed into a field (`value`, following every keystroke), handed to `apply` once typing
 *  pauses; `pending` while it differs from `applied`. */
export function useDebouncedInput<T>(applied: T, apply: (value: T) => void) {
  const [value, set] = useState(applied)
  useEffect(() => {
    if (value === applied) return
    const timer = setTimeout(() => apply(value), TYPING_PAUSE_MS)
    return () => clearTimeout(timer)
  }, [value])
  return { value, set, pending: value !== applied }
}
