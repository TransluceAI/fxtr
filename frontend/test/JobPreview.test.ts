import { describe, expect, it } from 'vitest'
import type { StepDoc } from '../src/types'
import { compactLayout, stepSummary } from '../src/components/JobPreview'
import { card } from './flowchartGraphs'

const step = (kind: StepDoc['kind'], counts: Partial<NonNullable<StepDoc['progress']['counts']>> | null) =>
  ({
    name: 'judge',
    kind,
    progress: { counts: counts && { total: 0, done: 0, running: 0, queued: 0, failed: 0, stopped: 0, ...counts } },
  }) as unknown as StepDoc

describe('stepSummary', () => {
  it('says how many results the job ran and reused, and how many failed or are still going', () => {
    expect(stepSummary(step('step', { failed: 2, running: 1 }), { ran: 5, reused: 3 })).toBe('Ran 5, reused 3 · 2 failed · 1 running')
    expect(stepSummary(step('step', {}), { ran: 0, reused: 7 })).toBe('Reused all 7 from earlier jobs')
  })

  it("counts a workflow's results as its steps'", () => {
    expect(stepSummary(step('workflow', {}), { ran: 4, reused: 0 })).toBe('Its steps ran 4')
  })

  it('says when a step did not run', () => {
    expect(stepSummary(step('step', {}), { ran: 0, reused: 0 })).toBe('Did not run')
  })
})

describe('compactLayout', () => {
  it('puts every step below the steps it reads, a step reading nothing above its one consumer', () => {
    const ran = (id: string, deps: string[] = []) => {
      const step = card(id, deps, { kind: 'step' })
      return { ...step, progress: { ...step.progress, counts: { total: 1, done: 1, running: 0, queued: 0, failed: 0, stopped: 0 } } }
    }
    const layout = compactLayout([card('config'), ran('generate'), ran('judge', ['generate', 'config']), ran('report', ['judge'])])
    const top = (id: string) => layout.nodes.find((node) => node.step.id === id)!.y
    expect(layout.nodes.map((node) => node.step.id).sort()).toEqual(['generate', 'judge', 'report'])
    expect(top('generate')).toBeLessThan(top('judge'))
    expect(top('judge')).toBeLessThan(top('report'))
  })

  it('draws one workflow the same for every job, whatever order or ids its steps have', () => {
    // A job's steps have ids of their own (content hashes: given other inputs, they differ and sort
    // in no relation to the names), and come in the order the backend wrote them, which differs
    // where steps were written together.
    const names = ['responses', 'judgments', 'helpfulness', 'refusal_rates', 'mean_helpfulness', 'report']
    const job = (idOf: (name: string) => string) => {
      const ran = (name: string, deps: string[] = []) => {
        const step = card(idOf(name), deps.map(idOf), { name, kind: 'step' })
        return { ...step, progress: { ...step.progress, counts: { total: 1, done: 1, running: 0, queued: 0, failed: 0, stopped: 0 } } }
      }
      return [
        ran('responses'),
        ran('judgments', ['responses']),
        ran('helpfulness', ['responses']),
        ran('refusal_rates', ['judgments']),
        ran('mean_helpfulness', ['helpfulness']),
        ran('report', ['refusal_rates', 'mean_helpfulness']),
      ]
    }
    const a = job((name) => `id-${names.indexOf(name)}`)
    const b = job((name) => `id-${names.length - names.indexOf(name)}`)
    const swapped = [b[0], b[2], b[1], b[4], b[3], b[5]]
    const placed = (steps: StepDoc[], layout: ReturnType<typeof compactLayout>) =>
      Object.fromEntries(layout.nodes.map((node) => [steps.find((step) => step.id === node.step.id)!.name, [node.x, node.y]]))
    expect(placed(swapped, compactLayout(swapped))).toEqual(placed(a, compactLayout(a)))
  })
})
