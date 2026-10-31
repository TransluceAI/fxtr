// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { CardTable } from '../src/components/CardTable'
import { useCardMode } from '../src/lib/cardTable'
import { LevelStateContext, type CardTableState } from '../src/lib/levelState'
import { defaultAnchor } from '../src/lib/scrollAnchor'
import { buildLevel } from '../src/model/level'
import type { KeyValue } from '../src/types'
import { base64Cbor, followed, inProgress, levelState, overviewOf, recordingOpener, spawn } from './fixtures'
import { OpenEntityContext } from '../src/lib/openEntity'
import { Store, useStoreSelector } from '../src/lib/store'

let root: Root
let host: HTMLDivElement

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  // JSDOM lays out no text: content is 7px a character.
  Range.prototype.getBoundingClientRect = function (this: Range) { return { width: this.toString().length * 7 } as DOMRect }
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  delete (Range.prototype as Partial<Range>).getBoundingClientRect
})

/** The `roll` step of the fixture level, with `s3` and `s7` failed unless `failures` is false. */
function rollStep({ sampled = false, failures = true } = {}) {
  const invocation = inProgress()
  if (sampled) invocation.nodes = invocation.nodes.map((n) => n.identity !== 'roll' ? n : { ...n,
    type: { dims: [['seed', 'str'], ['sample', 'int']], schema: 'str' } })
  if (failures) invocation.spawns = [...invocation.spawns, spawn('roll', ['s3'], 'failed', null), spawn('roll', ['s7'], 'failed', null)]
  return buildLevel('job', overviewOf(invocation)).steps.find((step) => step.id === 'roll')!
}

/** How each seed's invocation of `roll` stands: s3 and s7 failed, s1 is running. */
const seedState = (seed: string) => ['s3', 's7'].includes(seed) ? 'failed' : seed === 's1' ? 'running' : 'completed'
const seedSpawn = (seed: string) => ({nodeIdentity: 'roll', key: [seed], childPathname: null, stepCacheKey: `k/${seed}`,
  resultCid: seedState(seed) === 'completed' ? `cid-${seed}` : null, state: seedState(seed)})

/** The `roll` card's table over 30 rows, keyed by `seed` and (`sampled`, by default) a second dim
 *  `sample` of one value produced inside each invocation, three rows an invocation (without it,
 *  one row each, which the card lists as invocations), in a level state it holds itself; every slice
 *  it sets is recorded with the card it anchors, every link it follows too. `steps` holds the step
 *  the card shows. */
