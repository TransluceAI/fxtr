/**
 * What the UI loads that never changes, behind one promise cache that components suspend on
 * through `read`: where functions are implemented and the project
 * EXPERIMENT.md. Nothing here is ever refetched within a page load. The records that change while a job
 * runs are in `live.ts`; entities are loaded by the view system's shell (`views/`).
 *
 * A rejected promise stays in the cache, and `retryFailedResources` is what forgets the failures so
 * a retry can happen after the backend comes back. Dropping a rejection as soon as it happened
 * would be a retry loop instead of a cache: `read` suspends on the pending promise, and React
 * re-renders once it settles — if the entry is gone by then, that render starts a *new* request and
 * suspends again, for as long as the load keeps failing.
 */

import { EXPERIMENT_README_URL, FILE_URL, SOURCE_BASE } from '../config'
import type { ExperimentReadmeInfo, FileInfo, SourceInfo } from '../types'

const cache = new Map<string, Promise<unknown>>()

/** A promise that records how it settled, so a render can read it synchronously. */
type Tracked<T> = Promise<T> & {
  status?: 'pending' | 'fulfilled' | 'rejected'
  value?: T
  reason?: unknown
}

/**
 * The settled value of a cached promise, or suspend: throws the promise itself while it is pending
 * (React retries the render when it settles) and rethrows its rejection, which the nearest
 * `ErrorBoundary` shows. The first read of a promise starts tracking it.
 */
function read<T>(promise: Promise<T>): T {
  const tracked = promise as Tracked<T>
  switch (tracked.status) {
    case 'fulfilled':
      return tracked.value as T
    case 'rejected':
      throw tracked.reason
    case 'pending':
      throw tracked
  }
  tracked.status = 'pending'
  tracked.then(
    (value) => {
      tracked.status = 'fulfilled'
      tracked.value = value
    },
    (reason: unknown) => {
      tracked.status = 'rejected'
      tracked.reason = reason
    },
  )
  throw tracked
}

function resource<T>(key: string, load: () => Promise<T>): Promise<T> {
  let promise = cache.get(key) as Promise<T> | undefined
  if (!promise) {
    promise = load()
    cache.set(key, promise)
    // Mark the rejection handled here; `read` attaches the handlers that record how it settled.
    promise.catch(() => {})
  }
  return promise
}

/**
 * Forget every load that failed, so the next render requests it again. Part of what a "retry"
 * button does (`retryAndReset` in `live.ts`); a failure nothing retries stays cached, and is
 * requested once.
 */
export function retryFailedResources(): void {
  for (const [key, promise] of cache) {
    if ((promise as Tracked<unknown>).status === 'rejected') cache.delete(key)
  }
}

/**
 * Where a step or workflow is implemented, from the backend: as `codeVersion` (a CID) records it
 * when given and it records the function in the repository, else as the backend's own registry
 * has it; null if it is in neither.
 */
export const loadSource = (name: string, codeVersion: string | null): Promise<SourceInfo | null> =>
  resource(`source:${codeVersion ?? ''}:${name}`, async () => {
    const query = codeVersion === null ? '' : `?codeVersion=${encodeURIComponent(codeVersion)}`
    const response = await fetch(`${SOURCE_BASE}/${encodeURIComponent(name)}${query}`)
    if (response.status === 404) return null
    if (!response.ok) throw new Error(`source: ${response.status} ${response.statusText}`)
    return (await response.json()) as SourceInfo
  })
export const useSource = (name: string, codeVersion: string | null): SourceInfo | null =>
  read(loadSource(name, codeVersion))

/** A file of the project's repository, at `commit` or (null) in the working tree. */
export const loadFile = (path: string, commit: string | null): Promise<FileInfo> =>
  resource(`file:${commit ?? ''}:${path}`, async () => {
    const query = new URLSearchParams({ path, ...(commit === null ? {} : { commit }) })
    const response = await fetch(`${FILE_URL}?${query}`)
    if (!response.ok) throw new Error(`${path}: ${(await response.text()) || response.statusText}`)
    return (await response.json()) as FileInfo
  })
export const useFile = (path: string, commit: string | null): FileInfo => read(loadFile(path, commit))

/** The project's EXPERIMENT.md, from the backend; null when the project has none (the sidebar link then
 *  does not appear). */
export const loadExperimentReadme = (): Promise<ExperimentReadmeInfo | null> =>
  resource('experiment_readme', async () => {
    const response = await fetch(EXPERIMENT_README_URL)
    if (response.status === 404) return null
    if (!response.ok) throw new Error(`experiment readme: ${response.status} ${response.statusText}`)
    return (await response.json()) as ExperimentReadmeInfo
  })
export const useExperimentReadme = (): ExperimentReadmeInfo | null => read(loadExperimentReadme())
