// @vitest-environment jsdom
import {act} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, describe, expect, it} from 'vitest'
import {clearFinishes, finishLog, recordFinishes, spread} from '../src/api/finishes'
import {heartbeatLayout, HEARTBEAT_WINDOW_MS} from '../src/components/Heartbeat'
import {RunStatus} from '../src/components/RunStatus'
import {buildLevel} from '../src/model/level'
import {
  countStates,
  finishedFraction,
  finishRate,
  lineShares,
  timeLeft,
  usualGap,
  type Counts,
} from '../src/lib/progress'
import type {RunActivity, StepDoc} from '../src/types'
import {overviewOf} from './fixtures'

const counts = (parts: Partial<Counts>): Counts => ({...countStates([]), ...parts})
const activity = (parts: Partial<RunActivity>): RunActivity =>
  ({firstStartedAt: null, lastFinishedAt: null, finished: 0, oldestRunningSince: null, ...parts})

describe('progress readings', () => {
  it('counts done and failed as finished, and leaves stopped out', () => {
    const run = counts({total: 10, done: 3, failed: 1, running: 2, queued: 2, stopped: 2})
    expect(finishedFraction(run)).toBe(0.4)
    expect(lineShares(run)).toEqual({done: 0.3, failed: 0.1, running: 0.2})
    expect(finishedFraction(counts({}))).toBe(0)
  })

  it('takes the usual gap from the job’s own finishes, once there are a few', () => {
    expect(usualGap(activity({firstStartedAt: 0, lastFinishedAt: 9_000, finished: 2}))).toBeNull()
    expect(usualGap(activity({firstStartedAt: 0, lastFinishedAt: 9_000, finished: 3}))).toBe(3_000)
  })

  it('measures the rate over what the page has seen, or else the run’s average', () => {
    const run = activity({firstStartedAt: 0, lastFinishedAt: 20_000, finished: 10})
    const finishes = Array.from({length: 60}, (_, i) => ({at: 1_000_000 - i * 500}))
    expect(finishRate(finishes, 900_000, run, 1_000_000)).toBe(2)
    // Five seconds of history is too little: the average, one per 2s, stands in.
    expect(finishRate(finishes, 995_000, run, 1_000_000)).toBe(0.5)
    expect(finishRate(null, null, activity({}), 1_000_000)).toBeNull()
    expect(timeLeft(counts({queued: 90, running: 10}), 2)).toBe(50)
    expect(timeLeft(counts({queued: 90}), null)).toBeNull()
  })
})

describe('the finish log', () => {
  afterEach(() => clearFinishes())

  /** An overview of one run, `roll`, with these counts. */
  const overview = (completed: number, failed: number) => {
    const level = overviewOf()
    const roll = level.nodes.find((node) => node.identity === 'roll')!
    roll.counts = {queued: 0, running: 0, completed, failed, interrupted: 0, conflict: 0, stopped: 0}
    return {...level, nodes: [roll]}
  }

  it('spreads each fetch’s new finishes evenly across the time since the last', () => {
    recordFinishes('job', overview(10, 0), 1_000)
    expect(finishLog('job', 'root', 'roll')).toEqual({since: 1_000, finishes: []})
    recordFinishes('job', overview(13, 1), 2_000)
    const {finishes} = finishLog('job', 'root', 'roll')!
    expect(finishes.map((finish) => finish.at)).toEqual([1_125, 1_375, 1_625, 1_875])
    expect(finishes.filter((finish) => finish.failed)).toHaveLength(1)
  })

  it('adds nothing when a count goes down, and forgets finishes after three minutes', () => {
    recordFinishes('job', overview(10, 2), 0)
    recordFinishes('job', overview(11, 1), 1_000)
    expect(finishLog('job', 'root', 'roll')!.finishes).toHaveLength(1)
    recordFinishes('job', overview(11, 1), 3 * 60_000 + 1_000)
    expect(finishLog('job', 'root', 'roll')!.finishes).toEqual([])
  })

  it('places failures among the finishes rather than at one end', () => {
    expect(spread(0, 4, 2, 2).map((finish) => finish.failed)).toEqual([false, true, false, true])
  })
})

