/**
 * The mutable half of what the UI loads — workflow invocations, step pins, and the
 * levels built from payload-free overviews — kept current while a job runs. Full invocations
 * are separate entries loaded by custom renderers. Output pages belong to their mounted panes.
 *
 * **Entries.** Each record lives in one `Entry`, created on first use and kept for the page. Its
 * first load suspends the reader (so `Suspense` and `ErrorBoundary` handle it as before); every
 * later update is published in place and re-renders its readers through `useSyncExternalStore`,
 * never suspending again. Levels build directly from overview metadata without loading arrays.
 *
 * **Polling.** Components *watch* the entries they show (`useLevel`, `useWatchEntry`,
 * `useStepPin` watch while mounted). While a job is being viewed (`useLivePolling`) and anything
 * it watches is live, each tick refreshes watched overview metadata and asks for the
 * `invocationVersion` of complete invocation entries. Changed overviews and forced refreshes
 * invalidate watched step pins, including pins that looked settled before a resume. Live pins
 * also refresh for log lines, and complete invocation records can report changed pin states.
 * Once nothing watched is live, polling stops; watching something live, or `pokeLive`, restarts
 * it. A failed poll keeps the data, marks the connection as reconnecting, and backs off.
 */

import { useEffect, useSyncExternalStore } from 'react'
import type { Level } from '../types'
import { isOverviewLive, buildLevel } from '../model/level'
import { isLive, runStateOf } from '../lib/progress'
import {
  fetchInvocation,
  fetchStep,
  fetchVersions,
  fetchOverview,
  type GqlInvocation,
  type LevelOverview,
  type GqlStepPin,
} from './graphql'
import { recordFinishes } from './finishes'
import { retryFailedResources } from './resources'

/** How often a live job is polled. */
export const POLL_INTERVAL = 1500
/** How long to wait after a failed poll before trying again. */
const BACKOFF_INTERVAL = 5000

type EntryState<T> =
  | { status: 'pending'; promise: Promise<void> }
  | { status: 'ready'; value: T }
  | { status: 'error'; error: unknown }

/** One mutable record: pending until its first value, then republished in place. */
export class Entry<T> {
  state: EntryState<T>
  /** How many mounted components are showing this entry; the poller keeps watched ones current. */
  watchers = 0
  /** Undoes whatever this entry subscribed to, when it is dropped. */
  dispose: () => void = () => {}
  private readonly listeners = new Set<() => void>()
  private settleFirst: () => void = () => {}

  constructor() {
    this.state = { status: 'pending', promise: new Promise<void>((resolve) => (this.settleFirst = resolve)) }
  }

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getSnapshot = () => this.state

  get value(): T | undefined {
    return this.state.status === 'ready' ? this.state.value : undefined
  }

  publish(value: T) {
    this.state = { status: 'ready', value }
    this.settleFirst()
    for (const listener of [...this.listeners]) listener()
  }

  /** A failed first load; a failed refresh keeps the value instead (see `markFailedPoll`). */
  fail(error: unknown) {
    if (this.state.status !== 'pending') throw new Error('only a first load can fail an entry')
    this.state = { status: 'error', error }
    this.settleFirst()
    for (const listener of [...this.listeners]) listener()
  }
}

/** The entry's value, suspending on its first load and throwing its first load's error. */
function useEntryValue<T>(entry: Entry<T>): T {
  const state = useSyncExternalStore(entry.subscribe, entry.getSnapshot)
  if (state.status === 'pending') throw state.promise
  if (state.status === 'error') throw state.error
  return state.value
}

const subscribeNothing = () => () => {}
const nothing = () => null

/** The entry's state without suspending, for readers that report loading themselves; null for none. */
export function useEntryState<T>(entry: Entry<T> | null): EntryState<T> | null {
  return useSyncExternalStore(entry?.subscribe ?? subscribeNothing, entry?.getSnapshot ?? nothing)
}

/** Keep the entry current while the calling component is mounted. */
function useWatch(entry: Entry<unknown> | null) {
  useEffect(() => {
    if (!entry) return
    entry.watchers += 1
    kick()
    return () => {
      entry.watchers -= 1
    }
  }, [entry])
}

// ---------------------------------------------------------------------------
// Records
// ---------------------------------------------------------------------------

