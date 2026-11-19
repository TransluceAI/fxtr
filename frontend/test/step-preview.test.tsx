// @vitest-environment jsdom
import * as dagCbor from '@ipld/dag-cbor'
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {expect, it, vi} from 'vitest'
import {isPartial, PartialEntityInline, renderer, type DataMap, type PartialEntityInlineProps} from 'fxtr-view'
import {PARTIAL_TYPE_ID, unwrapPreview, useStepPreview, type Preview} from '../src/api/preview'
import {RegistryProvider, dispatchRegistry} from 'fxtr-view-shell-util'
import {defaultRenderers} from 'fxtr-view-default-renderers'
import {StepPanel} from '../src/components/StepPanel'
import {StepPreviewLine} from '../src/components/StepPreview'
import {OpenEntityContext} from '../src/lib/openEntity'
import {LevelStateContext} from '../src/lib/levelState'
import {buildLevel} from '../src/model/level'
import {inProgress, levelState, overviewOf, recordingOpener} from './fixtures'

vi.mock('fxtr-view-shell-util', async importOriginal => ({
  ...await importOriginal<typeof import('fxtr-view-shell-util')>(),
  useRenderers: () => [], chosenRenderer: () => null, RenderInvocation: () => null,
}))

const DRAFT = {$type: 'test.Draft', text: 'streamed so far', words: null}

it('takes every partial wrapper off a preview, at any depth, and leaves a finished one as it is', () => {
  const nested = {$type: PARTIAL_TYPE_ID, of: {...DRAFT, parts: [{$type: PARTIAL_TYPE_ID, of: {$type: 'test.Part', text: null}}]}}
  expect(unwrapPreview(nested, 3)).toEqual({value: {...DRAFT, parts: [{$type: 'test.Part', text: null}]}, partial: true, seq: 3})
  expect(unwrapPreview(DRAFT, 4)).toEqual({value: DRAFT, partial: false, seq: 4})
  expect(() => unwrapPreview([1, 2], 5)).toThrow('not tagged')
})

it('marks each part that came out of a partial wrapper, and only those', () => {
  const finishedPart = {$type: 'test.Part', text: 'done'}
  const {value} = unwrapPreview({$type: PARTIAL_TYPE_ID, of: {...DRAFT, parts: [finishedPart, {$type: PARTIAL_TYPE_ID, of: {$type: 'test.Part', text: null}}]}}, 1)
  const [first, second] = value.parts as DataMap[]
  expect([isPartial(value), isPartial(first), isPartial(second)]).toEqual([true, false, true])
  expect(isPartial(unwrapPreview(DRAFT, 2).value)).toBe(false)
})

it.each(['running', 'failed'])('shows a %s step\'s latest preview in its step panel', async state => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  const host = document.createElement('div')
  const root = createRoot(host)
  const jobId = `job-preview-${state}`
  const spawn = {...inProgress().spawns[1], state, resultCid: null}
  const table = buildLevel(jobId, overviewOf(inProgress({spawns: [spawn]}))).steps.find(s => s.id === 'roll')!
  const previewUrls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: {body?: string}) => {
    if (url.startsWith('/api/previews/')) {
      previewUrls.push(url)
      const bytes = dagCbor.encode({$type: PARTIAL_TYPE_ID, of: DRAFT})
      return {ok: true, status: 200, headers: new Headers({'X-Fxtr-Preview-Seq': '2'}),
        arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)}
    }
    const {query} = JSON.parse(init!.body!)
    let data: unknown
    if (query.includes('runPage')) {
      data = {runPage: {items: [spawn], total: 1, next: null}}
    } else if (query.includes('step(')) {
      data = {step: {stepName: 'roll', requestCid: 'request', state, resultCid: null, resultTaken: false, logs: [],
        preview: {attemptId: 'attempt-1', seq: 2, at: '2026-09-24T00:00:00Z'},
        attempt: {state, startedAt: '2026-09-24T00:00:00Z', finishedAt: state === 'failed' ? '2026-09-24T00:00:01Z' : null,
          error: state === 'failed' ? {typeName: 'Error', message: 'boom', traceback: ''} : null}}}
    } else throw new Error(`unexpected query: ${query}`)
    return {ok: true, json: async () => ({data})}
  }))
  try {
    await act(async () => root.render(<RegistryProvider registry={dispatchRegistry(defaultRenderers)}>
      <LevelStateContext.Provider value={levelState({slice: {seed: 'b'}})}>
      <OpenEntityContext.Provider value={recordingOpener().opener}>
        <StepPanel jobId={jobId} step={table} />
      </OpenEntityContext.Provider>
    </LevelStateContext.Provider></RegistryProvider>))
    await act(async () => {})
    expect(previewUrls).toEqual(['/api/previews/attempt-1?seq=2'])
    expect(host.textContent).toContain(state === 'running' ? 'live preview' : 'last preview')
    expect(host.textContent).toContain('partial')
    expect(host.textContent).toContain('streamed so far')
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})

