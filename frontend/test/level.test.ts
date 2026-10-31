import { describe, expect, it } from 'vitest'
import { buildLevel } from '../src/model/level'
import { inProgress, overviewOf } from './fixtures'

describe('a level in progress', () => {
  const invocation = overviewOf()
  const level = buildLevel('job', invocation)
  const card = (id: string) => level.steps.find((step) => step.id === id)!

  it('contains query references without eager row or invocation collections', () => {
    expect(card('in').output).toEqual({cid: 'cid-in'})
    expect(card('roll').browse).toEqual({jobId: 'job', pathname: 'root', nodeId: 'roll'})
    for (const step of level.steps) {
      expect(step).not.toHaveProperty('rows')
      expect(step).not.toHaveProperty('invocations')
      expect(step).not.toHaveProperty('replicas')
    }
  })

  it('gives each card its progress', () => {
    expect(card('in').progress).toEqual({ glyph: { shape: 'done', failed: false }, counts: null, activity: null, failures: [] })
    expect(card('roll').progress.glyph).toEqual({ shape: 'running', failed: false })
    expect(card('roll').progress.counts).toMatchObject({ total: 2, done: 1, running: 1 })
    expect(card('grade').progress.glyph.shape).toBe('waiting')
  })

  it('uses each run revision even when target changes leave counts unchanged', () => {
    const revised = structuredClone(invocation)
    revised.version = 'another-level-revision'
    const unchanged = buildLevel('job', revised)
    expect(unchanged.steps.map(step => step.version)).toEqual(level.steps.map(step => step.version))
    revised.nodes.find(node => node.identity === 'roll')!.version = 'targets-swapped'
    const changed = buildLevel('job', revised)
    expect(changed.steps.find(step => step.id === 'roll')!.version).not.toBe(card('roll').version)
    expect(changed.steps.find(step => step.id === 'grade')!.version).toBe(card('grade').version)
  })

  it('is live, not returned, and awaiting what the body observed', () => {
    expect(level.live).toBe(true)
    expect(level.result).toBeNull()
    expect(level.resultCid).toBeNull()
    expect(level.awaiting).toEqual({ id: 'roll', expr: [{ ref: 'roll' }] })
    expect(level.glyph).toEqual({ shape: 'running', failed: false })
  })

  it('reddens the level when a spawn failed, and stops an unexpanded run once nothing is live', () => {
    const failed = inProgress({
      state: 'interrupted',
      spawns: [
        { ...inProgress().spawns[0] },
        { ...inProgress().spawns[1], state: 'failed' },
      ],
      attempts: [],
    })
    const stopped = buildLevel('job', overviewOf(failed))
    expect(stopped.glyph).toEqual({ shape: 'stopped', failed: true })
    expect(stopped.live).toBe(false)
    expect(stopped.steps.find((step) => step.id === 'grade')!.progress.glyph.shape).toBe('stopped')
    expect(stopped.steps.find((step) => step.id === 'roll')!.progress.glyph).toEqual({ shape: 'failed', failed: true })
  })
})
