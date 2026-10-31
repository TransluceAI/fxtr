// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Pagination, rowOffset } from '../src/components/Pagination'
import { useEntityMetadata, useQuery } from '../src/api/browse'
import { gql } from '../src/api/graphql'

let root: Root
let host: HTMLDivElement
function paginationText(container: Element = host): string {
  const range = container.querySelector('.pagination-range')!
  return (range.querySelector('input')?.value ?? '') + range.textContent
}

async function enterRow(value: string, container: Element = host) {
  const input = container.querySelector<HTMLInputElement>('[aria-label="Go to row"]')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', {bubbles: true}))
  })
  await act(async () => input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})))
}
/** Answer output pages empty, leaving every other read to the fetch stubbed so far. */
/** The node's output, and the page of invocations its card lists, empty; every other read as the
 *  test's own fetch answers it. */
function withEmptyOutput(table: import('../src/types').StepDoc) {
  const answer = globalThis.fetch
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: {body: string}) => {
    const {query} = JSON.parse(init.body)
    if (query.includes('invocationPage(')) return {ok: true, json: async () => ({data: {invocationPage: {items: [], total: 0, offset: 0}}})}
    if (!query.includes('outputPage')) return answer(url, init)
    return {ok: true, json: async () => ({data: {outputPage: {type: table.type, rows: [], total: 0, offset: 0, hasMore: false}}})}
  }))
}

/** A card's table with no rows, over a level slice it holds itself, and its key columns' values
 *  opened. */
async function renderKeys(
  table: import('../src/types').StepDoc,
  onSlice: (slice: Readonly<Record<string, string | number>>) => void = () => {},
) {
  const { CardTable } = await import('../src/components/CardTable')
  const { LevelStateContext } = await import('../src/lib/levelState')
  const { levelState, recordingOpener } = await import('./fixtures')
  const { OpenEntityContext } = await import('../src/lib/openEntity')
  withEmptyOutput(table)
  function Picker() {
    const [slice, setSlice] = useState<Readonly<Record<string, string | number>>>({})
    const state = levelState({ slice,
      setSlice: (next) => { if (typeof next === 'function') throw new Error('unexpected update'); onSlice(next); setSlice(next) } })
    return <LevelStateContext.Provider value={state}>
      <OpenEntityContext.Provider value={recordingOpener().opener}>
        <CardTable step={table} matching={null} />
      </OpenEntityContext.Provider>
    </LevelStateContext.Provider>
  }
  await act(async () => root.render(<Picker />))
  await act(async () => host.querySelector<HTMLButtonElement>('button[aria-label^="Show "][aria-expanded]')!.click())
}

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

it('shows the resolver message on a GraphQL HTTP error', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ok: false, status: 400, statusText: 'Bad Request',
    json: async () => ({errors: [{message: 'an output array is missing'}]})})))
  await expect(gql('{ outputPage }')).rejects.toThrow('an output array is missing')
})

it('shares an identical request in flight, aborting it only once every caller has', async () => {
  const requests: {signal: AbortSignal; finish: () => void}[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {signal: AbortSignal}) => {
    await new Promise<void>((finish) => requests.push({signal: init.signal, finish}))
    return {ok: true, json: async () => ({data: {n: requests.length}})}
  }))
  const first = new AbortController()
  const second = new AbortController()
  const a = gql('{ n }', {}, first.signal)
  const b = gql('{ n }', {}, second.signal)
  expect(requests).toHaveLength(1)
  first.abort()
  await expect(a).rejects.toBeDefined()
  expect(requests[0].signal.aborted).toBe(false)
  requests[0].finish()
  await expect(b).resolves.toEqual({n: 1})
  const c = new AbortController()
  const d = new AbortController()
  const later = [gql('{ n }', {}, c.signal), gql('{ n }', {}, d.signal)]
  expect(requests).toHaveLength(2)
  c.abort()
  d.abort()
  await Promise.allSettled(later)
  expect(requests[1].signal.aborted).toBe(true)
  const fresh = gql('{ n }')
  expect(requests).toHaveLength(3)
  requests[2].finish()
  await expect(fresh).resolves.toEqual({n: 3})
})

it('aborts old pages and ignores their late responses', async () => {
  const requests: {signal: AbortSignal; resolve: (value: string) => void}[] = []
  function Page({ id }: {id: string}) {
    const query = useQuery(id, (signal) => new Promise<string>((resolve) => requests.push({signal, resolve})))
    return <div>{query.value ?? 'loading'}</div>
  }
  await act(async () => root.render(<Page id="first" />))
  await act(async () => root.render(<Page id="second" />))
  expect(requests[0].signal.aborted).toBe(true)
  await act(async () => requests[1].resolve('second page'))
  await act(async () => requests[0].resolve('stale first page'))
  expect(host.textContent).toBe('second page')
})

