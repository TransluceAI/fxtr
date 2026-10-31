/**
 * Run state, the one vocabulary every surface shows progress in (see the state glyph,
 * `components/StateGlyph.tsx`).
 *
 * The backend reports invocation states (`queued`, `running`, `completed`, `failed`, `interrupted`,
 * `stopped`) and step states (the same plus `conflict`, but not `interrupted`); `runStateOf` folds both
 * into a `RunState`, plus `waiting` for a run node whose inputs have no values yet, so it has no
 * spawns.
 *
 * Anything standing for several invocations (a card, a key value, a level) shows a `Glyph` with two
 * channels: its **shape** is the most active state present, so activity is never hidden, and it is
 * **failed** (drawn in the danger colour) when anything in it failed, so a failure is never hidden
 * either. A run still going with one failed invocation is a red running glyph; one that has
 * finished with failures is a red ×.
 *
 * A run's card and flowchart node show its progress as a line instead (`components/ProgressLine.tsx`)
 * and say in words what the line cannot: failures and stopped invocations. The
 * helpers at the end of this file are what they and the hover card read.
 */

import type { RunActivity } from '../types'

export type RunState = 'waiting' | 'queued' | 'running' | 'done' | 'failed' | 'stopped'

export interface Glyph {
  shape: RunState
  failed: boolean
}

/** How many of a run's invocations are in each state. */
export interface Counts {
  total: number
  done: number
  running: number
  queued: number
  failed: number
  stopped: number
}

const FROM_BACKEND: Record<string, RunState> = {
  queued: 'queued',
  running: 'running',
  completed: 'done',
  failed: 'failed',
  interrupted: 'stopped',
  conflict: 'stopped',
  stopped: 'stopped',
}

/** A backend invocation or step state as a run state. */
export function runStateOf(state: string): RunState {
  const mapped = FROM_BACKEND[state]
  if (mapped === undefined) throw new Error(`unknown invocation or step state ${JSON.stringify(state)}`)
  return mapped
}

/** Most active first: an aggregate takes the first of these any member is in. */
const SHAPE_ORDER: readonly RunState[] = ['running', 'queued', 'waiting', 'failed', 'stopped', 'done']

/** The glyph of several invocations together. There must be at least one. */
export function aggregate(states: Iterable<RunState>): Glyph {
  const present = new Set(states)
  const shape = SHAPE_ORDER.find((state) => present.has(state))
  if (shape === undefined) throw new Error('aggregate needs at least one state')
  return { shape, failed: present.has('failed') }
}

/** The glyph of one invocation. */
export function glyphOf(state: RunState): Glyph {
  return { shape: state, failed: state === 'failed' }
}

export function countStates(states: Iterable<RunState>): Counts {
  const counts: Counts = { total: 0, done: 0, running: 0, queued: 0, failed: 0, stopped: 0 }
  for (const state of states) {
    counts.total += 1
    if (state !== 'waiting') counts[state] += 1
  }
  return counts
}

/** Whether something in this state may still change on its own. */
export function isLive(state: RunState): boolean {
  return state === 'running' || state === 'queued' || state === 'waiting'
}

/** One piece of a run's counts, as `describeCounts` words it. */
export interface CountPart {
  state: 'done' | 'running' | 'failed' | 'queued' | 'stopped'
  text: string
}

const COUNT_ORDER = ['done', 'running', 'failed', 'queued', 'stopped'] as const

/**
 * A run's counts in words, leaving out the states nothing is in: `37 done · 4 running · 1 failed ·
 * 78 queued`; `all 120 done` once every invocation is done; and for a run of one invocation just
 * its state (`running`).
 */
export function countParts(counts: Counts): CountPart[] {
  const present = COUNT_ORDER.filter((state) => counts[state] > 0)
  if (counts.total === 1) return present.map((state) => ({ state, text: state }))
  if (counts.total > 0 && counts.done === counts.total) return [{ state: 'done', text: `all ${counts.total} done` }]
  return present.map((state) => ({ state, text: `${counts[state]} ${state}` }))
}

export function describeCounts(counts: Counts): string {
  return countParts(counts)
    .map((part) => part.text)
    .join(' · ')
}

/** A glyph's colour: danger for any failure, ink while running, the done grey, else quiet. */
export type Tone = 'danger' | 'ink' | 'done' | 'quiet'

export function toneOf({ shape, failed }: Glyph): Tone {
  return failed ? 'danger' : shape === 'running' ? 'ink' : shape === 'done' ? 'done' : 'quiet'
}