/** A workflow invocation as last fetched, with the version it had when that fetch began. */
export interface InvocationSnapshot {
  invocation: GqlInvocation
  version: string | null
}

interface Keyed<T> {
  jobId: string
  entry: Entry<T>
}

const invocations = new Map<string, Keyed<InvocationSnapshot> & { pathname: string }>()
export interface OverviewSnapshot { invocation: LevelOverview; version: string; receivedAt: number }

const overviews = new Map<string, Keyed<OverviewSnapshot> & { pathname: string }>()

/** An overview as fetched: `receivedAt` is this browser's time when it arrived (`Date.now()`). Every
 *  fetch is also recorded with `recordFinishes`, changed or not, so the gap between two fetches is
 *  known. */
async function fetchOverviewSnapshot(jobId: string, pathname: string, previous?: LevelOverview): Promise<OverviewSnapshot> {
  const invocation = await fetchOverview(jobId, pathname, previous)
  const receivedAt = Date.now()
  recordFinishes(jobId, invocation, receivedAt)
  return { invocation, version: invocation.version, receivedAt }
}
const levels = new Map<string, Keyed<Level>>()
const steps = new Map<string, Keyed<GqlStepPin> & { cacheKey: string }>()

const invocationKey = (jobId: string, pathname: string) => `${jobId}\u0000${pathname}`

/** Start an entry's first load; its arrival may make something watched live, so polling is kicked. */
function startLoad<T>(entry: Entry<T>, load: () => Promise<T>) {
  load().then(
    (value) => {
      entry.publish(value)
      kick()
    },
    (error: unknown) => entry.fail(error),
  )
}

export function invocationEntry(jobId: string, pathname: string): Entry<InvocationSnapshot> {
  const key = invocationKey(jobId, pathname)
  let found = invocations.get(key)
  if (!found) {
    const entry = new Entry<InvocationSnapshot>()
    found = { jobId, pathname, entry }
    invocations.set(key, found)
    startLoad(entry, async () => {
      // The version is read before the record, so a change landing between the two shows up as a
      // version newer than the record's, and the next poll refetches.
      const version = (await fetchVersions(jobId, [pathname])).get(pathname) ?? null
      return { invocation: await fetchInvocation(jobId, pathname), version }
    })
  }
  return found.entry
}

/** Payload-free records used by the trace; custom renderers retain the full invocation entry. */
export function overviewEntry(jobId: string, pathname: string): Entry<OverviewSnapshot> {
  const key = invocationKey(jobId, pathname)
  let found = overviews.get(key)
  if (!found) {
    const entry = new Entry<OverviewSnapshot>()
    found = { jobId, pathname, entry }
    overviews.set(key, found)
    startLoad(entry, () => fetchOverviewSnapshot(jobId, pathname))
  }
  return found.entry
}

export function useOverviewSnapshot(jobId: string, pathname: string): OverviewSnapshot {
  const entry = overviewEntry(jobId, pathname)
  useWatch(entry)
  return useEntryValue(entry)
}

/** One workflow invocation's complete record, kept current while watched. */
export function useInvocationSnapshot(jobId: string, pathname: string): InvocationSnapshot {
  const entry = invocationEntry(jobId, pathname)
  useWatch(entry)
  return useEntryValue(entry)
}

/** A level, rebuilt from its invocation's entry whenever that is published. */
export function levelEntry(jobId: string, pathname: string): Entry<Level> {
  const key = invocationKey(jobId, pathname)
  let found = levels.get(key)
  if (!found) {
    const entry = new Entry<Level>()
    found = { jobId, entry }
    levels.set(key, found)
    const source = overviewEntry(jobId, pathname)
    let built: OverviewSnapshot | null = null
    const rebuild = () => {
      const state = source.getSnapshot()
      if (state.status === 'error' && entry.state.status === 'pending') entry.fail(state.error)
      if (state.status !== 'ready' || state.value === built) return
      const snapshot = state.value
      built = snapshot
      try { entry.publish(buildLevel(jobId, snapshot.invocation, snapshot.receivedAt)) }
      catch (error) {
        if (entry.state.status === 'pending') entry.fail(error)
        else markFailedPoll()
      }
    }
    entry.dispose = source.subscribe(rebuild)
    rebuild()
  }
  return found.entry
}