it('keeps the previous result on a failed refresh and can retry', async () => {
  let fail = false
  let refresh: () => void = () => {}
  function Page() {
    const query = useQuery('page', async () => { if (fail) throw new Error('offline'); return 'rows' })
    refresh = query.reload
    return <div>{query.value}{query.error ? ' offline' : ''}</div>
  }
  await act(async () => root.render(<Page />))
  fail = true
  await act(async () => refresh())
  expect(host.textContent).toBe('rows offline')
  fail = false
  await act(async () => refresh())
  expect(host.textContent).toBe('rows')
})

it('keeps pagination usable during continuous slow refreshes and cancels on page changes', async () => {
  const { PagedTable } = await import('../src/components/PagedTable')
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf, base64Cbor } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps.find(step => step.id === 'roll')!
  const requests: {offset: number; signal: AbortSignal}[] = []
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn(async (_url, init) => {
    const {variables} = JSON.parse(init.body)
    requests.push({offset: variables.offset, signal: init.signal})
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(resolve, 2000)
      init.signal.addEventListener('abort', () => {
        clearTimeout(timer)
        reject(new DOMException('Aborted', 'AbortError'))
      })
    })
    return {ok: true, json: async () => ({data: {outputPage: {
      type: {dims: [['sample', 'int']], schema: 'int'}, total: 75, offset: variables.offset,
      hasMore: variables.offset < 50,
      rows: Array.from({length: 25}, (_, i) => ({key: [variables.offset + i], value: base64Cbor(i)})),
    }}})}
  }))
  const render = (version: string) => root.render(<PagedTable table={table} source={table.output!} version={version} />)
  const next = () => host.querySelector<HTMLButtonElement>('[aria-label="Next page"]')!
  try {
    await act(async () => render('0'))
    expect(next().disabled).toBe(true)
    for (let revision = 1; revision <= 4; revision++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
      await act(async () => render(String(revision)))
      if (revision >= 2) expect(next().disabled).toBe(false)
    }
    expect(host.querySelector('[aria-label="Loading page"]')).not.toBeNull()
    const previous = requests.at(-1)!
    await act(async () => next().click())
    expect(previous.signal.aborted).toBe(true)
    expect(requests.at(-1)!.offset).toBe(25)
    expect(next().disabled).toBe(true)
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(next().disabled).toBe(false)
    expect(paginationText()).toContain('26–50')
  } finally { vi.useRealTimers() }
})

it('batches type labels without fetching any entity bodies', async () => {
  const fetch = vi.fn(async (_url: string, init: {body: string}) => {
    const {query, variables} = JSON.parse(init.body)
    expect(query).toContain('entityMetas')
    expect(variables.cids).toHaveLength(100)
    return {ok: true, json: async () => ({data: {entityMetas: variables.cids.map((cid: string) => ({cid, typeTag: 'Example'}))}})}
  })
  vi.stubGlobal('fetch', fetch)
  function Label({cid}: {cid: string}) {
    return <span>{useEntityMetadata(cid).value?.typeTag}</span>
  }
  await act(async () => root.render(<>{Array.from({length: 100}, (_, i) => <Label key={i} cid={`metadata-${i}`} />)}</>))
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 10)) })
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(host.textContent).toBe('Example'.repeat(100))
})

it('pages 25 current rows and refreshes the active page when the run changes', async () => {
  const { PagedTable } = await import('../src/components/PagedTable')
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf, base64Cbor } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps.find((step) => step.id === 'roll')!
  let total = 60
  const requests: {offset: number; first: number}[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {query, variables} = JSON.parse(init.body)
    expect(query).not.toContain('snapshot')
    requests.push(variables)
    const rows = Array.from({length: Math.min(variables.first, total - variables.offset)}, (_, i) => ({
      key: [`key-${variables.offset + i}`], value: base64Cbor(`value-${variables.offset + i}`),
    }))
    return {ok: true, json: async () => ({data: {outputPage: {type: table.type, rows, total, offset: variables.offset, hasMore: variables.offset + rows.length < total}}})}
  }))
  await act(async () => root.render(<PagedTable table={table} source={table.output!} version="one" />))
  expect(host.querySelectorAll('tbody tr')).toHaveLength(25)
  expect(paginationText()).toBe('1–25 / 60')
  expect(host.textContent).not.toContain('refresh snapshot')
  await act(async () => (host.querySelector('[aria-label="Next page"]') as HTMLButtonElement).click())
  expect(paginationText()).toBe('26–50 / 60')
  expect(host.textContent).toContain('value-25')
  total = 70
  await act(async () => root.render(<PagedTable table={table} source={table.output!} version="two" />))
  expect(paginationText()).toBe('26–50 / 70')
  expect(requests.map(({offset, first}) => [offset, first])).toEqual([[0, 25], [25, 25], [25, 25]])
})

