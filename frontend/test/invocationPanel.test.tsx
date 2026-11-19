// @vitest-environment jsdom
import * as dagCbor from '@ipld/dag-cbor'
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {EntityID, ViewShellProvider, type InvocationRecord, type ViewShell} from 'fxtr-view'
import {RegistryProvider, dispatchRegistry} from 'fxtr-view-shell-util'
import {defaultRenderers} from 'fxtr-view-default-renderers'
import {RESULT_NOT_TAKEN} from '../src/components/InvocationInfo'
import {StepPanel} from '../src/components/StepPanel'
import {OpenEntityContext} from '../src/lib/openEntity'
import {LevelStateContext} from '../src/lib/levelState'
import {buildLevel} from '../src/model/level'
import type {StepDoc} from '../src/types'
import {base64Cbor, inProgress, levelState, overviewOf, recordingOpener, spawn as gqlSpawn} from './fixtures'

vi.mock('fxtr-view-shell-util', async importOriginal => ({
  ...await importOriginal<typeof import('fxtr-view-shell-util')>(),
  useRenderers: () => [], chosenRenderer: () => null, RenderInvocation: () => null,
}))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  host = document.createElement('div')
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  vi.unstubAllGlobals()
})

/** `roll` mapped over `seed`, producing a `sample` dim inside each invocation, and its input. */
function level(jobId: string, state: string) {
  const invocation = inProgress({spawns: [gqlSpawn('roll', ['b'], state, state === 'completed' ? 'result' : null)]})
  invocation.nodes = invocation.nodes.map(n => n.identity !== 'roll' ? n
    : {...n, type: {dims: [['seed', 'str'], ['sample', 'int']], schema: {struct: {text: 'str', score: 'int'}}}})
  const steps = buildLevel(jobId, overviewOf(invocation)).steps
  return {roll: steps.find(s => s.id === 'roll')!, input: steps.find(s => s.id === 'in')!}
}

function record(jobId: string, state: string): InvocationRecord {
  return {
    ref: {jobId, kind: 'step', cacheKey: 'k/b'}, name: 'roll', state,
    error: state === 'failed' ? {typeName: 'ValueError', message: 'important failure'} : null,
    inputs: {seed: new EntityID('cid-seed') as never},
    output: state === 'completed' ? new EntityID('result') as never : null,
    children: [],
  }
}

async function render({jobId, state, slice, setSlice = () => {}, preview, taken = true}: {
  jobId: string; state: string; slice: Record<string, string | number>; setSlice?: (next: unknown) => void
  /** The step's latest preview, as its decoded wire form; none if omitted. */
  preview?: unknown
  /** Whether the job has taken a completed step's result. */
  taken?: boolean
}) {
  const {roll, input} = level(jobId, state)
  let outputReads = 0
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).startsWith('/api/previews/') && preview !== undefined) {
      const bytes = dagCbor.encode(preview)
      return {ok: true, status: 200, headers: new Headers({'X-Fxtr-Preview-Seq': '4'}),
        arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)}
    }
    if (!String(url).includes('graphql')) return {ok: false, status: 404, statusText: 'not here', json: async () => ({})}
    const {query, variables} = JSON.parse(String(init!.body))
    let data: unknown
    if (query.includes('runPage')) {
      data = {runPage: {items: variables.unfinished ? [] : [gqlSpawn('roll', ['b'], state, null)], total: variables.unfinished ? 0 : 1, next: null}}
    } else if (query.includes('outputPage') && variables.source.cid === 'cid-seed') {
      data = {outputPage: {type: {dims: [], schema: 'str'}, rows: [{key: [], value: base64Cbor('b')}], offset: 0, total: 1, hasMore: false}}
    } else if (query.includes('outputPage')) {
      outputReads += 1
      const rows = [{key: ['b', 0], value: base64Cbor({text: 'first sample', score: 3})},
        {key: ['b', 1], value: base64Cbor({text: 'second sample', score: 5})}]
      const picked = JSON.stringify(variables).includes('"sample"') ? rows.slice(0, 1) : rows
      data = {outputPage: {type: roll.type, rows: picked, offset: 0, total: picked.length, hasMore: false}}
    } else if (query.includes('step(')) {
      data = {step: {stepName: 'roll', requestCid: 'request', state, logs: [], provenance: null,
        resultCid: state === 'completed' ? 'result' : null, resultTaken: state === 'completed' && taken,
        preview: preview === undefined ? null : {attemptId: 'attempt-1', seq: 4, at: '2026-09-24T00:00:03Z'},
        attempt: {state, startedAt: '2026-09-24T00:00:00Z', finishedAt: state === 'running' ? null : '2026-09-24T00:00:07Z',
          error: null, provenance: {jobId, commit: 'c0ffee1234567', ephemeral: false}}}}
    } else throw new Error(`unexpected query: ${query}`)
    return {ok: true, json: async () => ({data})}
  }))
  const shell: ViewShell = {
    useEntity: () => ({status: 'loading', promise: new Promise(() => {})}) as never,
    useInvocation: () => ({status: 'loaded', value: record(jobId, state)}),
    EntityLinkImpl: () => null,
  }
  await act(async () => root.render(
    <ViewShellProvider shell={shell}><RegistryProvider registry={dispatchRegistry(defaultRenderers)}>
      <LevelStateContext.Provider value={levelState({slice, setSlice: setSlice as never})}>
        <OpenEntityContext.Provider value={recordingOpener().opener}>
          <StepPanel jobId={jobId} step={roll} inputs={{seed: input as StepDoc}} view={{kind: 'invocation'}} />
        </OpenEntityContext.Provider>
      </LevelStateContext.Provider>
    </RegistryProvider></ViewShellProvider>))
  await act(async () => {})  // the preview's fetch, started by an effect
  return {outputReads: () => outputReads}
}

