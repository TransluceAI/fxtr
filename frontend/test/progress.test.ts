import { describe, expect, it } from 'vitest'
import { aggregate, countStates, describeCounts, jobGlyph, runStateOf, workflowProgress, type Counts, type RunState } from '../src/lib/progress'

describe('run state', () => {
  it('folds every backend state into a run state and rejects unknown ones', () => {
    expect(runStateOf('completed')).toBe('done')
    expect(runStateOf('conflict')).toBe('stopped')
    expect(runStateOf('interrupted')).toBe('stopped')
    expect(runStateOf('stopped')).toBe('stopped')
    expect(() => runStateOf('abandoned')).toThrow(/unknown/)
    expect(() => runStateOf('exploded')).toThrow(/unknown/)
  })

  it('shows the most active state as the shape, and any failure as the colour', () => {
    expect(aggregate(['done', 'running', 'failed'])).toEqual({ shape: 'running', failed: true })
    expect(aggregate(['done', 'failed'])).toEqual({ shape: 'failed', failed: true })
    expect(aggregate(['done', 'queued', 'stopped'])).toEqual({ shape: 'queued', failed: false })
    expect(aggregate(['stopped', 'done'])).toEqual({ shape: 'stopped', failed: false })
    expect(aggregate(['done'])).toEqual({ shape: 'done', failed: false })
    expect(() => aggregate([])).toThrow()
  })

  it('counts and describes a run', () => {
    const counts = countStates(['done', 'done', 'running', 'failed', 'queued'])
    expect(counts).toEqual({ total: 5, done: 2, running: 1, queued: 1, failed: 1, stopped: 0 })
    expect(describeCounts(counts)).toBe('2 done · 1 running · 1 failed · 1 queued')
    expect(describeCounts(countStates(['done', 'done', 'done']))).toBe('all 3 done')
    expect(describeCounts(countStates(['running']))).toBe('running')
    expect(describeCounts(countStates(['done']))).toBe('done')
  })

  it('maps every job state', () => {
    expect(jobGlyph('succeeded', false)).toEqual({ shape: 'done', failed: false })
    expect(jobGlyph('running', false)).toEqual({ shape: 'running', failed: false })
    expect(jobGlyph('stopped', true)).toEqual({ shape: 'failed', failed: true })
    expect(jobGlyph('stopped', false)).toEqual({ shape: 'stopped', failed: false })
    expect(() => jobGlyph('paused', false)).toThrow()
  })
})

describe('workflowProgress', () => {
  const run = (shape: RunState, counts: Partial<Counts> | null) => ({
    glyph: { shape, failed: false },
    counts: counts && { total: 0, done: 0, running: 0, queued: 0, failed: 0, stopped: 0, ...counts },
  })

  it('averages its runs, counting a settled run whole and one not expanded as nothing', () => {
    expect(workflowProgress([
      run('done', { total: 4, done: 4 }),
      run('running', { total: 4, done: 1, running: 3 }),
      run('waiting', { total: 0 }),
      run('done', null),
    ], true)).toBeCloseTo((1 + 0.25 + 0) / 3)
    // A run that settled with failures has finished.
    expect(workflowProgress([run('failed', { total: 2, failed: 2 })], false)).toBe(1)
  })

  it('is whole with no runs unless the workflow is live', () => {
    expect(workflowProgress([], false)).toBe(1)
    expect(workflowProgress([], true)).toBe(0)
  })
})