it('keeps the displayed output page and range while the next page is pending', async () => {
  const { PagedTable } = await import('../src/components/PagedTable')
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf, base64Cbor } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps[0]
  let finishPage: () => void = () => {}
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {variables} = JSON.parse(init.body)
    if (variables.offset === 25) await new Promise<void>((resolve) => { finishPage = resolve })
    const rows = Array.from({length: variables.offset ? 1 : 25}, (_, i) => ({
      key: [`key-${variables.offset + i}`], value: base64Cbor(`value-${variables.offset + i}`),
    }))
    return {ok: true, json: async () => ({data: {outputPage: {
      type: table.type, rows, total: 26, offset: variables.offset, hasMore: variables.offset === 0,
    }}})}
  }))
  await act(async () => root.render(<PagedTable table={table} source={table.output!} />))
  const next = host.querySelector('[aria-label="Next page"]') as HTMLButtonElement
  await act(async () => next.click())
  expect(host.querySelectorAll('tbody tr')).toHaveLength(25)
  expect(host.textContent).toContain('value-0')
  expect(paginationText()).toBe('1–25 / 26')
  expect(host.querySelector('[role="status"][aria-label="Loading page"]')).not.toBeNull()
  expect(next.disabled).toBe(true)
  // The initial filter debounce must not reset a page selected immediately after mounting.
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 275)) })
  expect(host.querySelector('[aria-label="Loading page"]')).not.toBeNull()
  await act(async () => finishPage())
  expect(host.querySelectorAll('tbody tr')).toHaveLength(1)
  expect(host.textContent).toContain('value-25')
  expect(paginationText()).toBe('26–26 / 26')
  expect(host.querySelector('[aria-label="Loading page"]')).toBeNull()
})

it('retains a previous page only within the same query scope', async () => {
  const pending: ((value: string) => void)[] = []
  function Page({source, page}: {source: string; page: number}) {
    const query = useQuery(`${source}:${page}`, () => new Promise<string>((resolve) => pending.push(resolve)), {retainKey: source})
    return <div>{query.value ?? 'empty'}{query.loading ? ' loading' : ''}</div>
  }
  await act(async () => root.render(<Page source="one" page={0} />))
  await act(async () => pending[0]('first page'))
  await act(async () => root.render(<Page source="one" page={1} />))
  expect(host.textContent).toBe('first page loading')
  await act(async () => root.render(<Page source="two" page={0} />))
  expect(host.textContent).toBe('empty loading')
  await act(async () => pending[1]('late previous source'))
  expect(host.textContent).toBe('empty loading')
  await act(async () => pending[2]('new source'))
  expect(host.textContent).toBe('new source')
})

it('retains rows during sorting, ignores stale sorts, and swaps pages without fading', async () => {
  const { PagedTable } = await import('../src/components/PagedTable')
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf, base64Cbor } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps[0]
  const pending: {signal: AbortSignal; finish: () => void}[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string; signal: AbortSignal}) => {
    const {variables} = JSON.parse(init.body)
    if (variables.sort) await new Promise<void>((finish) => pending.push({signal: init.signal, finish}))
    const prefix = variables.sort?.direction ?? 'original'
    const rows = Array.from({length: 25}, (_, i) => ({key: [`${prefix}-${i}`], value: base64Cbor(`${prefix}-${i}`)}))
    return {ok: true, json: async () => ({data: {outputPage: {type: table.type, rows, total: 50, offset: 0, hasMore: true}}})}
  }))
  await act(async () => root.render(<PagedTable table={table} source={table.output!} />))
  const grid = host.querySelector('.paged-table')!
  const row = host.querySelector('tbody tr')!
  const sort = host.querySelector('thead button') as HTMLButtonElement
  await act(async () => sort.click())
  expect(host.querySelector('tbody tr')).toBe(row)
  expect(paginationText()).toBe('1–25 / 50')
  expect(grid.getAttribute('aria-busy')).toBe('true')
  expect(host.querySelector('[aria-label="Loading page"]')).not.toBeNull()
  await act(async () => sort.click())
  expect(pending[0].signal.aborted).toBe(true)
  await act(async () => pending[1].finish())
  expect(host.textContent).toContain('desc-0')
  expect(host.querySelector('tbody .arrive')).toBeNull()
  expect(host.querySelector('.paged-table')).toBe(grid)
  expect(grid.getAttribute('aria-busy')).toBe('false')
  await act(async () => pending[0].finish())
  expect(host.textContent).not.toContain('asc-0')
  expect(host.textContent).toContain('desc-0')
  await act(async () => root.render(<PagedTable table={table} source={{cid: 'another-output'}} />))
  expect(host.textContent).not.toContain('desc-0')
})