export function stepEntry(jobId: string, cacheKey: string): Entry<GqlStepPin> {
  const key = invocationKey(jobId, cacheKey)
  let found = steps.get(key)
  if (!found) {
    const entry = new Entry<GqlStepPin>()
    found = { jobId, cacheKey, entry }
    steps.set(key, found)
    startLoad(entry, () => fetchStep(jobId, cacheKey))
  }
  return found.entry
}

// ---------------------------------------------------------------------------
// Hooks
// ---------------------------------------------------------------------------

/** One workflow invocation's graph and progress; contents are queried only when opened. */
export function useLevel(jobId: string, pathname: string): Level {
  const source = overviewEntry(jobId, pathname)
  useWatch(source)
  return useEntryValue(levelEntry(jobId, pathname))
}

/** One step's pin, kept current while watched. */
export function useStepPin(jobId: string, cacheKey: string): GqlStepPin {
  const entry = stepEntry(jobId, cacheKey)
  useWatch(entry)
  return useEntryValue(entry)
}

/** Watch an entry the caller reads through `useEntryState` (null for none). */
export function useWatchEntry(entry: Entry<unknown> | null) {
  useWatch(entry)
}

/** Keep a level current, and call `changed` whenever it is published, until the returned function
 *  is called: for a reader of many levels outside one component's render (the detail view's
 *  addresses, `panes/catalog.ts`). */