/** Render a running step's preview line, its pin in `state`, over a backend serving `DRAFT`. */
async function renderLine(state: string, renderers: typeof defaultRenderers) {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.createElement('div')
  const root = createRoot(host)
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: {body?: string}) => {
    if (url.startsWith('/api/previews/')) {
      const bytes = dagCbor.encode({$type: PARTIAL_TYPE_ID, of: DRAFT})
      return {ok: true, status: 200, headers: new Headers({'X-Fxtr-Preview-Seq': '4'}),
        arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)}
    }
    if (!JSON.parse(init!.body!).query.includes('step(')) throw new Error(`unexpected request: ${url}`)
    return {ok: true, json: async () => ({data: {step: {stepName: 'roll', requestCid: 'request', state, resultCid: null, resultTaken: false, logs: [],
      preview: {attemptId: 'attempt-line', seq: 4, at: '2026-09-24T00:00:00Z'},
      attempt: {state, startedAt: '2026-09-24T00:00:00Z', finishedAt: null, error: null}}}})}
  }))
  await act(async () => root.render(<RegistryProvider registry={dispatchRegistry(renderers)}>
    <StepPreviewLine jobId={`job-line-${state}`} cacheKey="k/roll" fallback="no preview" />
  </RegistryProvider>))
  await act(async () => {})
  return {host, unmount: () => act(async () => root.unmount())}
}

it('shows a running step\'s preview in one line through its type\'s line renderer, saying whether the step still runs', async () => {
  const DraftLine = ({value, partial, live}: PartialEntityInlineProps) =>
    <span>draft line: {String(value.text)} ({partial ? 'partial' : 'finished'}, {live ? 'live' : 'stopped'})</span>
  for (const [state, expected] of [['running', 'live'], ['stopped', 'stopped']] as const) {
    const {host, unmount} = await renderLine(state, [...defaultRenderers, renderer(PartialEntityInline, 'test.Draft', 'line', DraftLine)])
    try {
      expect(host.textContent).toBe(`draft line: streamed so far (partial, ${expected})`)
    } finally {
      await unmount()
      vi.unstubAllGlobals()
    }
  }
})

it('shows a preview whose type has no line renderer as its type name, partial', async () => {
  const {host, unmount} = await renderLine('running', defaultRenderers)
  try {
    expect(host.textContent).toBe('Draft partial')
  } finally {
    await unmount()
    vi.unstubAllGlobals()
  }
})

it('lets a preview fetch slower than the poll land, then fetches only the latest seq', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.createElement('div')
  const root = createRoot(host)
  const requests: {url: string, signal: AbortSignal, respond: () => void}[] = []
  vi.stubGlobal('fetch', vi.fn((url: string, init: {signal: AbortSignal}) => new Promise((resolve, reject) => {
    const seq = new URL(url, 'http://viewer').searchParams.get('seq')!
    init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    const bytes = dagCbor.encode({$type: PARTIAL_TYPE_ID, of: {...DRAFT, text: `at ${seq}`}})
    requests.push({url, signal: init.signal, respond: () => resolve({ok: true, status: 200,
      headers: new Headers({'X-Fxtr-Preview-Seq': seq}),
      arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)})})
  })))
  let shown: Preview | null = null
  const Probe = ({seq}: {seq: number}) => {
    shown = useStepPreview({attemptId: 'attempt-slow', seq, at: ''})
    return null
  }
  try {
    await act(async () => root.render(<Probe seq={1} />))
    // The pin polls twice more while the first fetch is still loading.
    await act(async () => root.render(<Probe seq={2} />))
    await act(async () => root.render(<Probe seq={3} />))
    expect(requests.map(r => r.url)).toEqual(['/api/previews/attempt-slow?seq=1'])
    expect(requests[0].signal.aborted).toBe(false)
    await act(async () => requests[0].respond())
    expect(shown!.value.text).toBe('at 1')
    // It landed; the latest seq is fetched next, skipping the one in between.
    expect(requests.map(r => r.url)).toEqual(['/api/previews/attempt-slow?seq=1', '/api/previews/attempt-slow?seq=3'])
    await act(async () => requests[1].respond())
    expect(shown!.value.text).toBe('at 3')
    expect(requests).toHaveLength(2)
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})