it('keeps rows on screen through a new slice, whose first page it reads first', async () => {
  const { PagedTable } = await import('../src/components/PagedTable')
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf, base64Cbor } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps[0]
  const requests: {selection: unknown; offset: number}[] = []
  const pending: (() => void)[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {variables} = JSON.parse(init.body)
    const seed = variables.source.selection?.seed ?? 'all'
    requests.push({selection: variables.source.selection ?? null, offset: variables.offset})
    if (seed === 'b') await new Promise<void>((finish) => pending.push(finish))
    const rows = Array.from({length: 25}, (_, i) => ({key: [`${seed}-${variables.offset + i}`], value: base64Cbor(`${seed}-${variables.offset + i}`)}))
    return {ok: true, json: async () => ({data: {outputPage: {type: table.type, rows, total: 50, offset: variables.offset, hasMore: variables.offset === 0}}})}
  }))
  const sliced = (seed: string) => ({...table.output!, selection: {seed}})
  await act(async () => root.render(<PagedTable table={table} source={sliced('a')} />))
  await act(async () => (host.querySelector('[aria-label="Next page"]') as HTMLButtonElement).click())
  expect(host.textContent).toContain('a-25')
  const grid = host.querySelector('.paged-table')!
  await act(async () => root.render(<PagedTable table={table} source={sliced('b')} />))
  expect(requests.at(-1)).toEqual({selection: {seed: 'b'}, offset: 0})
  expect(host.querySelector('.paged-table')).toBe(grid)
  expect(host.textContent).toContain('a-25')
  expect(host.textContent).not.toContain('loading rows')
  await act(async () => pending[0]())
  expect(host.textContent).toContain('b-0')
  expect(requests.filter((request) => (request.selection as {seed?: string} | null)?.seed === 'b')).toHaveLength(1)
})

it('keeps the filter and displayed range through filtering and clearing a single match', async () => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  const { PagedTable } = await import('../src/components/PagedTable')
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf, base64Cbor } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps[0]
  let delay = false
  let finish: () => void = () => {}
  const fetch = vi.fn(async (_url: string, init: {body: string}) => {
    const {variables} = JSON.parse(init.body)
    if (delay) await new Promise<void>((resolve) => { finish = resolve })
    const needle = variables.filters.find((filter: {text: string}) => filter.text)?.text
    const total = needle === 'missing' ? 0 : needle ? 1 : 26
    const rows = Array.from({length: Math.min(25, total - variables.offset)}, (_, i) => ({
      key: [`key-${variables.offset + i}`], value: base64Cbor(`value-${variables.offset + i}`),
    }))
    return {ok: true, json: async () => ({data: {outputPage: {type: table.type, rows, total, offset: variables.offset, hasMore: variables.offset + rows.length < total}}})}
  })
  vi.stubGlobal('fetch', fetch)
  await act(async () => root.render(<PagedTable table={table} source={table.output!} />))
  await act(async () => (host.querySelector('[aria-label="Next page"]') as HTMLButtonElement).click())
  const grid = host.querySelector('.paged-table')!
  const row = host.querySelector('tbody tr')!
  await act(async () => (host.querySelector('[aria-label="Filter seed"]') as HTMLButtonElement).click())
  const input = document.querySelector<HTMLInputElement>('[placeholder="Filter seed…"]')!
  const filter = async (value: string) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
      input.dispatchEvent(new Event('input', {bubbles: true}))
    })
  }
  const debounce = async () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 275)) })
  delay = true
  await filter('key-0')
  // The old page stays visible, with a spinner, even during the debounce.
  expect(grid.getAttribute('aria-busy')).toBe('true')
  expect(paginationText()).toBe('26–26 / 26')
  await debounce()
  expect(host.querySelector('tbody tr')).toBe(row)
  expect(paginationText()).toBe('26–26 / 26')
  expect(JSON.parse(fetch.mock.calls.at(-1)![1].body).variables.offset).toBe(0)
  await act(async () => finish())
  expect(host.querySelector('.paged-table')).toBe(grid)
  expect(paginationText()).toBe('1–1 / 1')
  expect(host.querySelector('tbody .arrive')).toBeNull()
  expect(document.querySelector('[placeholder="Filter seed…"]')).toBe(input)
  await filter('')
  await debounce()
  // A retained filtered match keeps the table, and its filter input mounted.
  expect(host.querySelector('.paged-table')).toBe(grid)
  expect(document.querySelector('[placeholder="Filter seed…"]')).toBe(input)
  expect(paginationText()).toBe('1–1 / 1')
  await act(async () => finish())
  expect(paginationText()).toBe('1–25 / 26')
  await filter('missing')
  await debounce()
  await act(async () => finish())
  expect(host.textContent).toContain('no rows')
  expect(grid.getAttribute('aria-busy')).toBe('false')
})

