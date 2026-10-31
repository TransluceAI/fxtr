// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {afterEach, beforeEach, expect, it, vi} from 'vitest'
import {InvocationPanel, declareView, renderer} from 'fxtr-view'
import {dispatchRegistry} from 'fxtr-view-shell-util'
import {buildLevel} from '../src/model/level'
import {parseRel, type Params} from '../src/panes/address'
import {TRACE, allPanels, sequence} from '../src/panes/columns'
import {useColumns, type Centre, type ColumnsState} from '../src/panes/useColumns'
import {linkTarget, panelBase} from '../src/lib/linkBase'
import type {Catalog} from '../src/panes/resolve'
import type {Level, StepDoc} from '../src/types'
import {inProgress, overviewOf} from './fixtures'

const built = buildLevel('job', overviewOf(inProgress()))
const roll = built.steps.find((step) => step.name === 'roll')!
// A step whose output is entities, so a path into it goes on through a reference.
const judge: StepDoc = {...roll, id: 'judge', name: 'judge', type: {...roll.type, schema: {id: 'test.Judgment'}}}
// A step that produces a dim of its own inside each invocation.
const expand: StepDoc = {...roll, id: 'expand', name: 'expand', type: {...roll.type, dims: [...roll.type.dims, ['sample', 'int']] as typeof roll.type.dims}}
// A workflow step mapped over `model`, whose run at `model="a"` has a step `inner`.
const score: StepDoc = {
  ...roll, id: 'score', name: 'score', kind: 'workflow', method: 'sweep.score', mapOver: ['model'], replicaDims: [],
  type: {...roll.type, dims: [['model', 'str']]},
}
const inner: StepDoc = {...roll, id: 'inner', name: 'inner'}
const rootLevel: Level = {...built, steps: [...built.steps, judge, expand, score]}
const levels: Record<string, Level> = {root: rootLevel, 'root/score[model:"a"]': {...built, pathname: 'root/score[model:"a"]', steps: [inner]}}

const View = () => null
const ROLLS = declareView(InvocationPanel, 'roll', 'test.Rolls')
const DETAIL = declareView(InvocationPanel, 'roll', 'detail', {within: ROLLS, params: ['point']})
const catalog: Catalog = {
  root: 'root',
  registry: dispatchRegistry([renderer(ROLLS, View), renderer(DETAIL, View)]),
  level: (pathname) => (levels[pathname] ? {status: 'ready', level: levels[pathname]} : {status: 'pending'}),
}
const rootCentre: Centre = {address: {view: 'trace', location: {root: 'step', steps: [], path: []}}, keys: {}}

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>
let state: ColumnsState

function Harness({slice, centre}: {slice: Params; centre: Centre}) {
  state = useColumns(slice, catalog, centre)
  return null
}

async function mount(slice: Params = {}, centre: Centre = rootCentre) {
  await act(async () => root.render(<Harness slice={slice} centre={centre} />))
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  host = document.createElement('div')
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  vi.unstubAllGlobals()
})

const link = (to: string, params: Params = {}) => ({rel: parseRel(to).rel, params})
const panels = () => allPanels(state.columns)
const describe = (panel: ReturnType<typeof panels>[number]) => `${panel.pinned ? 'pinned' : 'browses'} ${state.stateOf(panel.id).resolution.text}`
const shown = () => panels().map(describe)
/** The panels in sequence, rather than as laid out. */
const ordered = () => sequence(state.columns).map(describe)

async function follow(to: string, params: Params = {}, origin = TRACE, options: {pin?: boolean} = {}) {
  await act(async () => state.follow(link(to, params), origin, options))
}

it('navigates the browsing panel after the view on a plain click, remembering what it showed', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'})
  await follow('(/roll)', {seed: 'b'})
  expect(shown()).toEqual(['browses test.Rolls(/roll, seed=b)'])
  const [panel] = panels()
  expect(panel!.back).toHaveLength(1)
  await act(async () => state.history(panel!.id, -1))
  expect(shown()).toEqual(['browses test.Rolls(/roll, seed=a)'])
})

it('opens a pinned panel on Alt, which a plain click then leaves alone', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'})
  await follow('(/roll)', {seed: 'b'}, TRACE, {pin: true})
  await follow('(/grade)')
  expect(shown()).toEqual(['browses (/grade)', 'pinned test.Rolls(/roll, seed=b)'])
})

it('reveals a pinned panel already showing what a plain click names, rather than navigating to it', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'}, TRACE, {pin: true})
  await follow('(/grade)')
  await follow('(/roll)', {seed: 'a'})
  expect(shown()).toEqual(['pinned test.Rolls(/roll, seed=a)', 'browses (/grade)'])
})