/**
 * How far a workflow has got, from 0 to 1: the mean over its runs of how much of each has
 * finished, a run that has settled counting whole and one not expanded yet as nothing. Pooling the
 * runs' invocations instead would read whole once the first runs finish, since a run has no
 * invocations until its inputs have values. With no runs, whole unless `live`.
 */
export function workflowProgress(runs: readonly { glyph: Glyph; counts: Counts | null }[], live: boolean): number {
  const shares = runs.flatMap(({ glyph, counts }) =>
    counts === null ? [] : [isLive(glyph.shape) ? settledFraction(counts) : 1])
  if (shares.length === 0) return live ? 0 : 1
  return shares.reduce((sum, share) => sum + share, 0) / shares.length
}

/** How much of a run has finished (done, failed, or stopped), from 0 to 1. */
export function settledFraction(counts: Counts): number {
  return counts.total === 0 ? 0 : (counts.done + counts.failed + counts.stopped) / counts.total
}

/**
 * A job's glyph, from the backend's `JobState` (`running`, `succeeded`, `stopped`) and whether it
 * stopped with a failure. A stopped job that did not fail was cancelled or interrupted, or nothing
 * has taken it yet.
 */
export function jobGlyph(state: string, failed: boolean): Glyph {
  switch (state) {
    case 'succeeded':
      return glyphOf('done')
    case 'running':
      return glyphOf('running')
    case 'stopped':
      return glyphOf(failed ? 'failed' : 'stopped')
    default:
      throw new Error(`unknown job state ${JSON.stringify(state)}`)
  }
}

/** How much of a run has finished, completed or failed, from 0 to 1: the percentage a card head
 *  shows. Stopped invocations are left out of both, and named in words instead. */
export function finishedFraction(counts: Counts): number {
  return counts.total === 0 ? 0 : (counts.done + counts.failed) / counts.total
}

/** The shares of a run's invocations its progress line draws, of all of them, from 0 to 1. */
export function lineShares(counts: Counts): { done: number; failed: number; running: number } {
  const share = (n: number) => (counts.total === 0 ? 0 : n / counts.total)
  return { done: share(counts.done), failed: share(counts.failed), running: share(counts.running) }
}

/**
 * A run's usual time between finishes, in milliseconds: from its first start to its last finish,
 * over how many it finished. Null before it has finished `MIN_FINISHES_FOR_GAP`, when there is too
 * little to go on.
 *
 * It is an average over the whole run, not its recent pace: the ramp-up before the first finish
 * counts, and a run that has slowed down keeps the gap of its faster start. Only attempts the job ran
 * itself count (`RunActivity`), so a run whose results mostly came from another job's cache has
 * few finishes to go on. It stands in for the rate before the page has seen enough (`finishRate`).
 */
export function usualGap(activity: RunActivity): number | null {
  const { firstStartedAt, lastFinishedAt, finished } = activity
  if (firstStartedAt === null || lastFinishedAt === null || finished < MIN_FINISHES_FOR_GAP) return null
  return Math.max(0, lastFinishedAt - firstStartedAt) / finished
}

export const MIN_FINISHES_FOR_GAP = 3

/** How long a rate is measured over, at most. */
const RATE_WINDOW_MS = 30_000
/** Less than this much of a finish log, and its rate means little. */
const MIN_RATE_WINDOW_MS = 10_000

/**
 * A run's finishes per second: over the last 30 seconds of what this page has seen
 * (`finishes`, times on this browser's clock, recorded since `since`), or, before the page has
 * seen 10 seconds of it, the run's average (one per usual gap). Null when neither is known.
 */
export function finishRate(
  finishes: readonly { at: number }[] | null,
  since: number | null,
  activity: RunActivity,
  now: number,
): number | null {
  if (finishes !== null && since !== null && now - since >= MIN_RATE_WINDOW_MS) {
    const window = Math.min(RATE_WINDOW_MS, now - since)
    return (finishes.filter((finish) => finish.at > now - window).length / window) * 1000
  }
  const gap = usualGap(activity)
  return gap === null || gap === 0 ? null : 1000 / gap
}

/** Seconds until a run has finished everything still queued or running, at `rate` per second;
 *  null when the rate is unknown or zero. */
export function timeLeft(counts: Counts, rate: number | null): number | null {
  if (rate === null || rate <= 0) return null
  return (counts.queued + counts.running) / rate
}