async function renderTable(count = 30, { sampled = true, state = {} as Partial<CardTableState> } = {}) {
  const table = rollStep({ sampled })
  const steps = new Store(table)
  const pages: Record<string, unknown>[] = []
  const seeds = Array.from({length: count}, (_, i) => `s${i}`)
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {query, variables} = JSON.parse(init.body)
    if (query.includes('r0: runPage')) {
      // The invocation behind each row, by its selection.
      const answers = Object.fromEntries(Object.entries(variables).filter(([name]) => /^s\d+$/.test(name)).map(([name, selection]) =>
        [`r${name.slice(1)}`, {items: seeds.filter((seed) => seed === (selection as {seed: string}).seed).map(seedSpawn)}]))
      return {ok: true, json: async () => ({data: answers})}
    }
    if (query.includes('invocationPage(')) {
      // Every invocation's cells, filtered and sorted as the backend does: by substring, and in
      // key order between ties, those without the sorted cell last.
      pages.push(variables)
      const cellsOf = (seed: string): Record<string, string | number> => ({'key:seed': seed, 'state:': seedState(seed),
        ...(seedState(seed) === 'completed' ? {'rows:': sampled ? 3 : 1, ...(sampled ? {} : {'value:': `rolled ${seed}`})} : {})})
      let matching = seeds.filter((seed) => (!variables.selection.seed || variables.selection.seed === seed)
        && (!variables.state || seedState(seed) === variables.state)
        && (variables.filters as {key: string; text: string}[]).every(({key, text}) => String(cellsOf(seed)[key] ?? '').includes(text)))
      if (variables.sort) {
        const {key, direction} = variables.sort as {key: string; direction: string}
        const present = matching.filter((seed) => key in cellsOf(seed))
          .sort((a, b) => String(cellsOf(a)[key]).localeCompare(String(cellsOf(b)[key])) * (direction === 'desc' ? -1 : 1))
        matching = [...present, ...matching.filter((seed) => !(key in cellsOf(seed)))]
      }
      const offset = variables.last ? Math.floor((matching.length - 1) / variables.first) * variables.first : variables.offset
      const items = matching.slice(offset, offset + variables.first).map((seed) => ({spawn: seedSpawn(seed),
        rowCount: seedState(seed) === 'completed' ? (sampled ? 3 : 1) : null,
        value: seedState(seed) === 'completed' && !sampled ? base64Cbor(`rolled ${seed}`) : null}))
      return {ok: true, json: async () => ({data: {invocationPage: {items, total: matching.length, offset}}})}
    }
    if (query.includes('outputPage')) {
      pages.push(variables)
      const needle = (variables.filters as {text: string}[]).map((filter) => filter.text).join('')
      const seeds = Array.from({length: count}, (_, i) => `s${i}`)
        .filter((seed) => !variables.source.selection?.seed || variables.source.selection.seed === seed)
        .filter((seed) => `rolled ${seed}`.includes(needle))
      const offset = variables.last ? Math.floor((seeds.length - 1) / variables.first) * variables.first : variables.offset
      const rows = seeds.slice(offset, offset + variables.first).map((seed) => ({key: sampled ? [seed, 0] : [seed], value: base64Cbor(`rolled ${seed}`)}))
      return {ok: true, json: async () => ({data: {outputPage: {type: table.type, rows, total: seeds.length, offset,
        hasMore: offset + variables.first < seeds.length}}})}
    }
    if (query.includes('runPage(')) {
      pages.push(variables)
      const matching = seeds.filter((seed) => (!variables.selection.seed || variables.selection.seed === seed)
        && (!variables.state || seedState(seed) === variables.state))
      const offset = variables.offset ?? 0
      const items = matching.slice(offset, offset + variables.first).map(seedSpawn)
      return {ok: true, json: async () => ({data: {runPage: {items, total: matching.length, offset: variables.offset ?? null, next: null}}})}
    }
    if (query.includes('step(')) {
      const seed = (variables.cacheKey as string).slice(2)
      return {ok: true, json: async () => ({data: {step: {stepName: 'roll', requestCid: 'request', state: 'failed', resultCid: null,
        attempt: {state: 'failed', startedAt: '2026-01-01T00:00:00Z', finishedAt: '2026-01-01T00:00:01Z',
          error: {typeName: 'ValueError', message: `bad seed ${seed}\nwhile rolling`}}, logs: [{seq: 1, message: 'rolling', at: '2026-01-01T00:00:00Z'}]}}})}
    }
    return {ok: true, json: async () => ({data: {keyPage: {items: [], total: 30, offset: 0, next: null}}})}
  }))
  const slices: {slice: Readonly<Record<string, KeyValue>>; anchor: string | undefined}[] = []
  const cardTables = new Store<ReadonlyMap<string, CardTableState>>(new Map(Object.keys(state).length === 0 ? [] : [['roll', {
    sliceKey: '{}', mode: 'data', filters: {}, sort: null, offset: 0, rowsShown: true, invocationFilters: {}, invocationSort: null, failedOnly: false,
    invocationsOffset: 0, keysPick: null, ...state }]]))
  const { opener, follows } = recordingOpener()
  function Level() {
    const [slice, setSlice] = useState<Readonly<Record<string, KeyValue>>>({})
    const state = levelState({
      slice, cardTables,
      setSlice: (next, anchor) => {
        if (typeof next === 'function') throw new Error('unexpected update')
        slices.push({slice: next, anchor})
        setSlice(next)
      },
    })
    const step = useStoreSelector(steps, (current) => current)
    return <LevelStateContext.Provider value={state}>
      <OpenEntityContext.Provider value={opener}><CardTable step={step} matching={null} /></OpenEntityContext.Provider>
    </LevelStateContext.Provider>
  }
  await act(async () => root.render(<Level />))
  return { pages, slices, follows, tables: cardTables.get, cardTables, steps }
}