it('keeps the panels following a panel navigated at its location, whatever its view and params', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'})
  const first = panels()[0]!.id
  await follow('(.output)', {}, first)
  await follow('(/roll)', {seed: 'b'})
  expect(ordered()).toEqual(['browses test.Rolls(/roll, seed=b)', 'browses (/roll.output, seed=b)'])
  await act(async () => state.setAddress(first, {view: 'table', location: {root: 'step', steps: ['roll'], path: []}}))
  expect(ordered()).toEqual(['browses table(/roll, seed=b)', 'browses (/roll.output, seed=b)'])
  await act(async () => state.history(first, -1))
  expect(ordered()).toEqual(['browses test.Rolls(/roll, seed=b)', 'browses (/roll.output, seed=b)'])
})

it('closes the panels following a panel navigated to another location, even where they would still resolve, and brings them back with back', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'})
  const first = panels()[0]!.id
  await follow('(.output)', {}, first)
  const second = panels()[1]!.id
  await follow('(/grade)')
  expect(ordered()).toEqual(['browses (/grade)'])
  await act(async () => state.history(first, -1))
  expect(ordered()).toEqual(['browses test.Rolls(/roll, seed=a)', 'browses (/roll.output, seed=a)'])
  expect(panels()[1]!.id).toBe(second)
  await act(async () => state.history(first, 1))
  expect(ordered()).toEqual(['browses (/grade)'])
  await act(async () => state.history(first, -1))
  expect(ordered()).toEqual(['browses test.Rolls(/roll, seed=a)', 'browses (/roll.output, seed=a)'])
})

it('brings back the panels a navigation closed in a new column when theirs closed, before a pinned panel opened since', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'})
  const first = panels()[0]!.id
  await follow('(.output)', {}, first, {placement: {kind: 'newColumn', index: 1}})
  const second = panels()[1]!.id
  await follow('(/grade)')
  await follow('(/judge)', {}, first, {pin: true})
  await act(async () => state.history(first, -1))
  expect(ordered()).toEqual([
    'browses test.Rolls(/roll, seed=a)', 'browses (/roll.output, seed=a)', 'pinned test.Rolls(/judge)',
  ])
  expect(state.columns.map((column) => column.panels.map((panel) => panel.id))).toEqual([[first, expect.any(String)], [second]])
})

it('keeps a browsing panel relative to the one before it, so it follows that one and the slice; a pinned one follows nothing', async () => {
  await mount({seed: 'a'})
  await follow('test.Rolls(/roll)')
  const first = panels()[0]!.id
  await follow('test.Rolls/detail()', {point: 3}, first)
  expect(shown()).toEqual(['browses test.Rolls(/roll, seed=a)', 'browses test.Rolls/detail(/roll, point=3, seed=a)'])
  await mount({seed: 'b'})
  expect(shown()[1]).toBe('browses test.Rolls/detail(/roll, point=3, seed=b)')
  const second = panels()[1]!.id
  expect(state.stateOf(second).sources).toEqual({seed: 'slice', point: 'own'})
  await act(async () => state.pin(second))
  await mount({seed: 'c'})
  expect(shown()).toEqual(['browses test.Rolls(/roll, seed=c)', 'pinned test.Rolls/detail(/roll, point=3, seed=b)'])
})

it('closes a browsing view declared within another when the panel before it stops showing that one, and keeps a pinned one', async () => {
  await mount()
  await follow('test.Rolls(/roll)')
  const first = panels()[0]!.id
  await follow('test.Rolls/detail()', {point: 1}, first)
  await follow('test.Rolls/detail()', {point: 2}, first, {pin: true})
  await follow('table(/roll)')
  expect(shown()).toEqual(['browses table(/roll)', 'pinned test.Rolls/detail(/roll, point=2)'])
})

it('closes a view declared within another opened after a panel not showing that one', async () => {
  await mount()
  await follow('(/grade)')
  await follow('test.Rolls/detail(../roll)', {seed: 'a', point: 3}, panels()[0]!.id)
  expect(shown()).toEqual(['browses (/grade)'])
})

