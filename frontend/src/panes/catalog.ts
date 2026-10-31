/**
 * The detail view's catalog (`resolve.ts`'s `Catalog`): every level of the job, read from the live
 * store as addresses ask for them. Asking for a level starts its load and watches it from then on,
 * until the job changes or the view unmounts; each time a watched level is published, the catalog
 * is a new object, so whatever resolved against it resolves again.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { DispatchRegistry } from 'fxtr-view-shell-util'
import { levelEntry, watchLevel } from '../api/live'
import type { Catalog, LevelLookup } from './resolve'

export function useJobCatalog(jobId: string, root: string, registry: DispatchRegistry): Catalog {
  const [version, setVersion] = useState(0)
  const watched = useRef(new Map<string, () => void>())
  useEffect(() => {
    const stops = watched.current
    return () => {
      for (const stop of stops.values()) stop()
      stops.clear()
    }
  }, [jobId])
  return useMemo<Catalog>(() => ({
    root,
    registry,
    level: (pathname): LevelLookup => {
      if (!watched.current.has(pathname))
        watched.current.set(pathname, watchLevel(jobId, pathname, () => setVersion((current) => current + 1)))
      const state = levelEntry(jobId, pathname).state
      if (state.status === 'ready') return { status: 'ready', level: state.value }
      if (state.status === 'error') return { status: 'error', error: state.error }
      return { status: 'pending' }
    },
    // `version` is not read: it is there to make a published level a new catalog.
  }), [jobId, root, registry, version])
}
