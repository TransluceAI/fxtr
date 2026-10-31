import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useReducer, useRef, type ReactNode } from 'react'

type TrackLoad = (token: object, loading: boolean) => void
const LoadingContext = createContext<TrackLoad | null>(null)

/** Reveal a staged view only after its mounted queries and loading fallbacks have settled. */
export function LoadingBoundary({ children, onReady }: { children: ReactNode; onReady?: () => void }) {
  const pending = useRef(new Set<object>())
  const [revision, changed] = useReducer(value => value + 1, 0)
  const track = useCallback<TrackLoad>((token, loading) => {
    if (pending.current.has(token) === loading) return
    if (loading) pending.current.add(token)
    else pending.current.delete(token)
    changed()
  }, [])
  // Children register in layout effects, including children mounted by a completed query.
  // Read the live set so an earlier effect cannot reveal a later loading placeholder.
  useEffect(() => { if (pending.current.size === 0) onReady?.() }, [revision, onReady])
  return <LoadingContext.Provider value={track}>{children}</LoadingContext.Provider>
}

/** Initial and changed-key loads participate in the nearest staged view; background refreshes need not. */
export function useLoading(loading: boolean) {
  const track = useContext(LoadingContext)
  const token = useRef({})
  useLayoutEffect(() => {
    if (!loading || !track) return
    const current = token.current
    track(current, true)
    return () => track(current, false)
  }, [loading, track])
}

/** A Suspense/renderer fallback counts as loading even when its suspended hooks cannot commit. */
export function LoadingContent({ children }: { children: ReactNode }) {
  useLoading(true)
  return <>{children}</>
}