it('reads a thousand key values at once, draws those in view, and says when there are more', async () => {
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps.find((step) => step.id === 'roll')!
  const requests: Record<string, unknown>[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {variables} = JSON.parse(init.body)
    requests.push(variables)
    const items = Array.from({length: variables.first}, (_, i) => ({value: `key-${i}`, compatible: true, counts: {completed: 1}}))
    return {ok: true, json: async () => ({data: {keyPage: {items, total: 1500, next: variables.first < 1500 ? 'more' : null}}})}
  }))
  await renderKeys(table)
  // The header's value count reads one value; the opened list reads the first thousand.
  expect(requests.map(({first}) => first)).toEqual([1, 1000])
  expect(requests.every((request) => !('offset' in request))).toBe(true)
  const keys = host.querySelector('[aria-label="seed keys"]')!
  const list = keys.querySelector('[aria-busy]') as HTMLElement
  const drawn = () => Array.from(keys.querySelectorAll('[aria-pressed]'), (button) => button.textContent)
  expect(list.style.height).toBe('270px')
  expect(drawn()).toEqual(Array.from({length: 15}, (_, i) => `key-${i}`))
  expect(keys.querySelector('.pagination')).toBeNull()
  await act(async () => {
    list.scrollTop = 500 * 27
    list.dispatchEvent(new Event('scroll'))
  })
  expect(drawn()[0]).toBe('key-495')
  expect(drawn().at(-1)).toBe('key-514')
  expect(keys.querySelector('[aria-pressed]')!.parentElement!.style.paddingTop).toBe(`${495 * 27}px`)
  expect(list.lastElementChild!.textContent).toBe('more not shown')
})

it('preserves other dimension columns, search and scroll while compatibility refreshes', async () => {
  const { buildLevel } = await import('../src/model/level')
  const { inProgress, overviewOf } = await import('./fixtures')
  const fixture = inProgress()
  fixture.nodes[1].params.map_over = ['seed', 'model']
  fixture.nodes[1].type.dims = [['seed', 'str'], ['model', 'str']]
  const table = buildLevel('job', overviewOf(fixture)).steps.find((step) => step.id === 'roll')!
  const pending: {seed: string; signal: AbortSignal; resolve: () => void}[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string; signal: AbortSignal}) => {
    const {variables} = JSON.parse(init.body)
    const {dim, selection} = variables
    if (dim === 'model' && selection.seed) {
      await new Promise<void>((resolve) => pending.push({seed: selection.seed, signal: init.signal, resolve}))
    }
    const items = dim === 'seed' ? ['a', 'b'].map((value) => ({value, compatible: true, counts: {completed: 1}}))
      : Array.from({length: 26}, (_, i) => ({value: `m-${i}`, compatible: selection.seed !== 'a', counts: {completed: 1}}))
    return {ok: true, json: async () => ({data: {keyPage: {items, total: items.length, next: null}}})}
  }))
  await renderKeys(table)
  const column = host.querySelector('[aria-label="model keys"]')!
  const input = column.querySelector('input')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, 'm-')
    input.dispatchEvent(new Event('input', {bubbles: true}))
  })
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 275)) })
  const list = column.querySelector('[aria-busy]') as HTMLElement
  list.scrollTop = 54
  const height = list.style.height
  const choose = (value: string) => Array.from(host.querySelectorAll<HTMLButtonElement>('button[aria-pressed]')).find((button) => button.textContent === value)!
  await act(async () => choose('a').click())
  expect(column.querySelector('input')).toBe(input)
  expect(input.value).toBe('m-')
  expect(list.scrollTop).toBe(54)
  expect(list.style.height).toBe(height)
  expect(column.querySelector('[aria-label="Loading model keys"]')).not.toBeNull()
  expect(choose('m-3').getAttribute('aria-disabled')).toBe('true')
  expect(host.textContent).not.toContain('clear a')
  // A rapid second choice cancels the stale compatibility response without remounting columns.
  await act(async () => choose('b').click())
  expect(pending[0].signal.aborted).toBe(true)
  await act(async () => pending.find((request) => request.seed === 'b')!.resolve())
  await act(async () => pending.find((request) => request.seed === 'a')!.resolve())
  expect(choose('m-3').getAttribute('aria-disabled')).toBe('false')
  expect(column.querySelector('input')).toBe(input)
  // Selected rows still toggle off. A picked dim's column is headed by a clear cell in the search
  // box's place, outside the scrolling values; clearing brings the search back as it was.
  await act(async () => choose('m-3').click())
  expect(choose('m-3').getAttribute('aria-pressed')).toBe('true')
  await act(async () => choose('m-3').click())
  expect(choose('m-3').getAttribute('aria-pressed')).toBe('false')
  await act(async () => choose('m-3').click())
  const clear = host.querySelector('[aria-label="Clear model slice"]') as HTMLButtonElement
  expect(clear).not.toBeNull()
  expect(column.contains(clear)).toBe(true)
  expect(list.contains(clear)).toBe(false)
  expect(column.querySelector('[aria-label="Search model"]')).toBeNull()
  expect(list.style.height).toBe(height)
  await act(async () => clear.click())
  expect(host.querySelector('[aria-label="Clear model slice"]')).toBeNull()
  expect(column.querySelector<HTMLInputElement>('[aria-label="Search model"]')!.value).toBe('m-')
})