it('shows a completed invocation\'s status and where it came from, its inputs folded, then its output inline', async () => {
  await render({jobId: 'job-done', state: 'completed', slice: {seed: 'b'}})
  expect([...host.querySelectorAll('h3')].map(h => h.firstChild!.textContent)).toEqual(['Inputs', 'Output'])
  expect(host.textContent!.indexOf('c0ffee1234')).toBeLessThan(host.textContent!.indexOf('Inputs'))
  expect(host.textContent).toContain('completed in 7s')
  expect(host.textContent).not.toContain(RESULT_NOT_TAKEN)
  expect(host.textContent).toContain('Output')
  expect(host.textContent).toContain('Array[struct]')
  expect(host.textContent).toContain('second sample')
  expect(host.textContent).toContain('c0ffee1234')
  expect(host.textContent).toContain('k/b')
  const inputsSection = [...host.querySelectorAll('section')].find(s => s.querySelector('h3')?.textContent === 'Inputs')!
  const inputHead = inputsSection.querySelector<HTMLButtonElement>('button[aria-expanded]')!
  expect(inputHead.textContent).toContain('seed')
  expect(inputHead.textContent).toContain('str')
  expect(inputHead.textContent).toContain('1 row')
  expect(inputHead.textContent).not.toContain('cid-seed')
  expect(inputHead.getAttribute('aria-expanded')).toBe('false')
})

it('shows a failed invocation\'s error in place of its output, without reading it', async () => {
  const {outputReads} = await render({jobId: 'job-failed', state: 'failed', slice: {seed: 'b'}})
  expect(host.textContent).toContain('failed after 7s')
  expect(host.textContent).toContain('ValueError: important failure')
  expect(outputReads()).toBe(0)
})

it('sketches a running invocation\'s output from its schema', async () => {
  await render({jobId: 'job-running', state: 'running', slice: {seed: 'b'}})
  expect(host.textContent).toContain('running for')
  expect(host.textContent).toContain('text')
  expect(host.textContent).toContain('score')
})

it('says which of its rows a produced dim\'s binding leaves, and Show all leaves it out', async () => {
  const setSlice = vi.fn()
  await render({jobId: 'job-narrowed', state: 'completed', slice: {seed: 'b', sample: 0}, setSlice})
  expect(host.textContent).toContain('1 of 2 rows')
  expect(host.textContent).toContain('sample=0')
  expect(host.textContent).toContain('first sample')
  const showAll = [...host.querySelectorAll('button')].find(b => b.textContent === 'Show all')!
  await act(async () => showAll.click())
  const update = setSlice.mock.calls[0]![0] as (current: Record<string, unknown>) => Record<string, unknown>
  expect(update({seed: 'b', sample: 0, model: 'm'})).toEqual({seed: 'b', model: 'm'})
})

const PARTIAL_DRAFT = {$type: 'org.transluce.fxtr.Partial', of: {$type: 'test.Draft', reply: 'streamed so far', words: null}}
// Shown through the slot's wildcard, the raw-entity data tree, which marks it partial.

it('shows a running step\'s live preview in place of its sketch', async () => {
  await render({jobId: 'job-previewing', state: 'running', slice: {seed: 'b'}, preview: PARTIAL_DRAFT})
  expect(host.textContent).toContain('live preview')
  expect(host.textContent).toContain('partial')
  expect(host.textContent).toContain('streamed so far')
  expect(host.textContent).not.toContain('score')  // the schema's sketch is gone
})

it('shows a failed step\'s last preview under its error', async () => {
  await render({jobId: 'job-failed-preview', state: 'failed', slice: {seed: 'b'}, preview: PARTIAL_DRAFT})
  expect(host.textContent).toContain('ValueError: important failure')
  expect(host.textContent).toContain('last preview')
  expect(host.textContent).toContain('streamed so far')
})

// A step that finished after the workflow invocation waiting on it ended (a sibling failed, or the
// job was cancelled): it is completed, and its result is on offer until the job is resumed.
it('shows the output of a step whose result the job has not taken, and says so', async () => {
  await render({jobId: 'job-not-taken', state: 'completed', slice: {seed: 'b'}, taken: false})
  expect(host.textContent).toContain('completed in 7s')
  expect(host.textContent).toContain(RESULT_NOT_TAKEN)
  expect(host.textContent).toContain('second sample')
})

it('shows a stopped step\'s last preview in place of its output', async () => {
  await render({jobId: 'job-stopped-preview', state: 'stopped', slice: {seed: 'b'}, preview: PARTIAL_DRAFT})
  expect(host.textContent).toContain('stopped after 7s')
  expect(host.textContent).toContain('last preview')
  expect(host.textContent).toContain('streamed so far')
})

it('says a stopped step with no preview has no output', async () => {
  const {outputReads} = await render({jobId: 'job-stopped', state: 'stopped', slice: {seed: 'b'}})
  expect(host.textContent).toContain('no output: it stopped before it finished')
  expect(outputReads()).toBe(0)
})
