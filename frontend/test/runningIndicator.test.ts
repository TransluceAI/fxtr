import { describe, expect, it } from 'vitest'
import { offscreenRunning } from '../src/components/RunningIndicator'
import type { CardGeometry } from '../src/components/Minimap'
import type { RunState } from '../src/lib/progress'
import type { StepDoc } from '../src/types'

/** Cards 100px tall, one under another from the top of the level. */
function trace(shapes: RunState[]) {
  const level = shapes.map((shape, index) => ({ id: `s${index}`, progress: { glyph: { shape, failed: false } } }) as unknown as StepDoc)
  const geometry = new Map<string, CardGeometry>(
    level.map((step, index) => [step.id, { top: index * 100, bottom: index * 100 + 100, center: index * 100 + 10 }]),
  )
  return { level, geometry }
}

describe('offscreenRunning', () => {
  it('names nothing while a running card overlaps the viewport, or before it is measured', () => {
    const { level, geometry } = trace(['done', 'running', 'running', 'done'])
    expect(offscreenRunning(level, geometry, { top: 150, height: 10 })).toBeNull()
    expect(offscreenRunning(level, geometry, { top: 0, height: 0 })).toBeNull()
  })

  it('names the running card above or below the viewport', () => {
    const { level, geometry } = trace(['running', 'done', 'done', 'done'])
    expect(offscreenRunning(level, geometry, { top: 200, height: 100 })).toEqual({ id: 's0', above: true })
    const below = trace(['done', 'done', 'done', 'running'])
    expect(offscreenRunning(below.level, below.geometry, { top: 0, height: 150 })).toEqual({ id: 's3', above: false })
  })

  it('names the running card nearest the viewport', () => {
    const { level, geometry } = trace(['running', 'done', 'done', 'done', 'running', 'done'])
    expect(offscreenRunning(level, geometry, { top: 300, height: 100 })).toEqual({ id: 's4', above: false })
    expect(offscreenRunning(level, geometry, { top: 100, height: 100 })).toEqual({ id: 's0', above: true })
  })

  it('names nothing when nothing is running', () => {
    const { level, geometry } = trace(['done', 'queued', 'failed'])
    expect(offscreenRunning(level, geometry, { top: 0, height: 50 })).toBeNull()
  })
})