it('closed key column headers read only their counts, and ignore picks made elsewhere', async () => {
  const { CardTable } = await import('../src/components/CardTable')
  const { LevelStateContext } = await import('../src/lib/levelState')
  const { buildLevel } = await import('../src/model/level')
  const { levelState, overviewOf, recordingOpener } = await import('./fixtures')
  const { OpenEntityContext } = await import('../src/lib/openEntity')
  const table = buildLevel('job', overviewOf()).steps.find((step) => step.id === 'roll')!
  const reads: {dim: string; selection: object}[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {variables} = JSON.parse(init.body)
    reads.push({dim: variables.dim, selection: variables.selection})
    return {ok: true, json: async () => ({data: {keyPage: {items: [{value: 'a', compatible: true, counts: {completed: 1}}], total: 1, offset: 0, next: null}}})}
  }))
  withEmptyOutput(table)
  const { opener } = recordingOpener()
  const render = (slice: Record<string, string>) => root.render(<LevelStateContext.Provider value={levelState({ slice })}>
    <OpenEntityContext.Provider value={opener}><CardTable step={table} matching={null} /></OpenEntityContext.Provider>
  </LevelStateContext.Provider>)
  await act(async () => render({}))
  expect(reads).toEqual([{dim: 'seed', selection: {}}])
  expect(host.textContent).toContain('seed1')
  await act(async () => render({model: 'm'}))
  expect(reads).toHaveLength(1)
  // Hovering does not open the values; clicking the header does, and it, Escape, or a click outside
  // closes them.
  await act(async () => host.querySelector('div')!.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})))
  expect(reads).toHaveLength(1)
  const toggle = host.querySelector<HTMLElement>('button[aria-label="Show seed values"]')!
  const isOpen = () => toggle.getAttribute('aria-expanded') === 'true'
    && host.querySelector('[aria-label="seed keys"]')?.closest('[hidden]') === null
  await act(async () => toggle.click())
  expect(reads).toHaveLength(2)
  expect(isOpen()).toBe(true)
  await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})))
  expect(isOpen()).toBe(false)
  await act(async () => toggle.click())
  await act(async () => host.querySelector('[aria-label="seed keys"]')!.dispatchEvent(new Event('pointerdown', {bubbles: true})))
  expect(isOpen()).toBe(true)
  await act(async () => document.body.dispatchEvent(new Event('pointerdown', {bubbles: true})))
  expect(isOpen()).toBe(false)
  await act(async () => toggle.click())
  await act(async () => toggle.click())
  expect(isOpen()).toBe(false)
})

it('keeps incompatible dimension values visible and prevents selecting them', async () => {
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps.find((step) => step.id === 'roll')!
  vi.stubGlobal('fetch', vi.fn(async () => ({ok: true, json: async () => ({data: {keyPage: {
    items: [{value: 'available', compatible: true, counts: {completed: 1}}, {value: 'unavailable', compatible: false, counts: {queued: 1}}], total: 2, offset: 0, next: null,
  }}})})))
  const onSlice = vi.fn()
  await renderKeys(table, onSlice)
  const [available, unavailable] = host.querySelectorAll<HTMLButtonElement>('[aria-pressed]')
  expect(unavailable.textContent).toContain('unavailable')
  expect(unavailable.getAttribute('aria-disabled')).toBe('true')
  await act(async () => unavailable.click())
  expect(onSlice).not.toHaveBeenCalled()
  await act(async () => available.click())
  expect(onSlice).toHaveBeenCalledWith({seed: 'available'})
})