const toggle = () => host.querySelector<HTMLButtonElement>('button[aria-label$=" table"]')!

const padRows = () => host.querySelectorAll('tbody tr[aria-hidden]').length

const rowOf = (text: string) => Array.from(host.querySelectorAll('tbody tr')).find((row) => row.textContent?.includes(text))!

it('shows ten rows a page, sorts every column, and filters only the value columns', async () => {
  const { pages, follows, steps } = await renderTable()
  expect(host.querySelectorAll('tbody tr')).toHaveLength(10)
  expect(pages.at(-1)).toMatchObject({offset: 0, first: 10})
  expect(host.querySelector('[aria-label="Sort by seed"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="Filter seed"]')).toBeNull()
  expect(host.querySelector('[aria-label="Filter value"]')).not.toBeNull()
  // A row opens the invocation that produced it, narrowed to the row by its keys, in place of a
  // modal.
  expect(host.querySelector('[aria-label^="Expand"]')).toBeNull()
  await act(async () => rowOf('rolled s4').dispatchEvent(new MouseEvent('click', {bubbles: true})))
  expect(follows.map(followed)).toEqual(['invocation(/roll, sample=0, seed=s4)'])
  // While that panel is open its row is lit, and only it.
  await act(async () => steps.set({ ...steps.get() }))
  expect(Array.from(host.querySelectorAll('tbody tr[data-selected]')).map((row) => row.textContent)).toEqual([rowOf('rolled s4').textContent])
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Next page"]')!.click())
  expect(pages.at(-1)).toMatchObject({offset: 10, first: 10})
  expect(host.textContent).toContain('rolled s10')
})

it('shows a table\'s rows until hidden, with several key columns too, and only its header row then', async () => {
  const { tables } = await renderTable(30, { sampled: true })
  expect(toggle().getAttribute('aria-label')).toBe('Hide table')
  expect(host.querySelectorAll('tbody tr')).toHaveLength(10)
  await act(async () => toggle().click())
  expect(tables().get('roll')?.rowsShown).toBe(false)
  expect(toggle().getAttribute('aria-label')).toBe('Show table')
  expect(host.querySelector('thead')).not.toBeNull()
  expect(host.querySelector('tbody')).toBeNull()
  expect(host.querySelector('[aria-label="Next page"]')).toBeNull()
  // The header row keeps its key headers' slice controls, but draws no sorting or filters.
  expect(host.querySelector('button[aria-label="Show seed values"]')).not.toBeNull()
  expect(host.querySelector('[aria-label^="Sort by"]')).toBeNull()
  expect(host.querySelector('[aria-label^="Filter"]')).toBeNull()
  expect(host.querySelector('thead button[class*="thSort"]')).toBeNull()
  await act(async () => toggle().click())
  expect(tables().get('roll')?.rowsShown).toBe(true)
  expect(host.querySelectorAll('tbody tr')).toHaveLength(10)
})