it('closes the panels following a closed panel, up to a pinned one, which follows the panel left before it', async () => {
  await mount()
  await follow('test.Rolls(/roll)', {seed: 'a'})
  const first = panels()[0]!.id
  await follow('test.Rolls/detail()', {point: 2}, first, {pin: true})
  const pinned = panels()[1]!.id
  await follow('test.Rolls/detail()', {point: 1}, first)
  expect(ordered()).toEqual([
    'browses test.Rolls(/roll, seed=a)',
    'browses test.Rolls/detail(/roll, point=1, seed=a)',
    'pinned test.Rolls/detail(/roll, point=2, seed=a)',
  ])
  await act(async () => state.close(first))
  expect(shown()).toEqual(['pinned test.Rolls/detail(/roll, point=2, seed=a)'])
  await follow('invocation()', {}, pinned)
  expect(shown()).toEqual(['pinned test.Rolls/detail(/roll, point=2, seed=a)', 'browses invocation(/roll, seed=a)'])
  await act(async () => state.close(pinned))
  expect(shown()).toEqual([])
})

it('pins a panel where it is, at its address whole and every binding it has, and the panels after it follow it', async () => {
  await mount({seed: 'a'})
  await follow('test.Rolls(/roll)')
  const first = panels()[0]!.id
  await follow('test.Rolls/detail()', {point: 1}, first)
  await act(async () => state.pin(first))
  expect(shown()).toEqual(['pinned test.Rolls(/roll, seed=a)', 'browses test.Rolls/detail(/roll, point=1, seed=a)'])
  expect(panels()[0]!.rel.location.kind).toBe('absolute')
  expect(panels()[0]!.own).toEqual({seed: 'a'})
  await mount({seed: 'b'})
  expect(shown()).toEqual(['pinned test.Rolls(/roll, seed=a)', 'browses test.Rolls/detail(/roll, point=1, seed=a)'])
  await follow('test.Rolls/detail()', {point: 2}, first)
  expect(shown()).toEqual(['pinned test.Rolls(/roll, seed=a)', 'browses test.Rolls/detail(/roll, point=2, seed=a)'])
})

it('opens a browsing panel before a pinned one after the view, leaving the pinned one as it is', async () => {
  await mount()
  await follow('test.Rolls(/roll)', {seed: 'a'})
  const first = panels()[0]!.id
  await follow('test.Rolls/detail()', {point: 1}, first)
  await act(async () => state.pin(panels()[1]!.id))
  await follow('test.Rolls/detail()', {point: 3}, first)
  expect(ordered()).toEqual([
    'browses test.Rolls(/roll, seed=a)',
    'browses test.Rolls/detail(/roll, point=3, seed=a)',
    'pinned test.Rolls/detail(/roll, point=1, seed=a)',
  ])
})

it('navigates the panel after a pinned panel clicked in, leaving the panels before it open', async () => {
  await mount()
  await follow('(/grade)')
  await follow('test.Rolls(../roll)', {seed: 'a'}, panels()[0]!.id)
  const second = panels()[1]!.id
  await follow('test.Rolls/detail()', {point: 1}, second)
  await act(async () => state.pin(second))
  await follow('test.Rolls/detail()', {point: 2}, second)
  expect(ordered()).toEqual([
    'browses (/grade)',
    'pinned test.Rolls(/roll, seed=a)',
    'browses test.Rolls/detail(/roll, point=2, seed=a)',
  ])
  // A click in the trace navigates the first panel, and stops at the pinned one.
  await follow('(/roll)', {seed: 'b'})
  expect(ordered()).toEqual([
    'browses test.Rolls(/roll, seed=b)',
    'pinned test.Rolls(/roll, seed=a)',
    'browses test.Rolls/detail(/roll, point=2, seed=a)',
  ])
})

it('opens a dropped link where it was dropped: browsing when its view has no browsing panel after it, else pinned', async () => {
  await mount()
  const drop = (to: string, index: number) => act(async () => state.follow(link(to), TRACE, {placement: {kind: 'newColumn', index}}))
  await drop('(/roll)', 0)
  expect(shown()).toEqual(['browses test.Rolls(/roll)'])
  await drop('(/grade)', 1)
  expect(shown()).toEqual(['browses test.Rolls(/roll)', 'pinned (/grade)'])
  expect(state.columns).toHaveLength(2)
})

it('moves a panel without changing what it follows, and spares one dropped in place of the panel it follows', async () => {
  await mount()
  await follow('test.Rolls(/roll)', {seed: 'a'})
  const first = panels()[0]!.id
  await follow('(.output)', {point: 1}, first)
  const second = panels()[1]!.id
  await act(async () => state.move(second, {kind: 'newColumn', index: 0}))
  expect(shown()).toEqual(['browses (/roll.output, seed=a)', 'browses test.Rolls(/roll, seed=a)'])
  await follow('(/roll)', {seed: 'b'})
  expect(shown()).toEqual(['browses (/roll.output, seed=b)', 'browses test.Rolls(/roll, seed=b)'])
  await act(async () => state.move(second, {kind: 'replace', panelId: first}))
  expect(shown()).toEqual(['browses (/roll.output, seed=b)'])
  expect(panels()[0]!.prevId).toBeNull()
})

