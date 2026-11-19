// @vitest-environment jsdom
/**
 * The contract of `components/StepPreview.tsx`: every view that shows a step invocation that has
 * not finished (running, failed, or stopped), in place of its output, shows the step's latest preview. A node's panel shows one
 * such invocation two ways, and both must:
 *
 * - its default view, sliced at one unfinished invocation (`PendingInvocation`);
 * - its `invocation` view (`DefaultInvocationPanel`), which is what clicking a running row opens.
 *
 * Both dispatch the preview like an entity: the previewed type's `PartialEntityPanel` renderer,
 * else the raw data tree. A view added for unfinished steps belongs in `VIEWS` below.
 */
import * as dagCbor from '@ipld/dag-cbor'
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {EntityID, ViewShellProvider, type InvocationRecord, type ViewShell} from 'fxtr-view'
import {PARTIAL_TYPE_ID} from '../src/api/preview'
import {RegistryProvider, dispatchRegistry} from 'fxtr-view-shell-util'
import {PartialEntityPanel, renderer, type PartialEntityPanelProps} from 'fxtr-view'
import {defaultRenderers} from 'fxtr-view-default-renderers'
import {StepPanel} from '../src/components/StepPanel'
import {OpenEntityContext} from '../src/lib/openEntity'
import {LevelStateContext} from '../src/lib/levelState'
import {buildLevel} from '../src/model/level'
import {inProgress, levelState, overviewOf, recordingOpener, spawn as gqlSpawn} from './fixtures'

vi.mock('fxtr-view-shell-util', async importOriginal => ({
  ...await importOriginal<typeof import('fxtr-view-shell-util')>(),
  useRenderers: () => [], chosenRenderer: () => null, RenderInvocation: () => null,
}))

const PREVIEW = {$type: PARTIAL_TYPE_ID, of: {$type: 'test.Draft', reply: 'streamed so far', words: null}}

const VIEWS = [
  {name: 'the default view at one unfinished invocation', view: undefined},
  {name: 'the invocation view', view: {kind: 'invocation'} as const},
]

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

/** A project's renderer for the previewed type, which takes precedence over the raw data tree. */
function DraftView({value, partial, live}: PartialEntityPanelProps) {
  return <p>draft view: {String(value.reply)}{partial ? ' (partial)' : ''}{live ? ' (live)' : ' (stopped)'}</p>
}
const DRAFT_RENDERER = renderer(PartialEntityPanel, 'test.Draft', 'draft', DraftView)

async function render(jobId: string, state: string, view: (typeof VIEWS)[number]['view'], projectRenderers: readonly unknown[] = []) {
  const spawn = gqlSpawn('roll', ['b'], state, null)
  const roll = buildLevel(jobId, overviewOf(inProgress({spawns: [spawn]}))).steps.find(s => s.id === 'roll')!
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (String(url).startsWith('/api/previews/')) {
      const bytes = dagCbor.encode(PREVIEW)
      return {ok: true, status: 200, headers: new Headers({'X-Fxtr-Preview-Seq': '7'}),
        arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)}
    }
    if (!String(url).includes('graphql')) return {ok: false, status: 404, statusText: 'not here', json: async () => ({})}
    const {query} = JSON.parse(String(init!.body))
    let data: unknown
    if (query.includes('runPage')) {
      data = {runPage: {items: [spawn], total: 1, next: null}}
    } else if (query.includes('step(')) {
      data = {step: {stepName: 'roll', requestCid: 'request', state, resultCid: null, resultTaken: false, logs: [], provenance: null,
        preview: {attemptId: 'attempt-1', seq: 7, at: '2026-09-24T00:00:03Z'},
        attempt: {state, startedAt: '2026-09-24T00:00:00Z', finishedAt: state === 'running' ? null : '2026-09-24T00:00:07Z',
          error: state === 'failed' ? {typeName: 'ValueError', message: 'boom', traceback: ''} : null,
          provenance: {jobId, commit: 'c0ffee1234567', ephemeral: false}}}}
    } else throw new Error(`unexpected query: ${query}`)
    return {ok: true, json: async () => ({data})}
  }))
  const record: InvocationRecord = {
    ref: {jobId, kind: 'step', cacheKey: 'k/b'}, name: 'roll', state,
    error: state === 'failed' ? {typeName: 'ValueError', message: 'boom'} : null,
    inputs: {}, output: null, children: [],
  }
  const shell: ViewShell = {
    useEntity: () => ({status: 'loading', promise: new Promise(() => {})}) as never,
    useInvocation: () => ({status: 'loaded', value: record}),
    EntityLinkImpl: () => null,
  }
  await act(async () => root.render(
    <ViewShellProvider shell={shell}><RegistryProvider registry={dispatchRegistry([...defaultRenderers, ...projectRenderers] as never)}>
      <LevelStateContext.Provider value={levelState({slice: {seed: 'b'}})}>
        <OpenEntityContext.Provider value={recordingOpener().opener}>
          <StepPanel jobId={jobId} step={roll} view={view} />
        </OpenEntityContext.Provider>
      </LevelStateContext.Provider>
    </RegistryProvider></ViewShellProvider>))
  await act(async () => {})  // the preview's fetch, started by an effect
}

for (const {name, view} of VIEWS) {
  it.each([['running', 'live preview'], ['failed', 'last preview'], ['stopped', 'last preview']])(
    `${name} shows a %s step's preview`,
    async (state, label) => {
      await render(`job-${state}-${view?.kind ?? 'default'}`, state, view)
      expect(host.textContent).toContain(label)
      expect(host.textContent).toContain('partial')
      expect(host.textContent).toContain('streamed so far')
    },
  )
}

for (const {name, view} of VIEWS) {
  it.each([['running', '(live)'], ['failed', '(stopped)'], ['stopped', '(stopped)']])(
    `${name} shows a %s step's preview through its type's renderer, saying whether the step still runs`,
    async (state, live) => {
      await render(`job-renderer-${state}-${view?.kind ?? 'default'}`, state, view, [DRAFT_RENDERER])
      expect(host.textContent).toContain(`draft view: streamed so far (partial) ${live}`)
    },
  )
}