it.each(['MacIntel', 'Win32', 'Linux x86_64'])('slices rows only with the platform modifier on %s', async (platform) => {
  vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform)
  const modifier = platform === 'MacIntel' ? { metaKey: true } : { ctrlKey: true }
  const wrongModifier = platform === 'MacIntel' ? { ctrlKey: true } : { metaKey: true }
  const key = platform === 'MacIntel' ? 'Meta' : 'Control'
  const { slices } = await renderTable()
  // The header row: seed's and sample's key columns, then the value column.
  const valueHeader = () => host.querySelectorAll('thead th')[2]!
  // Not sliced to one row, the value column's header names its type under its name.
  expect(valueHeader().textContent).toBe('valuestr')
  // Only the platform modifier marks the rows as clickable.
  const root = document.documentElement
  for (const modifiers of [wrongModifier, { altKey: true }, { metaKey: true, ctrlKey: true }]) {
    window.dispatchEvent(new KeyboardEvent('keydown', modifiers))
    expect(root.hasAttribute('data-modifier-held')).toBe(false)
    await act(async () => rowOf('rolled s4').dispatchEvent(new MouseEvent('click', {bubbles: true, ...modifiers})))
  }
  expect(slices).toEqual([])
  window.dispatchEvent(new KeyboardEvent('keydown', {key, ...modifier}))
  expect(root.hasAttribute('data-modifier-held')).toBe(true)
  window.dispatchEvent(new KeyboardEvent('keyup', {key}))
  expect(root.hasAttribute('data-modifier-held')).toBe(false)
  // Pointer movement also corrects the highlight after a missed key release.
  window.dispatchEvent(new MouseEvent('mousemove', modifier))
  expect(root.hasAttribute('data-modifier-held')).toBe(true)
  window.dispatchEvent(new MouseEvent('mousemove', {altKey: true}))
  expect(root.hasAttribute('data-modifier-held')).toBe(false)
  // A plain click (which opens the row), an Option-click, or a double click picks nothing.
  await act(async () => rowOf('rolled s4').dispatchEvent(new MouseEvent('click', {bubbles: true})))
  await act(async () => rowOf('rolled s4').dispatchEvent(new MouseEvent('click', {bubbles: true, altKey: true})))
  await act(async () => rowOf('rolled s4').dispatchEvent(new MouseEvent('dblclick', {bubbles: true})))
  expect(slices).toEqual([])
  await act(async () => rowOf('rolled s4').dispatchEvent(new MouseEvent('click', {bubbles: true, ...modifier})))
  // The pick is made in this card, so the trace holds this card still.
  expect(slices).toEqual([{slice: {seed: 's4', sample: 0}, anchor: 'roll'}])
  expect(host.querySelectorAll('tbody tr')).toHaveLength(1)
  // The sliced dim's header names the value it is sliced at. Every dim being sliced, the value
  // column's header names the one row's value once the rows are hidden, its type while shown.
  expect(host.querySelector('button[aria-label="Show seed values"]')!.textContent).toContain('s4')
  expect(valueHeader().textContent).toBe('valuestr')
  await act(async () => toggle().click())
  expect(valueHeader().textContent).toBe('valuerolled s4')
})

it('holds the first card whose top is on screen, or the one across the top of the viewport', () => {
  const cards = [{id: 'a', top: -300, bottom: -10}, {id: 'b', top: -10, bottom: 200}, {id: 'c', top: 200, bottom: 600}]
  expect(defaultAnchor(cards, 0, 500)).toBe('c')
  expect(defaultAnchor([{id: 'a', top: -300, bottom: 900}], 0, 500)).toBe('a')
  expect(defaultAnchor([{id: 'a', top: 600, bottom: 900}], 0, 500)).toBeNull()
})

it('keeps a full page\'s height and a pager on the last of several pages, but neither for one page', async () => {
  await renderTable(25)
  expect(padRows()).toBe(0)
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Last page"]')!.click())
  expect(host.textContent).toContain('rolled s24')
  expect(host.querySelectorAll('tbody tr:not([aria-hidden])')).toHaveLength(5)
  expect(padRows()).toBe(5)
  // Filtered down to one page, the table is as tall as its rows.
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Filter value"]')!.click())
  const input = document.querySelector<HTMLInputElement>('[placeholder="Filter value…"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 's2')
    input.dispatchEvent(new Event('input', {bubbles: true}))
  })
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 275)) })
  expect(host.querySelectorAll('tbody tr:not([aria-hidden])')).toHaveLength(6)
  expect(padRows()).toBe(0)
  // One page has no pager.
  expect(host.querySelector('.pagination')).toBeNull()
})