it('writes a node\'s default view into its address, so a view declared within it can follow it', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'})
  await follow('test.Rolls/detail()', {point: 2}, panels()[0]!.id)
  expect(shown()).toEqual(['browses test.Rolls(/roll, seed=a)', 'browses test.Rolls/detail(/roll, point=2, seed=a)'])
})

it('says what a link names is open, and whether in the browsing panel after its view', async () => {
  await mount()
  await follow('(/roll)', {seed: 'a'})
  await follow('(/roll)', {seed: 'b'}, TRACE, {pin: true})
  expect(state.linkState(link('(/roll)', {seed: 'a'}), TRACE)).toEqual({selected: true, open: true})
  expect(state.linkState(link('(/roll)', {seed: 'b'}), TRACE)).toEqual({selected: false, open: true})
  expect(state.linkState(link('(/grade)'), TRACE)).toEqual({selected: false, open: false})
})

it('moves a panel opened from an invocation\'s input along when the trace picks another invocation', async () => {
  await mount()
  await follow('invocation(/roll)', {seed: 'a'})
  const invocation = panels()[0]!.id
  // The input chip of `invocation`, passing the invocation's keys as DefaultInvocationPanel does.
  await follow('(.inputs.seed)', {seed: 'a'}, invocation)
  expect(shown()).toEqual(['browses invocation(/roll, seed=a)', 'browses (/roll.inputs.seed, seed=a)'])
  await follow('invocation(/roll)', {seed: 'b'})
  expect(shown()).toEqual(['browses invocation(/roll, seed=b)', 'browses (/roll.inputs.seed, seed=b)'])
  expect(state.stateOf(panels()[1]!.id).resolution.problem).toBeNull()
})

it('lists the views of its function a panel shows a node through, not its invocation or a view within another', async () => {
  await mount({seed: 'a'})
  await follow('invocation(/roll)')
  expect(state.viewsShownOf(['roll'])).toEqual([])
  await follow('test.Rolls(/roll)', {}, TRACE, {pin: true})
  const pinned = panels()[1]!.id
  await follow('test.Rolls/detail()', {point: 3}, pinned, {pin: true})
  expect(state.viewsShownOf(['roll'])).toEqual(['test.Rolls'])
})

it('closes a browsing panel whose data has no place at its path, only while it still shows that address', async () => {
  await mount()
  await follow('invocation(/judge)', {seed: 'a'})
  const invocation = panels()[0]!.id
  await follow('(.output.call)', {}, invocation)
  const child = panels()[1]!.id
  const stale = state.stateOf(child).resolution.text
  await follow('invocation(/judge)', {seed: 'b'})
  await act(async () => state.namesNothing(child, stale))
  expect(shown()).toEqual(['browses invocation(/judge, seed=b)', 'browses (/judge.output.call, seed=b)'])
  await act(async () => state.namesNothing(child, state.stateOf(child).resolution.text))
  expect(shown()).toEqual(['browses invocation(/judge, seed=b)'])
})

it('keeps a pinned panel when its data has no place at its path', async () => {
  await mount()
  await follow('invocation(/judge)', {seed: 'a'})
  await follow('(.output.call)', {}, panels()[0]!.id, {pin: true})
  const child = panels()[1]!.id
  await act(async () => state.namesNothing(child, state.stateOf(child).resolution.text))
  expect(shown()).toEqual(['browses invocation(/judge, seed=a)', 'pinned (/judge.output.call, seed=a)'])
})

it('moves the panels opened down a chain of entity chips along when the first one is navigated, and closes them at another chip', async () => {
  await mount()
  await follow('invocation(/judge)', {seed: 'a'})
  // A chip at `path` inside a panel, as EntityLinkImpl builds it from the chip's place.
  const chip = async (panelId: string, path: string[]) => {
    const address = state.stateOf(panelId).address!
    await act(async () => state.follow(linkTarget('cid', {base: panelBase(panelId, address), path, keys: {}}), panelId))
  }
  const [a] = panels()
  await chip(a!.id, ['call'])
  const b = panels()[1]!
  await chip(b.id, ['x'])
  expect(shown()).toEqual(['browses invocation(/judge, seed=a)', 'browses (/judge.output.call, seed=a)', 'browses (/judge.output.call.x, seed=a)'])
  await follow('invocation(/judge)', {seed: 'b'})
  expect(shown()).toEqual(['browses invocation(/judge, seed=b)', 'browses (/judge.output.call, seed=b)', 'browses (/judge.output.call.x, seed=b)'])
  await chip(a!.id, ['other'])
  expect(shown()).toEqual(['browses invocation(/judge, seed=b)', 'browses (/judge.output.other, seed=b)'])
})