it('snaps 1-based row entries to page boundaries and clamps both ends', async () => {
  const changes = vi.fn()
  function Controls() {
    const [offset, setOffset] = useState(25)
    return <Pagination offset={offset} count={Math.min(25, 63 - offset)} total={63} hasMore={offset < 50} loading={false}
      onPage={(next) => { changes(next); setOffset(next) }} onLast={() => setOffset(50)} />
  }
  await act(async () => root.render(<Controls />))
  for (const [entry, expected] of [['25', '1–25 / 63'], ['26', '26–50 / 63'], ['38', '26–50 / 63'],
    ['50', '26–50 / 63'], ['51', '51–63 / 63'], ['9,999', '51–63 / 63'], ['-12', '1–25 / 63'], ['0', '1–25 / 63']]) {
    await enterRow(entry)
    expect(paginationText()).toBe(expected)
  }
  expect(changes.mock.calls.flat()).toEqual([0, 25, 50, 0])
  for (const invalid of ['', 'abc', '1.5']) {
    await enterRow(invalid)
    expect(paginationText()).toBe('1–25 / 63')
  }
  expect(changes).toHaveBeenCalledTimes(4)
  expect(rowOffset(999, 50, 25)).toBe(25) // An exact multiple ends on its existing last page.
  expect(rowOffset(1, 0, 25)).toBe(0)
  expect(rowOffset(Infinity, null, 25)).toBe(2_147_483_625)
})

it('disables jump controls while loading or empty, and cancels draft input with Escape', async () => {
  const onPage = vi.fn()
  await act(async () => root.render(<Pagination offset={0} count={25} total={50} hasMore loading
    onPage={onPage} onLast={() => {}} />))
  expect(Array.from(host.querySelectorAll('button, input')).every((element) => (element as HTMLButtonElement).disabled)).toBe(true)
  await act(async () => root.render(<Pagination offset={0} count={0} total={0} hasMore={false} loading={false}
    onPage={onPage} onLast={() => {}} />))
  expect(Array.from(host.querySelectorAll('button, input')).every((element) => (element as HTMLButtonElement).disabled)).toBe(true)
  await act(async () => root.render(<Pagination offset={0} count={25} total={50} hasMore loading={false}
    onPage={onPage} onLast={() => {}} />))
  const input = host.querySelector('input')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '38')
    input.dispatchEvent(new Event('input', {bubbles: true}))
  })
  await act(async () => input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})))
  expect(input.value).toBe('1')
  expect(onPage).not.toHaveBeenCalled()
})

it('jumps straight to output end with an unknown total and uses the returned offset', async () => {
  const { PagedTable } = await import('../src/components/PagedTable')
  const { buildLevel } = await import('../src/model/level')
  const { overviewOf, base64Cbor } = await import('./fixtures')
  const table = buildLevel('job', overviewOf()).steps[0]
  const requests: {offset: number; last: boolean}[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {variables} = JSON.parse(init.body)
    requests.push(variables)
    const offset = variables.last ? 50 : Math.min(50, variables.offset)
    const rows = Array.from({length: Math.min(25, 63 - offset)}, (_, i) => ({key: [offset + i], value: base64Cbor(`row-${offset + i}`)}))
    return {ok: true, json: async () => ({data: {outputPage: {type: table.type, rows, offset, total: offset === 0 ? null : 63, hasMore: offset < 50}}})}
  }))
  await act(async () => root.render(<PagedTable table={table} source={table.output!} />))
  expect(paginationText()).toBe('1–25')
  await act(async () => (host.querySelector('[aria-label="Last page"]') as HTMLButtonElement).click())
  expect(paginationText()).toBe('51–63 / 63')
  expect(host.textContent).toContain('row-50')
  expect(requests.map(({offset, last}) => [offset, last])).toEqual([[0, false], [0, true]])
  await act(async () => (host.querySelector('[aria-label="First page"]') as HTMLButtonElement).click())
  await enterRow('999')
  expect(paginationText()).toBe('51–63 / 63')
  expect(requests[3].offset).toBe(975) // Unknown bounds are clamped by the server.
  await enterRow('38')
  expect(paginationText()).toBe('26–50 / 63')
  await act(async () => (host.querySelector('[aria-label="Previous page"]') as HTMLButtonElement).click())
  expect(paginationText()).toBe('1–25')
})