describe('the heartbeat', () => {
  it('stacks finishes sharing a column and sizes blocks to the tallest', () => {
    const now = Date.now()
    const one = heartbeatLayout([{at: now - 10_000, failed: false}], now)
    expect(one.blockHeight).toBe(25)
    const stacked = heartbeatLayout([0, 1, 2].map(() => ({at: now - 10_000, failed: false})), now)
    expect(stacked.tallest).toBe(3)
    expect(stacked.blockHeight).toBe(7)
    expect(heartbeatLayout([{at: now - HEARTBEAT_WINDOW_MS - 5_000, failed: false}], now).columns).toEqual([])
  })

  it('draws bars instead of blocks when a column holds more finishes than the height has room for', () => {
    const now = Date.now()
    const crowded = Array.from({length: 20}, () => ({at: now - 1_000, failed: false}))
    expect(heartbeatLayout(crowded, now).blockHeight).toBeNull()
  })
})

describe('a level’s clock', () => {
  it('puts a run’s times on this browser’s clock', () => {
    const level = overviewOf()
    level.asOf = '2026-10-01T12:00:10Z'
    const roll = level.nodes.find((node) => node.identity === 'roll')!
    roll.activity = {firstStartedAt: '2026-10-01T12:00:00Z', lastFinishedAt: null, finished: 0, oldestRunningSince: null}
    // The database's clock is 10s ahead: its 12:00:10 arrived at our 12:00:00.
    const step = buildLevel('job', level, Date.parse('2026-10-01T12:00:00Z')).steps.find((s) => s.id === 'roll')!
    expect(step.progress.activity!.firstStartedAt).toBe(Date.parse('2026-10-01T11:59:50Z'))
  })
})

describe('a card head’s status', () => {
  let host: HTMLDivElement
  let root: Root
  beforeEach(() => {
    host = document.createElement('div')
    document.body.append(host)
    root = createRoot(host)
  })
  afterEach(() => {
    act(() => root.unmount())
    host.remove()
  })

  const step = (run: Counts, times: Partial<RunActivity>): StepDoc => {
    const base = buildLevel('job', overviewOf()).steps.find((s) => s.id === 'roll')!
    return {...base, progress: {...base.progress, counts: run, activity: activity(times)}}
  }
  const show = (doc: StepDoc) => {
    act(() => root.render(<RunStatus step={doc} failedOnly={false} onToggleFailed={() => {}} />))
    return host.textContent
  }

  it('says only the percentage while all is well', () => {
    const now = Date.now()
    expect(show(step(counts({total: 200, done: 86, running: 4, queued: 110}),
      {firstStartedAt: now - 60_000, lastFinishedAt: now - 500, finished: 86, oldestRunningSince: now - 3_000}))).toBe('43%')
  })

  it('says only the percentage for a quiet run, and names failures out of how many once common', () => {
    const now = Date.now()
    expect(show(step(counts({total: 200, done: 86, running: 4, queued: 110}),
      {firstStartedAt: now - 120_000, lastFinishedAt: now - 60_000, finished: 86, oldestRunningSince: now - 70_000})))
      .toBe('43%')
    expect(show(step(counts({total: 200, done: 60, failed: 4, running: 4, queued: 132}), {}))).toBe('32% · 4 failed')
    expect(show(step(counts({total: 200, done: 20, failed: 40, running: 4, queued: 136}), {}))).toBe('30% · 40 of 60 failed')
  })

  it('says done, waiting, or a run of one’s state in place of a percentage', () => {
    expect(show(step(counts({total: 200, done: 200}), {}))).toBe('done')
    expect(show(step(counts({total: 200, done: 190, failed: 10}), {}))).toBe('done · 10 failed')
    expect(show(step(counts({total: 0}), {}))).toBe('waiting')
    expect(show(step(counts({total: 1, running: 1}), {oldestRunningSince: Date.now() - 134_000}))).toBe('running · 2m14s')
  })
})
