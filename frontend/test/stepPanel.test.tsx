// @vitest-environment jsdom
import {act, type ReactNode} from 'react'
import {createRoot, type Root} from 'react-dom/client'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {StepControls, StepPanel} from '../src/components/StepPanel'
import {LevelStateContext, type PanelView} from '../src/lib/levelState'
import {buildLevel} from '../src/model/level'
import {CID} from 'multiformats/cid'
import {identity} from 'multiformats/hashes/identity'
import {dispatchRegistry} from 'fxtr-view-shell-util'
import {TableShellProvider} from 'fxtr-view-shell-util/testing'
import {EntityPanel, renderer as registered, unwrap, useEntity, type EntityData, type EntityID} from 'fxtr-view'
import type {StepDoc} from '../src/types'
import type {NodeView} from '../src/panes/resolve'
import {base64Cbor, inProgress, levelState, node, overviewOf, recordingOpener, spawn} from './fixtures'
import {OpenEntityContext} from '../src/lib/openEntity'
import {getDragPayload} from '../src/panes/drag'
import {formatRel} from '../src/panes/address'
import {Store} from '../src/lib/store'

/** The renderers the registry offers each test: the output type's, and the step's. */
const renderers = vi.hoisted(() => ({entity: [] as Mocked[], invocation: [] as Mocked[]}))

/** A renderer as the registry lists it, for the view dropdown's rows: its id, its component, and
 *  the type it is registered for. */
interface Mocked { id: string; Component: () => null; subjectType: string }
const renderer = (id: string): Mocked => ({id, Component: () => null, subjectType: 'test.Roll'})

vi.mock('fxtr-view-shell-util', async importOriginal => {
  const {InvocationPanel} = await import('fxtr-view')
  return {
    ...await importOriginal<typeof import('fxtr-view-shell-util')>(),
    useRenderers: (slot: unknown) => slot === InvocationPanel ? renderers.invocation : renderers.entity,
    chosenRenderer: () => null, RenderInvocation: () => null,
  }
})

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  renderers.entity = []
  renderers.invocation = []
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  host = document.createElement('div')
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  vi.unstubAllGlobals()
})

/** A run of two finished invocations, each returning one string, shown in a panel. */
async function renderPanel() {
  const spawns = [spawn('roll', ['a'], 'completed', 'cid-a'), spawn('roll', ['b'], 'completed', 'cid-b')]
  const table = buildLevel('job', overviewOf(inProgress({spawns}))).steps.find(s => s.id === 'roll')!
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    const {query, variables} = JSON.parse(init.body)
    let data: unknown
    if (query.includes('runPage')) data = {runPage: {items: variables.unfinished ? [] : spawns, total: variables.unfinished ? 0 : 2, next: null}}
    else if (query.includes('outputPage')) {
      const rows = ['a', 'b'].map((seed) => ({key: [seed], value: base64Cbor(`rolled ${seed}`)}))
      data = {outputPage: {type: table.type, rows: rows.slice(0, variables.first), offset: 0, total: 2, hasMore: variables.first < 2}}
    } else throw new Error(`unexpected query: ${query}`)
    return {ok: true, json: async () => ({data})}
  }))
  const state = levelState()
  await act(async () => root.render(<LevelStateContext.Provider value={state}>
    <OpenEntityContext.Provider value={recordingOpener().opener}>
      <StepPanel jobId="job" step={table} />
    </OpenEntityContext.Provider>
  </LevelStateContext.Provider>))
  return table
}

/** The head controls of a panel showing the node through `view`, over the same run as `renderPanel`. */
async function renderControls(view: NodeView) {
  const table = await renderPanel()
  await act(async () => root.render(<LevelStateContext.Provider value={levelState()}>
    <StepControls step={table} view={view} onView={() => {}} />
  </LevelStateContext.Provider>))
}

const viewDropdown = () => host.querySelector('button[aria-label="View"]')

it('offers no renderer of its function in a node panel\'s view dropdown, which chooses among entity renderers', async () => {
  await renderControls({kind: 'table'})
  expect(viewDropdown()).toBeNull()
  renderers.invocation = [renderer('test.Rolls'), renderer('test.Tally')]
  await renderControls({kind: 'table'})
  expect(viewDropdown()).toBeNull()
})

it('shows no view dropdown on a panel showing a view of its function', async () => {
  renderers.invocation = [renderer('test.Rolls'), renderer('test.Tally')]
  await renderControls({kind: 'renderer', id: 'test.Rolls'})
  expect(viewDropdown()).toBeNull()
})

it('shows no view dropdown on a panel showing one invocation', async () => {
  renderers.invocation = [renderer('test.Rolls')]
  await renderControls({kind: 'invocation'})
  expect(viewDropdown()).toBeNull()
})

