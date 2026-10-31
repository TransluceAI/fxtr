import { describe, expect, it } from 'vitest'
import { buildLevel } from '../src/model/level'
import { invocationSliceFor, isMapped, rowSliceFor } from '../src/lib/invocations'
import { levelDims } from '../src/lib/keyLists'
import {
  hasOutput,
  isEntityOutput,
  outputSourceFor,
  outputValues,
} from '../src/lib/output'
import { makeRow } from '../src/model/level'
import { inProgress, node, overviewOf } from './fixtures'

describe('the level slice, read by one step', () => {
  const level = buildLevel('job', overviewOf(inProgress()))
  const card = (id: string) => level.steps.find((step) => step.id === id)!

  it('calls a step mapped when it has invocation dims', () => {
    expect(isMapped(card('roll'))).toBe(true)
    expect(isMapped(card('in'))).toBe(false)
    expect(isMapped(card('grade'))).toBe(false)
  })

  it('keeps only the dims the step has', () => {
    expect(invocationSliceFor(card('roll'), { seed: 'a', model: 'm' })).toEqual({ seed: 'a' })
    expect(invocationSliceFor(card('grade'), { seed: 'a' })).toEqual({})
  })

  it('slices a run output source or a registered array', () => {
    expect(outputSourceFor(card('roll'), { seed: 'b' })).toEqual({ ...card('roll').output, selection: { seed: 'b' } })
    expect(outputSourceFor(card('in'), { seed: 'b' })).toEqual({ ...card('in').output, selection: { seed: 'b' } })
  })

  it('has output once a run has invocations, or a registered array is stored', () => {
    expect(hasOutput(card('roll'))).toBe(true)
    expect(hasOutput(card('in'))).toBe(true)
    expect(hasOutput(card('grade'))).toBe(false)
  })

  it('reads the rows as values, a string output not being entities', () => {
    const roll = card('roll')
    expect(isEntityOutput(roll)).toBe(false)
    expect(outputValues(roll, [makeRow(roll.id, roll.type, ['a'], 'rolled a')])).toEqual(['rolled a'])
  })
})

describe('a node with a dim produced inside its invocations', () => {
  const sampled = node('sample', 'run_step', {
    params: { step_name: 'sample', map_over: ['seed'], replicas: {} },
    inputs: [{ label: 'seed', identity: 'in' }],
    type: { dims: [['seed', 'str'], ['sample', 'int']], schema: 'str' },
  })
  const invocation = inProgress()
  const level = buildLevel('job', overviewOf({ ...invocation, nodes: [...invocation.nodes, sampled] }))
  const card = (id: string) => level.steps.find((step) => step.id === id)!

  it('filters its rows on every dim it has, and its invocations on the mapped ones only', () => {
    expect(invocationSliceFor(card('sample'), { seed: 'a', sample: 1, model: 'm' })).toEqual({ seed: 'a' })
    expect(rowSliceFor(card('sample'), { seed: 'a', sample: 1, model: 'm' })).toEqual({ seed: 'a', sample: 1 })
  })

  it('lists every dim of the level, read from a run it keys, else from output that has it', () => {
    expect(levelDims(level.steps).map(({ dim, source }) => [dim, source?.kind, source?.step.id])).toEqual([
      ['seed', 'invocations', 'roll'],
      ['sample', 'output', 'sample'],
    ])
    expect(levelDims(level.steps.filter((step) => step.id === 'in'))[0].source).toMatchObject({ kind: 'output', step: { id: 'in' } })
    // A dim no node keys has no source until some node with it has output.
    expect(levelDims([{ ...card('sample'), output: null }]).map(({ source }) => source?.kind ?? null)).toEqual(['invocations', null])
  })
})