it('sizes key columns a unit and value columns to their content, and snaps a resize to that', async () => {
  const { MIN_UNIT } = await import('../src/lib/columnWidths')
  await renderTable()
  const widths = () => Array.from(host.querySelectorAll('col')).map((col) => col.style.width)
  // seed and sample (a unit each: JSDOM's table has no width), value (`rolled s0`: 9 characters,
  // 63px, and a cell's 26px of padding), the empty slack column, and the trailing one (the
  // invocations' buttons, and the column menu).
  const fitted = 9 * 7 + 26
  expect(widths()).toEqual([`${MIN_UNIT}px`, `${MIN_UNIT}px`, `${fitted}px`, '', '24px'])
  const grip = host.querySelector('th:nth-child(3) [class*="thGrip"]')!
  Object.assign(grip, { setPointerCapture: () => {} })
  const pointer = (type: string, clientX: number) =>
    act(async () => grip.dispatchEvent(Object.assign(new MouseEvent(type, {bubbles: true, clientX}), {pointerId: 1})))
  const guide = () => host.querySelector('[class*="snapGuide"]')
  await pointer('pointerdown', 0)
  await pointer('pointermove', 5)
  expect(widths()[2]).toBe(`${fitted}px`)
  await pointer('pointermove', 43)
  expect(widths()[2]).toBe(`${fitted + 43}px`)
  // Wider than its default, a line marks where it snaps back to, while the drag lasts.
  expect(guide()).not.toBeNull()
  await pointer('pointerup', 43)
  expect(guide()).toBeNull()
  // Dragged back to within a few pixels of its default, it snaps to it, and stays there once let go.
  await pointer('pointerdown', 0)
  await pointer('pointermove', -37)
  expect(widths()[2]).toBe(`${fitted}px`)
  await pointer('pointerup', -37)
  await pointer('pointerdown', 0)
  await pointer('pointermove', -20)
  expect(widths()[2]).toBe(`${fitted - 20}px`)
  expect(guide()).toBeNull()
})

it('gives key columns a unit, value columns their content, and the slack to those cut off', async () => {
  const { MIN_UNIT, MIN_VALUE_W, defaultWidths, unitWidth } = await import('../src/lib/columnWidths')
  expect(unitWidth(300)).toBe(MIN_UNIT)
  expect(unitWidth(1200)).toBe(200)
  const columns = [
    { key: 'model', group: 'key' as const, content: 400 },
    { key: 'score', group: 'value' as const, content: 30 },
    { key: 'name', group: 'value' as const, content: 150 },
    { key: 'transcript', group: 'value' as const, content: 5000 },
  ]
  // A unit of 200: the key a unit whatever its content, the int at the least, the name as it is,
  // the transcript at three units, and the room left over to the transcript, the one cut off.
  expect(Object.fromEntries(defaultWidths(columns, 1200))).toEqual(
    { model: 200, score: MIN_VALUE_W, name: 150, transcript: 600 + 1200 - (200 + MIN_VALUE_W + 150 + 600) })
  // Nothing cut off, the room stays empty; too little room, nothing shares it.
  expect(defaultWidths(columns.slice(0, 3), 1200).get('name')).toBe(150)
  expect(defaultWidths(columns, 600).get('transcript')).toBe(300)
  // Unmeasured, a value column is a unit.
  expect(defaultWidths([{ key: 'v', group: 'value', content: undefined }], 1200).get('v')).toBe(200)
})

it('lists the failed invocations alone, one row each with its error, while asked to', async () => {
  const { pages, cardTables, follows } = await renderTable(30, { state: { mode: 'invocations' } })
  const showFailed = (failedOnly: boolean) => act(async () => cardTables.update((tables) =>
    new Map(tables).set('roll', { ...tables.get('roll')!, failedOnly, invocationsOffset: 0 })))
  await showFailed(true)
  expect(pages.at(-1)).toMatchObject({state: 'failed', selection: {}, offset: 0, first: 10})
  const rows = host.querySelectorAll('tbody tr')
  expect(rows).toHaveLength(2)
  // The key cell, then the rest of the row as one cell holding the error, then the trailing one,
  // empty for an invocation that is not running.
  const cells = rows[0]!.querySelectorAll('td')
  expect(cells).toHaveLength(3)
  expect(cells[0]!.textContent).toBe('s3')
  // The state and value columns, and the slack after them.
  expect(cells[1]!.colSpan).toBe(3)
  expect(cells[2]!.childElementCount).toBe(0)
  // The trailing cell is red with it (the failure cell has the failure class alone).
  expect(cells[2]!.classList.contains(cells[1]!.className)).toBe(true)
  expect(rows[0]!.textContent).toContain('ValueError: bad seed s3')
  expect(rows[1]!.textContent).toContain('ValueError: bad seed s7')
  // They sort and filter as every invocation does; the key headers still slice.
  expect(host.querySelector('[aria-label="Sort by seed"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="Filter state"]')).not.toBeNull()
  expect(host.querySelector('button[aria-label="Show seed values"]')).not.toBeNull()
  // The row opens the invocation's panel.
  await act(async () => rows[0]!.dispatchEvent(new MouseEvent('click', {bubbles: true})))
  expect(followed(follows.at(-1))).toBe('invocation(/roll, seed=s3)')
  await showFailed(false)
  expect(host.querySelectorAll('tbody tr')).toHaveLength(10)
  expect(rowOf('s0').textContent).toBe('s0completed3 rows')
})

