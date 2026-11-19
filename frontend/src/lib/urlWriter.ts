/**
 * The one writer of the URL and the browser's history (`lib/urlState.ts` says what is in them).
 * Browser history is not a log of every click: only switching jobs makes a history entry, and
 * everything else replaces the current one, its URL and its state (a `Snapshot` of the job's view).
 *
 * Two things write through it, in either order: `App`'s `Jobs` says which job is shown
 * (`showJob`), so a job whose view never loads is still named, and a link naming no job gets the
 * one shown, the rest of it kept; the job's view says what it shows (`show`). The writer knows
 * which job the current entry is for, so a write for another job pushes an entry, whichever of the
 * two writes first, and the other then replaces it or does nothing.
 *
 * Replacing is throttled (browsers refuse `replaceState` called many times a second, as a column
 * being resized would). A replace still waiting is written before a push, so the entry left holds
 * what was shown last; back or forward drops it (`popped`), since it is of the entry left.
 */

import { parseUrlState, withJob } from './urlState'

/** How often the current entry is replaced at most, in ms. */
const REPLACE_EVERY_MS = 300

export class UrlWriter {
  /** The job the current entry is for; null for a link naming none, until one is shown. */
  private entryJob: string | null = parseUrlState(window.location.hash).state.job
  private lastReplaceAt = 0
  private waiting: { timer: number; write: () => void } | null = null

  /** Job `job` is shown: the entry names it, a new one when it is for another job. */
  showJob(job: string): void {
    if (this.entryJob === job) return
    if (this.entryJob === null) {
      this.entryJob = job
      writeHistory('replace', window.history.state, withJob(window.location.hash, job))
    } else this.push(job, null, withJob('', job))
  }

  /** Job `job`'s view shows what `hash` (the whole URL hash, its job included) and `snapshot` say. */
  show(job: string, hash: string, snapshot: unknown): void {
    if (this.entryJob !== null && this.entryJob !== job) {
      this.push(job, snapshot, hash)
      return
    }
    this.entryJob = job
    const write = () => {
      this.waiting = null
      this.lastReplaceAt = Date.now()
      writeHistory('replace', snapshot, hash)
    }
    if (this.waiting) window.clearTimeout(this.waiting.timer)
    const wait = this.lastReplaceAt + REPLACE_EVERY_MS - Date.now()
    if (wait <= 0) write()
    else this.waiting = { timer: window.setTimeout(write, wait), write }
  }

  /** Back, forward, or an edited link: the replace waiting was of the entry left. */
  popped(): void {
    this.drop()
    this.entryJob = parseUrlState(window.location.hash).state.job
  }

  /** The page goes: what is waiting is written. */
  dispose(): void {
    this.flush()
  }

  private push(job: string, snapshot: unknown, hash: string): void {
    this.flush()
    this.entryJob = job
    writeHistory('push', snapshot, hash)
  }

  private flush(): void {
    const waiting = this.waiting
    if (!waiting) return
    window.clearTimeout(waiting.timer)
    waiting.write()
  }

  private drop(): void {
    if (this.waiting) window.clearTimeout(this.waiting.timer)
    this.waiting = null
  }
}

/**
 * Write the history: the browser may refuse (too many writes a second, a state too large to keep,
 * or one it cannot copy). Then the URL alone is written, without the entry's snapshot, so the link
 * is still right and a later opening of the entry reads it; failing that too, nothing is.
 */
function writeHistory(how: 'push' | 'replace', state: unknown, hash: string): void {
  const call = (value: unknown) =>
    how === 'push' ? window.history.pushState(value, '', hash) : window.history.replaceState(value, '', hash)
  try {
    call(state)
  } catch {
    try {
      call(null)
    } catch {
      // Left as it was.
    }
  }
}