it('coalesces refreshes without aborting slow reads and publishes each completed result', async () => {
  const requests: {signal: AbortSignal; resolve: (value: string) => void}[] = []
  let refresh = () => {}
  function Page({version}: {version: string}) {
    const query = useQuery('stable-source', signal => new Promise<string>(resolve => requests.push({signal, resolve})), {version})
    refresh = query.reload
    return <div>{query.value ?? 'loading'}</div>
  }
  await act(async () => root.render(<Page version="initial" />))
  for (let version = 0; version < 5; version++) {
    await act(async () => root.render(<Page version={String(version)} />))
    await act(async () => refresh())
  }
  expect(requests).toHaveLength(1)
  expect(requests[0].signal.aborted).toBe(false)
  await act(async () => requests[0].resolve('first complete result'))
  expect(host.textContent).toBe('first complete result')
  expect(requests).toHaveLength(2)
  await act(async () => requests[1].resolve('fresh result'))
  expect(host.textContent).toBe('fresh result')
  expect(requests).toHaveLength(2)
  await act(async () => root.render(null))
  expect(requests.every(request => request.signal.aborted)).toBe(true)
})

it('slices on every dim of the level from a floating panel, letting dimmed values be chosen', async () => {
  const { SliceDims } = await import('../src/components/SliceDims')
  const { LevelStateContext } = await import('../src/lib/levelState')
  const { buildLevel } = await import('../src/model/level')
  const { inProgress, node, overviewOf } = await import('./fixtures')
  const fixture = inProgress()
  fixture.nodes.push(node('sample', 'run_step', {
    params: { step_name: 'sample', map_over: ['seed'], replicas: {} },
    inputs: [{ label: 'seed', identity: 'in' }],
    type: { dims: [['seed', 'str'], ['sample', 'int']], schema: 'str' },
  }))
  const steps = buildLevel('job', overviewOf(fixture)).steps
  const reads: {query: string; variables: Record<string, unknown>}[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: {body: string}) => {
    const {query, variables} = JSON.parse(init.body)
    reads.push({query, variables})
    if (query.includes('outputDimValues'))
      return {ok: true, json: async () => ({data: {outputDimValues: {
        items: [{value: 0, compatible: true}, {value: 1, compatible: false}], total: 2, offset: 0}}})}
    return {ok: true, json: async () => ({data: {keyPage: {
      items: [{value: 'a', compatible: true, counts: {completed: 1}}], total: 1, offset: 0, next: null}}})}
  }))
  const picks: Record<string, string | number>[] = []
  function Slicer() {
    const [slice, setSlice] = useState<Readonly<Record<string, string | number>>>({ seed: 'a' })
    const state = { slice, levelPath: [],
      setSlice: (next: Readonly<Record<string, string | number>>) => { picks.push({...next}); setSlice(next) } }
    return <LevelStateContext.Provider value={state}><SliceDims steps={steps} /></LevelStateContext.Provider>
  }
  await act(async () => root.render(<Slicer />))
  const button = host.querySelector<HTMLButtonElement>('button[aria-expanded]')!
  expect(button.textContent).toBe('sliced on 1 dim')
  expect(reads).toHaveLength(0)
  await act(async () => button.click())
  const dims = Array.from(host.querySelectorAll<HTMLButtonElement>('[role="dialog"] > div:first-child button[aria-pressed]'))
  expect(dims.map((dim) => dim.textContent)).toEqual(['seeda', 'sample '])
  // The first dim is read from the run it keys.
  expect(reads.at(-1)!.query).toContain('keyPage')
  expect(reads.at(-1)!.variables).toMatchObject({dim: 'seed', nodeId: 'roll', selection: {}})
  // A dim produced inside an invocation is read from its node's output, against the other picks.
  await act(async () => dims[1].click())
  expect(reads.at(-1)!.query).toContain('outputDimValues')
  expect(reads.at(-1)!.variables).toMatchObject({dim: 'sample', source: {nodeId: 'sample', selection: {seed: 'a'}}})
  const values = () => Array.from(host.querySelectorAll<HTMLButtonElement>('[aria-label="sample keys"] button[aria-pressed]'))
  expect(values()[1].getAttribute('aria-disabled')).toBe('false')
  await act(async () => values()[1].click())
  expect(picks.at(-1)).toEqual({seed: 'a', sample: 1})
  expect(button.textContent).toBe('sliced on 2 dims')
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Clear sample slice"]')!.click())
  expect(picks.at(-1)).toEqual({seed: 'a'})
  await act(async () => document.body.dispatchEvent(new Event('pointerdown', {bubbles: true})))
  expect(host.querySelector('[role="dialog"]')).toBeNull()
  // The × beside the button clears the slice in one click, and goes with it.
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="Clear the slice"]')!.click())
  expect(picks.at(-1)).toEqual({})
  expect(button.textContent).toBe('slice dims')
  expect(host.querySelector('[aria-label="Clear the slice"]')).toBeNull()
})