it('lists every invocation again once the run has no failures left to show, since their switch goes with them', async () => {
  const { steps } = await renderTable(30, { state: { mode: 'invocations', failedOnly: true } })
  expect(host.querySelectorAll('tbody tr')).toHaveLength(2)
  // The failed invocations are retried and complete.
  await act(async () => steps.set(rollStep({ failures: false })))
  expect(host.querySelectorAll('tbody tr')).toHaveLength(10)
})

it('lists the invocations with the value each produced, opening one on a click', async () => {
  const { pages, follows, cardTables, steps } = await renderTable(30, { sampled: false })
  expect(pages.at(-1)).toMatchObject({state: null, selection: {}, offset: 0, first: 10})
  const rows = host.querySelectorAll('tbody tr')
  expect(rows).toHaveLength(10)
  // One row each: after the state, the value column, headed as the rows' is, holds the
  // invocation's one row.
  expect(host.querySelectorAll('thead th')[2]!.textContent).toBe('valuestr')
  expect(rowOf('s0').textContent).toBe('s0completedrolled s0')
  // The row opens the invocation, so there is no button for it; only a running invocation has a
  // spinner at its right end, and nothing yet in its values.
  expect(host.querySelector('tbody button')).toBeNull()
  expect(rows[0]!.querySelector('[role="status"]')).toBeNull()
  expect(rows[1]!.lastElementChild!.querySelector('[aria-label="Invocation running"] [class*="spinner"]')).not.toBeNull()
  expect(rows[1]!.textContent).toBe('s1running—')
  await act(async () => rowOf('s0').dispatchEvent(new MouseEvent('click', {bubbles: true})))
  expect(followed(follows.at(-1))).toBe('invocation(/roll, seed=s0)')
  // Its row stands for the invocation its panel shows, so the row is lit.
  await act(async () => steps.set({ ...steps.get() }))
  expect(rowOf('s0').hasAttribute('data-selected')).toBe(true)
  expect(rowOf('s2').hasAttribute('data-selected')).toBe(false)
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Next page"]')!.click())
  expect(cardTables.get().get('roll')).toMatchObject({offset: 0, invocationsOffset: 10})
  expect(pages.at(-1)).toMatchObject({offset: 10, first: 10})
})

it('sorts and filters the invocations on the server, with filters and an order of their own', async () => {
  const { pages, cardTables } = await renderTable(30, { sampled: false })
  // Every column sorts, and the state and value columns filter too.
  expect(host.querySelector('[aria-label="Sort by seed"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="Filter state"]')).not.toBeNull()
  expect(host.querySelector('[aria-label="Filter value"]')).not.toBeNull()
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Filter state"]')!.click())
  const input = document.querySelector<HTMLInputElement>('[placeholder="Filter state…"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'fail')
    input.dispatchEvent(new Event('input', {bubbles: true}))
  })
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 275)) })
  expect(pages.at(-1)).toMatchObject({filters: [{key: 'state:', text: 'fail'}], offset: 0})
  expect(Array.from(host.querySelectorAll('tbody tr')).map((row) => row.firstElementChild!.textContent)).toEqual(['s3', 's7'])
  // A sort on the values puts those with none (unfinished) last.
  const valueSort = Array.from(host.querySelectorAll<HTMLButtonElement>('thead button'))
    .find((button) => button.textContent === 'value')!
  await act(async () => valueSort.click())
  expect(pages.at(-1)).toMatchObject({sort: {key: 'value:', direction: 'asc'}})
  // The rows' own filters and order are left as they were.
  expect(cardTables.get().get('roll')).toMatchObject({filters: {}, sort: null,
    invocationFilters: {'state:': 'fail'}, invocationSort: {key: 'value:', direction: 'asc'}})
})

