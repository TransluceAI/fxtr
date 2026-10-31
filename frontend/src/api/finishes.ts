/**
 * What finished when, as far as this page has seen: for each run node, its finishes (invocations
 * that completed or failed) over the last few minutes, from successive overview fetches. The live
 * store records every fetch here (`recordFinishes`), changed or not.
 *
 * An overview carries counts, not finish times, so a fetch only says how many invocations finished
 * since the fetch before. Those are placed evenly across the time between the two fetches, the
 * failures spread among them. At the live store's poll interval that is within 1.5 seconds of when
 * they really finished, which is what the hover card's heartbeat draws (`components/Heartbeat.tsx`).
 * A count going down (a failed invocation retried) adds nothing. The record starts when the page
 * first fetches the level, and times are this browser's (`Date.now()`).
 *
 * So the times are made up, within a poll: finishes that came in a burst look evenly spaced, and a
 * gap longer than a poll (the tab hidden, polling backed off after an error) spreads its finishes
 * thinly across all of it. Nothing from before the page opened is known, so the heartbeat starts
 * empty and the rate falls back to the run's average for its first seconds (`finishRate`). The
 * fix is finish times per invocation in the overview, which the backend does not return.
 */

import { useSyncExternalStore } from 'react'
import type { LevelOverview } from './graphql'

export interface Finish {
  at: number
  failed: boolean
}

export interface FinishLog {
  /** When this page started recording the node: nothing before it is known. */
  since: number
  /** Oldest first, none older than `KEEP_MS`. */
  finishes: readonly Finish[]
}

/** How far back finishes are kept: the heartbeat's minute, and room for a rate's window. */
const KEEP_MS = 3 * 60_000

interface Tracked {
  log: FinishLog
  at: number
  done: number
  failed: number
}

const records = new Map<string, Tracked>()
const listeners = new Map<string, Set<() => void>>()

const keyOf = (jobId: string, pathname: string, nodeId: string) => `${jobId}\u0000${pathname}\u0000${nodeId}`

/** Record one fetch of an overview, arrived at `at`. */
export function recordFinishes(jobId: string, overview: LevelOverview, at: number) {
  for (const node of overview.nodes) {
    if (node.counts === null) continue
    const key = keyOf(jobId, overview.pathname, node.identity)
    const { completed: done, failed } = node.counts
    const record = records.get(key)
    if (!record) {
      records.set(key, { log: { since: at, finishes: [] }, at, done, failed })
      continue
    }
    const added = spread(record.at, at, Math.max(0, done - record.done), Math.max(0, failed - record.failed))
    const cutoff = at - KEEP_MS
    const kept = record.log.finishes[0] !== undefined && record.log.finishes[0].at < cutoff
      ? record.log.finishes.filter((finish) => finish.at >= cutoff)
      : record.log.finishes
    Object.assign(record, { at, done, failed })
    if (added.length === 0 && kept === record.log.finishes) continue
    record.log = { since: record.log.since, finishes: [...kept, ...added] }
    for (const listener of [...(listeners.get(key) ?? [])]) listener()
  }
}

/** `done + failed` finishes evenly across `from`–`to`, with the failures evenly among them. */
export function spread(from: number, to: number, done: number, failed: number): Finish[] {
  const total = done + failed
  return Array.from({ length: total }, (_, index) => ({
    at: from + ((index + 0.5) / total) * (to - from),
    failed: Math.floor(((index + 1) * failed) / total) > Math.floor((index * failed) / total),
  }))
}

/** The node's finish log, re-rendering the caller when it changes; null before its first fetch. */
export function useFinishLog(jobId: string, pathname: string, nodeId: string): FinishLog | null {
  const key = keyOf(jobId, pathname, nodeId)
  return useSyncExternalStore(
    (listener) => {
      let set = listeners.get(key)
      if (!set) listeners.set(key, (set = new Set()))
      set.add(listener)
      return () => set.delete(listener)
    },
    () => records.get(key)?.log ?? null,
  )
}

/** The node's finish log outside React, for tests. */
export function finishLog(jobId: string, pathname: string, nodeId: string): FinishLog | null {
  return records.get(keyOf(jobId, pathname, nodeId))?.log ?? null
}

/** Forget every record, for tests. */
export function clearFinishes() {
  records.clear()
}
