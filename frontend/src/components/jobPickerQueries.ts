import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchJobSummariesByIds } from '../api/graphql'
import type { JobSummary } from '../types'

const SUMMARY_BATCH = 50
/** Start reading a few rows before they enter the scroller. */
const ROW_BUFFER = 200
const NO_IDS: string[] = []

/** The rendered jobs in or near the scroller. `contents` changes when filters or history change
 *  its rows; moving the preview does not recreate the observer. Null disables observation. */
export function useNearbyJobIds(root: HTMLElement | null, contents: unknown): readonly string[] {
  const [ids, setIds] = useState<string[]>([])
  useEffect(() => {
    setIds([])
    if (!root) return
    const nearby = new Set<string>()
    let active = true
    const observer = new IntersectionObserver((entries) => {
      if (!active) return
      let changed = false
      for (const entry of entries) {
        const id = (entry.target as HTMLElement).dataset.jobId!
        if (entry.isIntersecting && !nearby.has(id)) {
          nearby.add(id)
          changed = true
        } else if (!entry.isIntersecting && nearby.delete(id)) changed = true
      }
      if (changed) setIds([...nearby])
    }, { root, rootMargin: `${ROW_BUFFER}px 0px` })
    for (const row of root.querySelectorAll('[data-job-id]')) observer.observe(row)
    return () => {
      active = false
      observer.disconnect()
    }
  }, [root, contents])
  return root ? ids : NO_IDS
}

interface Reads {
  fresh: Set<string>
  keep: (ids: readonly string[], found: JobSummary[]) => void
  load: () => Promise<void>
}

/** Summaries stay cached across scrolls and reopenings; freshness lasts for one opening.
 *  One batch at a time serves the latest requested rows. Scrolling leaves that batch running,
 *  then the next batch uses the current demand. A preview has its own cancellable request, even
 *  if its job is in a slow batch; the first successful response for an id wins this opening. */
export function useJobSummaries(ids: readonly string[], previewId: string | null, open: boolean) {
  const [summaries, setSummaries] = useState<ReadonlyMap<string, JobSummary>>(new Map())
  const [resolved, setResolved] = useState<ReadonlySet<string>>(new Set())
  const [error, setError] = useState<unknown>(null)
  const [attempt, setAttempt] = useState(0)
  const reload = useCallback(() => setAttempt((value) => value + 1), [])
  const wanted = useRef(ids)
  wanted.current = ids
  const reads = useRef<Reads | null>(null)

  useEffect(() => {
    if (!open) return
    setError(null)
    setResolved(new Set())
    const controller = new AbortController()
    const fresh = new Set<string>()
    let busy = false
    const keep = (batch: readonly string[], found: JobSummary[]) => {
      if (controller.signal.aborted) return
      const unread = batch.filter((id) => !fresh.has(id))
      if (unread.length === 0) return
      for (const id of unread) fresh.add(id)
      setResolved(new Set(fresh))
      const values = new Map(found.map((job) => [job.id, job]))
      setSummaries((previous) => {
        const next = new Map(previous)
        for (const id of unread) {
          const job = values.get(id)
          if (job) next.set(id, job)
          else next.delete(id)
        }
        return next
      })
    }
    const load = async () => {
      if (busy) return
      busy = true
      try {
        while (!controller.signal.aborted) {
          const batch = wanted.current.filter((id) => !fresh.has(id)).slice(0, SUMMARY_BATCH)
          if (batch.length === 0) break
          keep(batch, await fetchJobSummariesByIds(batch, controller.signal))
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure)
      } finally {
        busy = false
      }
    }
    reads.current = { fresh, keep, load }
    void load()
    return () => {
      controller.abort()
      reads.current = null
    }
  }, [open, attempt])

  const key = ids.join(',')
  useEffect(() => { void reads.current?.load() }, [key])

  useEffect(() => {
    const current = reads.current
    if (!current || previewId === null || current.fresh.has(previewId)) return
    const controller = new AbortController()
    fetchJobSummariesByIds([previewId], controller.signal).then(
      (found) => {
        if (!controller.signal.aborted) current.keep([previewId], found)
      },
      (failure: unknown) => {
        if (!controller.signal.aborted) setError(failure)
      },
    )
    return () => controller.abort()
  }, [open, previewId, attempt])

  return { summaries, resolved, error, reload }
}