it('says how many rows each invocation produced when it produces several, under no header', async () => {
  await renderTable(30, { sampled: true, state: { mode: 'invocations' } })
  const headers = Array.from(host.querySelectorAll('thead th')).map((th) => th.textContent)
  expect(headers.slice(0, 3)).toEqual(['seed30str', 'statestr', ''])
  expect(rowOf('s0').textContent).toBe('s0completed3 rows')
  // The count describes the rows rather than being one: faint and italic.
  expect(rowOf('s0').querySelectorAll('td')[2]!.className).toContain('tdDescribes')
  // Its column is sized to the counts (`3 rows`: 6 characters, 42px, and a cell's 26px).
  expect(Array.from(host.querySelectorAll('col')).map((col) => col.style.width)[2]).toBe(`${6 * 7 + 26}px`)
})

it('marks a row\'s invocation at its right end while it runs or once it failed, and opens nothing from there', async () => {
  const { pages, follows } = await renderTable()
  // The page's invocations are read at once, apart from the pages.
  expect(pages.every((page) => !('s0' in page))).toBe(true)
  const end = (seed: string) => rowOf(`rolled ${seed}`).lastElementChild!
  expect(end('s1').querySelector('[aria-label="Invocation running"] [class*="spinner"]')).not.toBeNull()
  expect(end('s3').querySelector('[aria-label="Invocation failed"]')).not.toBeNull()
  expect(end('s0').querySelector('[role="status"]')).toBeNull()
  expect(host.querySelector('tbody button')).toBeNull()
  // A click at the right end is the row's: its invocation, narrowed to it.
  await act(async () => end('s1').dispatchEvent(new MouseEvent('click', {bubbles: true})))
  expect(follows.map(followed)).toEqual(['invocation(/roll, sample=0, seed=s1)'])
})

it('picks an invocation in every key column at once from a value in one, and opens its panel', async () => {
  const table = rollStep()
  const step = { ...table, mapOver: ['seed', 'model'], type: { dims: [['seed', 'str'], ['model', 'str'], ['sample', 'int']], schema: 'str' } } as typeof table
  const runPages: Record<string, unknown>[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {query, variables} = JSON.parse(init.body)
    if (query.includes('keyPage(')) {
      const values = variables.dim === 'seed' ? ['s0', 's1'] : ['m0', 'm1']
      return {ok: true, json: async () => ({data: {keyPage: {items: values.map((value) => ({value, compatible: true, counts: {completed: 1}})),
        total: 2, offset: 0, next: null}}})}
    }
    if (query.includes('runPage(')) {
      runPages.push(variables.selection)
      // Every seed has m1; only s1 has m0.
      const keys = [['s0', 'm1'], ['s1', 'm0'], ['s1', 'm1']].filter(([seed, model]) =>
        (!variables.selection.seed || variables.selection.seed === seed) && (!variables.selection.model || variables.selection.model === model))
      return {ok: true, json: async () => ({data: {runPage: {items: keys.map((key) => spawn('roll', key, 'completed', 'cid')),
        total: keys.length, offset: null, next: null}}})}
    }
    return {ok: true, json: async () => ({data: {outputPage: {type: step.type, rows: [], total: 0, offset: 0, hasMore: false}}})}
  }))
  const cardTables = new Store<ReadonlyMap<string, CardTableState>>(new Map([['roll', {
    sliceKey: '{}', mode: 'keys', filters: {}, sort: null, offset: 0, rowsShown: true, invocationFilters: {}, invocationSort: null, failedOnly: false, invocationsOffset: 0, keysPick: null }]]))
  const { opener, follows } = recordingOpener()
  await act(async () => root.render(<LevelStateContext.Provider value={levelState({ cardTables })}>
    <OpenEntityContext.Provider value={opener}><CardTable step={step} matching={null} /></OpenEntityContext.Provider>
  </LevelStateContext.Provider>))
  const value = (dim: string, text: string) => Array.from(host.querySelectorAll(`[aria-label="${dim} keys"] button`))
    .find((button) => button.textContent === text) as HTMLButtonElement
  expect(value('seed', 's0')).toBeDefined()
  // Picking s1 picks the first model it has, and opens the node's panel there.
  await act(async () => value('seed', 's1').click())
  expect(cardTables.get().get('roll')?.keysPick).toEqual({seed: 's1', model: 'm0'})
  expect(followed(follows.at(-1))).toBe('(/roll, model=m0, seed=s1)')
  // The picked values are selected, as what the panel shows: drawn in selection blue.
  expect(value('seed', 's1').className).toContain('isSelection')
  expect(value('model', 'm0').className).toContain('isSelection')
  // Picking s0 keeps m0 if it can: it cannot, so it takes s0's first model.
  await act(async () => value('seed', 's0').click())
  expect(runPages.slice(-2)).toEqual([{seed: 's0', model: 'm0'}, {seed: 's0'}])
  expect(cardTables.get().get('roll')?.keysPick).toEqual({seed: 's0', model: 'm1'})
  expect(followed(follows.at(-1))).toBe('(/roll, model=m1, seed=s0)')
})

