import { useEffect, useMemo, useRef } from 'react'

/**
 * The ids that were not there the last time the caller rendered the same `scope` (a level), so
 * they can fade in once as a job in progress adds them. Nothing is fresh on a scope's first render,
 * nor when the scope changes. The set stays the same until the ids change again, so the fade is not
 * cut short by an unrelated re-render.
 */
export function useFreshIds(ids: readonly string[], scope: string): ReadonlySet<string> {
  const seen = useRef<{ scope: string; ids: ReadonlySet<string> } | null>(null)
  const signature = ids.join('\n')
  const fresh = useMemo(() => {
    const previous = seen.current
    if (!previous || previous.scope !== scope) return new Set<string>()
    return new Set(ids.filter((id) => !previous.ids.has(id)))
    // `signature` stands for `ids`: a new array with the same ids is the same set.
  }, [scope, signature])
  useEffect(() => {
    seen.current = { scope, ids: new Set(ids) }
  }, [scope, signature])
  return fresh
}