it('binds a pick inside a browsing panel itself, and pins it at a pick leaving out a binding it inherits', async () => {
  await mount({seed: 'a', sample: 1})
  await follow('invocation(/expand)')
  const panel = panels()[0]!.id
  expect(shown()).toEqual(['browses invocation(/expand, sample=1, seed=a)'])
  await act(async () => state.setBindings(panel, {seed: 'a', sample: 3}))
  expect(shown()).toEqual(['browses invocation(/expand, sample=3, seed=a)'])
  expect(state.stateOf(panel).sources).toEqual({seed: 'slice', sample: 'own'})
  // Leaving out a binding of its own inherits it again.
  await act(async () => state.setBindings(panel, {seed: 'a'}))
  expect(shown()).toEqual(['browses invocation(/expand, sample=1, seed=a)'])
  await act(async () => state.setBindings(panel, {seed: 'a'}))
  expect(shown()).toEqual(['pinned invocation(/expand, seed=a)'])
  expect(state.stateOf(panel).sources).toEqual({seed: 'own'})
  await mount({seed: 'b', sample: 2})
  expect(shown()).toEqual(['pinned invocation(/expand, seed=a)'])
})

it('resolves a step inside a mapped workflow through the run its key picks, asking for the key until it is bound', async () => {
  await mount()
  await follow('(/score/inner)')
  const panel = panels()[0]!.id
  const resolution = () => state.stateOf(panel).resolution
  expect(resolution().problem).toBe('pick model to look inside score')
  expect(resolution().structural).toBe(false)
  await act(async () => state.setOwn(panel, {model: 'a'}))
  expect(shown()).toEqual(['browses test.Rolls(/score/inner, model=a)'])
  expect(resolution().target?.kind).toBe('node')
  // A run not loaded yet is waited for, not closed.
  await act(async () => state.setOwn(panel, {model: 'b'}))
  expect(resolution()).toMatchObject({loading: true, structural: false})
  expect(shown()).toEqual(['browses (/score/inner, model=b)'])
})

it('shows a node\'s code, and the root workflow\'s, as the code version its level ran, through the keys of the workflows on the way', async () => {
  await mount()
  const resolution = () => state.stateOf(panels()[0]!.id).resolution
  await follow('code(/roll)')
  expect(resolution().target).toEqual({kind: 'code', name: roll.method, codeVersion: roll.codeVersion})
  await follow('code(/)')
  expect(resolution().target).toEqual({kind: 'code', name: rootLevel.workflowName, codeVersion: rootLevel.codeVersion})
  await follow('code(/score/inner)')
  expect(resolution().problem).toBe('pick model to look inside score')
  await follow('code(/score/inner)', {model: 'a'})
  expect(resolution().text).toBe('code(/score/inner, model=a)')
  expect(resolution().target).toEqual({kind: 'code', name: inner.method, codeVersion: inner.codeVersion})
  await follow('code(/roll.output)', {}, TRACE, {pin: true})
  expect(state.stateOf(panels().at(-1)!.id).resolution).toMatchObject({target: null, structural: true})
})

it('keeps every panel as it is when the centre moves to another level, binding the level\'s keys from a link in the trace', async () => {
  const inside: Centre = {address: {view: 'trace', location: {root: 'step', steps: ['score'], path: []}}, keys: {model: 'a'}}
  await mount({}, inside)
  await follow('(/score/inner)', {seed: 'x'})
  await follow('(/roll)', {seed: 'a'}, TRACE, {pin: true})
  expect(shown()).toEqual(['browses test.Rolls(/score/inner, model=a, seed=x)', 'pinned test.Rolls(/roll, seed=a)'])
  expect(panels()[0]!.own).toEqual({model: 'a', seed: 'x'})
  await mount({}, rootCentre)
  expect(shown()).toEqual(['browses test.Rolls(/score/inner, model=a, seed=x)', 'pinned test.Rolls(/roll, seed=a)'])
})

it('follows a relative link from a view in the centre, against the centre\'s address', async () => {
  const centre: Centre = {address: {view: 'test.Overview', location: {root: 'step', steps: ['score'], path: []}}, keys: {model: 'a'}}
  await mount({}, centre)
  await follow('(.output)')
  expect(shown()).toEqual(['browses (/score.output, model=a)'])
  expect(panels()[0]!.rel.location.kind).toBe('absolute')
})