it('chooses a mode exactly while the table is shown: choosing the one shown hides it, any choice shows it', async () => {
  const step = rollStep({ sampled: true })
  const cardTables = new Store<ReadonlyMap<string, CardTableState>>(new Map())
  let mode: ReturnType<typeof useCardMode> = [null, () => {}]
  function Switch() {
    mode = useCardMode(step)
    return null
  }
  await act(async () => root.render(<LevelStateContext.Provider value={levelState({ cardTables })}><Switch /></LevelStateContext.Provider>))
  const state = () => cardTables.get().get('roll')
  expect(mode[0]).toBe('data')
  await act(async () => mode[1]('data'))
  expect(mode[0]).toBeNull()
  expect(state()).toMatchObject({ mode: 'data', rowsShown: false })
  await act(async () => mode[1]('data'))
  expect(mode[0]).toBe('data')
  await act(async () => mode[1]('data'))
  await act(async () => mode[1]('keys'))
  expect(mode[0]).toBe('keys')
  expect(state()).toMatchObject({ mode: 'keys', rowsShown: true })
  await act(async () => mode[1]('invocations'))
  expect(mode[0]).toBe('invocations')
})

it('steps from the lit row to the next or previous one on j and k while the table has focus', async () => {
  const { follows, steps } = await renderTable()
  await act(async () => rowOf('rolled s4').dispatchEvent(new MouseEvent('click', {bubbles: true})))
  await act(async () => steps.set({ ...steps.get() }))
  const table = host.querySelector<HTMLElement>('.table-panel')!
  const press = (key: string, init: KeyboardEventInit = {}) =>
    act(async () => { table.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true, ...init})) })
  await press('j')
  expect(followed(follows.at(-1))).toBe('invocation(/roll, sample=0, seed=s5)')
  await act(async () => steps.set({ ...steps.get() }))
  await press('k')
  await act(async () => steps.set({ ...steps.get() }))
  await press('k')
  expect(follows.slice(1).map(followed)).toEqual(['invocation(/roll, sample=0, seed=s5)',
    'invocation(/roll, sample=0, seed=s4)', 'invocation(/roll, sample=0, seed=s3)'])
  // A modified key is not a step.
  await press('j', {metaKey: true})
  expect(follows).toHaveLength(4)
})

it('turns the page on j from its last row and k from its first, opening the row beyond', async () => {
  const { pages, follows, steps } = await renderTable()
  const table = host.querySelector<HTMLElement>('.table-panel')!
  const press = (key: string) => act(async () => { table.dispatchEvent(new KeyboardEvent('keydown', {key, bubbles: true})) })
  const settle = async () => {
    for (let i = 0; i < 5; i++) await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
    await act(async () => steps.set({ ...steps.get() }))
  }
  await act(async () => rowOf('rolled s9').dispatchEvent(new MouseEvent('click', {bubbles: true})))
  await settle()
  await press('j')
  await settle()
  expect(pages.at(-1)).toMatchObject({offset: 10, first: 10})
  expect(followed(follows.at(-1))).toBe('invocation(/roll, sample=0, seed=s10)')
  await press('k')
  await settle()
  expect(pages.at(-1)).toMatchObject({offset: 0, first: 10})
  expect(followed(follows.at(-1))).toBe('invocation(/roll, sample=0, seed=s9)')
})