export function watchLevel(jobId: string, pathname: string, changed: () => void): () => void {
  const source = overviewEntry(jobId, pathname)
  const unsubscribe = levelEntry(jobId, pathname).subscribe(changed)
  source.watchers += 1
  kick()
  return () => {
    unsubscribe()
    source.watchers -= 1
  }
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

export interface LiveStatus {
  /** Whether the viewed job is being polled: something it shows may still change. */
  polling: boolean
  /** Whether the last poll failed; the data on screen is what the last good one returned. */
  reconnecting: boolean
}

let status: LiveStatus = { polling: false, reconnecting: false }
const statusListeners = new Set<() => void>()

function setStatus(next: Partial<LiveStatus>) {
  status = { ...status, ...next }
  for (const listener of [...statusListeners]) listener()
}

function markFailedPoll() {
  setStatus({ reconnecting: true })
}

export function useLiveStatus(): LiveStatus {
  return useSyncExternalStore(
    (listener) => {
      statusListeners.add(listener)
      return () => statusListeners.delete(listener)
    },
    () => status,
  )
}

let polledJob: string | null = null
let timer: number | null = null
let inFlight = false
let forceNext = false

/** Poll the job while the calling component (the job's view) is mounted. */
export function useLivePolling(jobId: string) {
  useEffect(() => {
    startPolling(jobId)
    return () => stopPolling(jobId)
  }, [jobId])
}

/** Make `jobId` the polled job (what `useLivePolling` does on mount). */
export function startPolling(jobId: string) {
  polledJob = jobId
  setStatus({ polling: false, reconnecting: false })
  kick()
}

/** Stop polling `jobId`, if it is still the polled job (what `useLivePolling` does on unmount). */
export function stopPolling(jobId: string) {
  if (polledJob !== jobId) return
  polledJob = null
  if (timer !== null) window.clearTimeout(timer)
  timer = null
  setStatus({ polling: false, reconnecting: false })
}

/** The live status outside React, for tests. */
export function liveStatus(): LiveStatus {
  return status
}

/**
 * Poll now even if nothing watched looks live: what the job list said (a job resumed elsewhere
 * shows as running there) may be newer than the records on screen. A poll already in flight is
 * followed by another as soon as it finishes.
 */
export function pokeLive() {
  forceNext = true
  if (timer !== null) window.clearTimeout(timer)
  timer = null
  kick()
}

/** Start polling if it is not already scheduled; a tick decides whether there is anything to do. */
function kick() {
  if (timer !== null || inFlight || polledJob === null) return
  timer = window.setTimeout(tick, 0)
}

function schedule(delay: number) {
  timer = window.setTimeout(tick, delay)
}

async function tick() {
  timer = null
  const jobId = polledJob
  if (jobId === null) return
  const force = forceNext
  const watchedInvocations = [...invocations.values()].filter(
    (found) => found.jobId === jobId && found.entry.watchers > 0 && found.entry.value !== undefined,
  )
  const watchedOverviews = [...overviews.values()].filter(
    (found) => found.jobId === jobId && found.entry.watchers > 0 && found.entry.value !== undefined,
  )
  const watchedSteps = [...steps.values()].filter(
    (found) => found.jobId === jobId && found.entry.watchers > 0 && found.entry.value !== undefined,
  )
  const live =
    watchedOverviews.some(found => isOverviewLive(found.entry.value!.invocation)) ||
    watchedInvocations.some(({entry}) => isLive(runStateOf(entry.value!.invocation.state)) || entry.value!.invocation.spawns.some(spawn => isLive(runStateOf(spawn.state)))) ||
    watchedSteps.some((found) => isLive(runStateOf(found.entry.value!.state)))
  if (!live && !force) {
    setStatus({ polling: false })
    return
  }
  if (document.hidden) {
    schedule(POLL_INTERVAL)
    return
  }
  forceNext = false
  setStatus({ polling: live })
  inFlight = true
  try {
    let overviewChanged = false
    await Promise.all(watchedOverviews.map(async (found) => {
      const snapshot = await fetchOverviewSnapshot(jobId, found.pathname, found.entry.value!.invocation)
      if (snapshot.version !== found.entry.value!.version) {
        overviewChanged = true
        found.entry.publish(snapshot)
      }
    }))
    await poll(jobId, watchedInvocations, watchedSteps, overviewChanged || force)
    if (polledJob === jobId) setStatus({ reconnecting: false })
    inFlight = false
    if (polledJob === jobId) schedule(forceNext ? 0 : POLL_INTERVAL)
  } catch {
    inFlight = false
    if (polledJob === jobId) {
      // An overview may already be published while its pin refresh failed. Retry the
      // invalidated pins even though that overview's version will now look unchanged.
      forceNext = true
      markFailedPoll()
      schedule(BACKOFF_INTERVAL)
    }
  }
}

async function poll(
  jobId: string,
  watchedInvocations: (Keyed<InvocationSnapshot> & { pathname: string })[],
  watchedSteps: (Keyed<GqlStepPin> & { cacheKey: string })[],
  refreshSteps: boolean,
) {
  const versions = await fetchVersions(
    jobId,
    watchedInvocations.map((found) => found.pathname),
  )
  const moved = watchedInvocations.filter(
    (found) => versions.get(found.pathname) !== found.entry.value!.version,
  )
  await Promise.all(
    moved.map(async (found) => {
      const previous = found.entry.value!.invocation
      const invocation = await fetchInvocation(jobId, found.pathname, previous)
      found.entry.publish({ invocation, version: versions.get(found.pathname) ?? null })
    }),
  )
  // What each step's pin should now say, according to the invocations just fetched.
  const reported = new Map<string, string>()
  for (const found of watchedInvocations)
    for (const spawn of found.entry.value!.invocation.spawns)
      if (spawn.stepCacheKey !== null) reported.set(spawn.stepCacheKey, spawn.state)
  const stale = watchedSteps.filter((found) => {
    const pin = found.entry.value!
    const expected = reported.get(found.cacheKey)
    return refreshSteps || isLive(runStateOf(pin.state)) || (expected !== undefined && expected !== pin.state)
  })
  await Promise.all(
    stale.map(async (found) => found.entry.publish(await fetchStep(jobId, found.cacheKey))),
  )
}

// ---------------------------------------------------------------------------
// Retrying
// ---------------------------------------------------------------------------

/** Forget every record whose first load failed, so the next read requests it again. */
function retryFailedLive() {
  for (const map of [invocations, overviews, levels, steps] as Map<string, Keyed<unknown>>[]) {
    for (const [key, found] of map) {
      if (found.entry.state.status !== 'error') continue
      found.entry.dispose()
      map.delete(key)
    }
  }
}

/** What every "retry" button does: forget the failed loads, live and immutable, then clear the
 *  error boundary that offered the button, so the render that follows requests them again. */
export function retryAndReset(reset: () => void): void {
  retryFailedResources()
  retryFailedLive()
  reset()
}
