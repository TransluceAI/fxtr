import { useCallback, useEffect, useRef, useState } from 'react'
import { useLoading } from '../lib/loading'

interface Options {
  enabled?: boolean
  /** Pages of one source can retain the displayed value while the next page loads. */
  retainKey?: string
  /** A changed revision schedules a refresh without cancelling useful work in progress. */
  version?: string
}

/** How many queries' last values `recent` keeps. */
const RECENT_LIMIT = 500

/** The last value each recent query key read, oldest first: a query mounted (or changed) to a key
 *  read before starts from its value and revalidates it as a refresh, so a view that remounts (a
 *  panel reopened, a level returned to) shows its data at once. */
const recent = new Map<string, { value: unknown }>()

/** Drop every kept value (between tests, which reuse keys over different data). */
export function forgetRecentQueries() {
  recent.clear()
}

function remember(key: string, value: unknown) {
  recent.delete(key)
  recent.set(key, { value })
  if (recent.size > RECENT_LIMIT) recent.delete(recent.keys().next().value!)
}

/** Identity changes cancel reads; refreshes coalesce into one follow-up after the current read.
 * `loading` blocks interaction with an initial or changing query, and holds back the nearest
 * `LoadingBoundary` even while a retained value is shown; `refreshing` preserves both. A key
 * names the data it reads wherever it is used (tagged by what it reads), since a key read before
 * starts from the value `recent` kept for it. */
export function useQuery<T>(
  key: string,
  load: (signal: AbortSignal) => Promise<T>,
  { enabled = true, retainKey = key, version }: Options = {},
) {
  const loader = useRef(load)
  loader.current = load
  const refresh = useRef(() => {})
  const revision = useRef(version)
  const [result, setResult] = useState<{
    key: string
    retainKey: string
    value?: T
    error?: unknown
    valueKey?: string
    fetching: boolean
  }>(() => {
    const hit = enabled ? recent.get(key) : undefined
    return { key, retainKey, fetching: true, value: hit?.value as T | undefined, valueKey: hit && key }
  })
  const reload = useCallback(() => refresh.current(), [])

  useEffect(() => {
    revision.current = version
    if (!enabled) return
    const controller = new AbortController()
    let running = false
    let dirty = false
    const run = async () => {
      if (running) {
        dirty = true
        return
      }
      running = true
      do {
        dirty = false
        setResult(previous => {
          if (previous.key !== key) {
            const hit = recent.get(key)
            if (hit) return { key, retainKey, value: hit.value as T, valueKey: key, fetching: true }
          }
          return {
            key, retainKey,
            value: previous.retainKey === retainKey ? previous.value : undefined,
            valueKey: previous.retainKey === retainKey ? previous.valueKey : undefined,
            fetching: true,
          }
        })
        try {
          const value = await loader.current(controller.signal)
          if (controller.signal.aborted) return
          remember(key, value)
          setResult({ key, retainKey, value, valueKey: key, fetching: false })
        } catch (error) {
          if (controller.signal.aborted) return
          setResult(previous => ({ ...previous, key, retainKey, error, fetching: false }))
        }
      } while (dirty)
      running = false
    }
    refresh.current = () => { void run() }
    void run()
    return () => {
      controller.abort()
      refresh.current = () => {}
    }
  }, [key, retainKey, enabled])

  useEffect(() => {
    if (revision.current !== version) {
      revision.current = version
      reload()
    }
  }, [version, reload])

  // A key changed this render, before the effect reads it: its kept value, else the retained one.
  const hit = result.key === key ? undefined : enabled ? recent.get(key) : undefined
  const shown = result.key === key && result.retainKey === retainKey ? result : hit ? {
    fetching: true,
    valueKey: key,
    value: hit.value as T,
    error: undefined,
  } : {
    fetching: true,
    valueKey: undefined,
    value: result.retainKey === retainKey ? result.value : undefined,
    error: undefined,
  }
  const loading = shown.fetching && shown.valueKey !== key
  useLoading(enabled && loading)
  return {
    value: shown.value,
    error: shown.error,
    loading,
    refreshing: shown.fetching && shown.valueKey === key,
    reload,
  }
}