it('shows several rows of output as a table', async () => {
  await renderPanel()
  expect(host.querySelector('table')).not.toBeNull()
  expect(host.textContent).toContain('rolled b')
  // No head line naming the table and its type: the pane's title names the node.
  expect(host.textContent).not.toContain('[seed: str]')
})

it('drags a row of its table as the link clicking it follows', async () => {
  await renderPanel()
  const row = Array.from(host.querySelectorAll<HTMLTableRowElement>('tbody tr')).find((tr) => tr.textContent!.includes('rolled b'))!
  expect(row.draggable).toBe(true)
  const data = new Map<string, string>()
  const dataTransfer = {
    types: [] as string[], effectAllowed: 'none',
    setData(type: string, value: string) { data.set(type, value); this.types.push(type) },
    getData: (type: string) => data.get(type) ?? '',
  }
  const event = new Event('dragstart', {bubbles: true})
  Object.defineProperty(event, 'dataTransfer', {value: dataTransfer})
  await act(async () => row.dispatchEvent(event))
  const payload = getDragPayload(dataTransfer as unknown as DataTransfer)
  expect(payload?.kind).toBe('link')
  if (payload?.kind !== 'link') return
  expect(formatRel(payload.target.rel, payload.target.params)).toBe('table(seed=b)')
})

it('shows an output renderer over several rows under the table, for the row clicked', async () => {
  renderers.entity = [renderer('card')]
  const Card = ({id}: {id: EntityID}) => <p className="card">{`card n=${unwrap(useEntity(id)).n}`}</p>
  const {table, wrap} = noteRun(dispatchRegistry([registered(EntityPanel, 'org.example.Note', 'card', Card)]))
  // The output type's own renderer is the panel's default view.
  await act(async () => root.render(wrap(<LevelStateContext.Provider value={levelState()}>
    <OpenEntityContext.Provider value={recordingOpener().opener}>
      <StepPanel jobId="job" step={table} />
    </OpenEntityContext.Provider>
  </LevelStateContext.Provider>)))
  expect(host.querySelector('table')).not.toBeNull()
  // The first row until one is clicked.
  expect(host.querySelector('.card')!.textContent).toBe('card n=0')
  const [first, second] = host.querySelectorAll('tbody tr[data-row]')
  await act(async () => second!.dispatchEvent(new MouseEvent('click', {bubbles: true})))
  expect(host.querySelector('.card')!.textContent).toBe('card n=1')
  // The row shown is lit.
  expect(first!.hasAttribute('data-selected')).toBe(false)
  expect(second!.hasAttribute('data-selected')).toBe(true)
})

/** A run of `count` finished invocations (seeds `a`, `b`, …), each returning an
 *  `org.example.Note` entity (`n` 0, 1, …), and a wrapper providing the entities and `registry`. */
function noteRun(registry = dispatchRegistry([]), count = 2) {
  const seeds = Array.from({length: count}, (_, index) => String.fromCharCode(97 + index))
  const cids = seeds.map((seed) => CID.create(1, 0x71, identity.digest(Uint8Array.from([...seed].map((c) => c.charCodeAt(0))))))
  const entities = new Map<string, EntityData>(cids.map((cid, index) => [cid.toString(), {$type: 'org.example.Note', n: index}]))
  const invocation = inProgress({spawns: seeds.map((seed) => spawn('roll', [seed], 'completed', `cid-${seed}`))})
  invocation.nodes = invocation.nodes.map((n) => n.identity !== 'roll' ? n : node('roll', 'run_step', {
    ...n, type: {dims: [['seed', 'str']], schema: {id: 'org.example.Note'}},
  }))
  const table = buildLevel('job', overviewOf(invocation)).steps.find(s => s.id === 'roll')!
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    const {query, variables} = JSON.parse(init.body)
    let data: unknown
    if (query.includes('runPage')) data = {runPage: {items: variables.unfinished ? [] : invocation.spawns, total: variables.unfinished ? 0 : count, next: null}}
    else if (query.includes('outputPage')) {
      const rows = cids.map((cid, index) => ({key: [seeds[index]], value: base64Cbor(cid)}))
      const offset = variables.last ? Math.max(0, count - variables.first) : variables.offset
      const end = offset + variables.first
      data = {outputPage: {type: table.type, rows: rows.slice(offset, end), offset, total: count, hasMore: end < count}}
    } else if (query.includes('entityMetas')) data = {entityMetas: variables.cids.map((cid: string) => ({cid, typeTag: 'org.example.Note'}))}
    else throw new Error(`unexpected query: ${query}`)
    return {ok: true, json: async () => ({data})}
  }))
  const wrap = (children: ReactNode) => <TableShellProvider registry={registry} entities={entities}>{children}</TableShellProvider>
  return {cids, table, wrap}
}
