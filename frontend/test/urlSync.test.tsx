// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
vi.mock('../src/api/live', async (original) => ({
  ...(await original<typeof import('../src/api/live')>()),
  useLivePolling: () => {},
}))
import { InvocationPanel, declareView, renderer } from 'fxtr-view'
import { dispatchRegistry } from 'fxtr-view-shell-util'
import { toast } from 'sonner'
import { App } from '../src/App'
import { parseUrlState } from '../src/lib/urlState'
import { UrlWriter } from '../src/lib/urlWriter'
import { inProgress, node, overviewOf, spawn } from './fixtures'

/** The root: an input of two seeds, a step mapped over them, and a child workflow mapped over them,
 *  whose run at seed `a` has one step, `judge`. */
const root = inProgress({
  state: 'completed',
  nodes: [
    ...inProgress().nodes.slice(0, 2),
    node('inner', 'run_child_workflow', {
      params: { workflow_name: 'inner', map_over: ['seed'], replicas: {} },
      inputs: [{ label: 'seed', identity: 'in' }],
      type: { dims: [['seed', 'str']], schema: 'str' },
    }),
  ],
  spawns: [spawn('roll', ['a'], 'completed', 'cid-a'), { ...spawn('inner', ['a'], 'completed', null), childPathname: 'root/inner[seed:"a"]' }],
})
const child = inProgress({
  pathname: 'root/inner[seed:"a"]',
  workflowName: 'inner',
  state: 'completed',
  nodes: [node('judge', 'run_step', { params: { step_name: 'judge', map_over: [], replicas: {} } })],
  spawns: [spawn('judge', [], 'completed', 'cid-j')],
})

/** The root level served: `root`, unless a test serves another. */
let rootLevel = root
let host: HTMLDivElement
let reactRoot: Root | undefined

async function settle(ms = 400) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

async function render(hash: string, registry = dispatchRegistry([])) {
  window.history.replaceState(null, '', `/${hash}`)
  const created = createRoot(host)
  reactRoot = created
  await act(async () => created.render(<App views={{ registry, problem: null }} />))
  await settle()
}

/** Back or forward as the browser does it: the entry changes, then `popstate`. */
async function traverse(entryState: unknown, hash: string) {
  window.history.replaceState(entryState, '', hash)
  await act(async () => window.dispatchEvent(new PopStateEvent('popstate', { state: entryState })))
  await settle()
}

const shown = () => parseUrlState(window.location.hash).state
/** The detail view's panels on screen, by id (the resizable workspace's panels have ids too). */
const panelIds = () => [...host.querySelectorAll('[data-panel-id^="panel-"]')].map((element) => element.getAttribute('data-panel-id'))

beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  Element.prototype.animate ??= (() => ({ finished: Promise.resolve(), cancel() {}, onfinish: null })) as never
  // jsdom computes no padding, which the columns read the panels' heights against.
  const computed = window.getComputedStyle.bind(window)
  vi.stubGlobal('getComputedStyle', (element: Element) => new Proxy(computed(element), {
    get(style, key) {
      if (key === 'paddingTop' || key === 'paddingBottom') return '0px'
      const value = Reflect.get(style, key)
      return typeof value === 'function' ? value.bind(style) : value
    },
  }))
  vi.stubGlobal('CSS', { escape: (text: string) => text })
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }))
  if (!document.querySelector('link[rel="icon"]')) document.head.append(Object.assign(document.createElement('link'), { rel: 'icon' }))
  sessionStorage.clear()
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init?: RequestInit) => {
      // Source, files, the experiment's readme: none here.
      if (!init?.body) return { ok: false, status: 404, statusText: 'Not Found', json: async () => ({}), text: async () => '' }
      const { query, variables } = JSON.parse(String(init.body))
      if (query.includes('levelOverview')) {
        const level = variables.pathname === 'root' ? rootLevel : variables.pathname === child.pathname ? child : null
        return { ok: true, json: async () => ({ data: { levelOverview: level && overviewOf(level, 'v1') } }) }
      }
      if (query.includes('jobSummar')) {
        const summary = (id: string) => ({ id, workflowName: `wf-${id}`, root: 'root', state: 'succeeded', failed: false, createdAt: '2026-10-06T00:00:00Z' })
        return { ok: true, json: async () => ({ data: query.includes('jobSummary(') ? { jobSummary: summary(variables.id) } : { jobSummaries: { items: [summary('job-a')], next: null } } }) }
      }
      return { ok: true, json: async () => ({ data: null, errors: [{ message: 'not served in this test' }] }) }
    }),
  )
  host = document.createElement('div')
  document.body.append(host)
  rootLevel = root
})

afterEach(async () => {
  await act(async () => toast.dismiss())
  if (reactRoot) await act(async () => reactRoot!.unmount())
  reactRoot = undefined
  host.remove()
  window.history.replaceState(null, '', '/')
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.useRealTimers()
})

it('opens a link at its level inside a mapped workflow, its slice and panels, and keeps it in the URL', async () => {
  const link = '#job=job-a&w=flowchart(/inner,seed=a)&slice=(seed=a)&p=table(/inner/judge,seed=a)&pin=(/roll)'
  window.history.replaceState(null, '', `/${link}`)
  const entries = window.history.length
  await render(link)
  await settle()
  // Opening a link is not a move: no entry, and the link as it was, with where its panels opened.
  expect(window.history.length).toBe(entries)
  expect(window.location.hash).toMatch(new RegExp(`^${link.replace(/[()|.*+?^$[\]{}\\]/g, '\\$&')}&layout=[0-9,|@]+$`))
  expect(shown().workflow?.address).toEqual({ view: 'flowchart', location: { root: 'step', steps: ['inner'], path: [] } })
  expect(shown().workflow?.keys).toEqual({ seed: 'a' })
  expect(shown().slice).toEqual({ seed: 'a' })
  expect(shown().panels.map((panel) => panel.pinned)).toEqual([false, true])
  expect(panelIds()).toHaveLength(2)
  // The flowchart of the run of inner at seed a: its one step.
  expect(host.textContent).toContain('judge')
})

it('applies a link naming no job to the newest, and names it', async () => {
  await render('#p=table(/roll)')
  expect(shown().job).toBe('job-a')
  expect(shown().panels).toHaveLength(1)
  expect(window.history.length).toBeGreaterThan(0)
})

it('closes a browsing panel from a link that names nothing once its level loads, and says why', async () => {
  await render('#job=job-a&p=(/nothing)&p=.output&pin=(/roll)')
  await settle()
  expect(shown().panels.map((panel) => panel.pinned)).toEqual([true])
  expect(panelIds()).toHaveLength(1)
  expect(document.body.textContent).toContain('this link opens nothing')
  expect(document.body.textContent).toContain('the run has no step nothing')
})

it('writes again once a link whose only panel names nothing has closed it', async () => {
  await render('#job=job-a&p=(/nothing)')
  await settle()
  expect(panelIds()).toHaveLength(0)
  expect(window.location.hash).toBe('#job=job-a')
})

describe('a link to a card', () => {
  /** Where the trace was scrolled to, each time. */
  let scrolls: number[]
  beforeEach(() => {
    scrolls = []
    Element.prototype.scrollTo = function (this: Element, options?: ScrollToOptions | number) {
      if (typeof options === 'object') scrolls.push(options.top ?? 0)
    } as never
  })
  const button = (text: string) => [...host.querySelectorAll('button')].find((element) => element.textContent === text)!

  it('scrolls the trace to the card, at the root and inside a mapped workflow, and keeps it in the URL', async () => {
    await render('#job=job-a&card=roll')
    expect(scrolls).toHaveLength(1)
    expect(window.location.hash).toBe('#job=job-a&card=roll')
    await act(async () => reactRoot!.unmount())
    reactRoot = undefined
    scrolls = []
    await render('#job=job-a&w=trace(/inner,seed=a)&card=judge')
    await settle()
    expect(scrolls).toHaveLength(1)
    expect(shown().card).toBe('judge')
    expect(shown().workflow?.address.location).toEqual({ root: 'step', steps: ['inner'], path: [] })
  })

  it('scrolls to the return card, and selects the step returned in the flowchart', async () => {
    // The root returns roll's output: the return card has no table of its own.
    const attempt = root.attempts[0]!
    rootLevel = { ...root, resultCid: 'cid-a', attempts: [{ ...attempt, events: [...attempt.events, { kind: 'return', identity: 'roll' }] }] }
    // A job of its own: the live store keeps the levels other tests read.
    await render('#job=job-r&card=return')
    expect(scrolls).toHaveLength(1)
    expect(shown().card).toBe('return')
    await act(async () => reactRoot!.unmount())
    reactRoot = undefined
    await render('#job=job-r&w=flowchart(/)&card=return')
    await settle()
    expect(host.querySelector('[class*="flowchartCard"]')?.textContent).toContain('roll')
    expect(shown().card).toBe('return')
  })

  it('selects the card in the flowchart', async () => {
    await render('#job=job-a&w=flowchart(/)&card=inner')
    // The selected node's card, under the flowchart.
    expect(host.querySelector('[class*="flowchartCard"]')?.textContent).toContain('inner')
  })

  it('says a card the level does not have is not there, and leaves it out', async () => {
    await render('#job=job-a&card=nothing')
    expect(document.body.textContent).toContain('card=nothing: this level has no step nothing')
    expect(shown().card).toBeNull()
  })

  it('is dropped, and says why, when a renderer shows the level', async () => {
    const overview = declareView(InvocationPanel, 'sweep', 'test.Overview')
    await render('#job=job-a&w=test.Overview(/)&card=roll', dispatchRegistry([renderer(overview, () => null)]))
    expect(document.body.textContent).toContain('card=roll: the level is shown by test.Overview, which has no cards')
    expect(shown().card).toBeNull()
    expect(shown().workflow?.address.view).toBe('test.Overview')
  })

  it('is dropped when another level is shown', async () => {
    await render('#job=job-a&w=trace(/inner,seed=a)&card=judge')
    await settle()
    await act(async () => button('root').click())
    await settle()
    expect(shown().workflow).toBeNull()
    expect(shown().card).toBeNull()
  })
})

it('checks a link opened twice in a row, as quick back and forward do', async () => {
  await render('#job=job-a')
  window.history.replaceState(null, '', '#job=job-a&p=(/nothing)&p=.output&pin=(/roll)')
  await act(async () => {
    window.dispatchEvent(new PopStateEvent('popstate'))
    await vi.advanceTimersByTimeAsync(400)
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
  await settle()
  await settle()
  expect(shown().panels.map((panel) => panel.pinned)).toEqual([true])
  expect(panelIds()).toHaveLength(1)
})

it('applies a link naming no job to the newest, also when come to in an open tab', async () => {
  await render('#job=job-b')
  expect(shown().job).toBe('job-b')
  await traverse(null, '#p=table(/roll)')
  await settle()
  expect(shown().job).toBe('job-a')
  expect(shown().panels).toHaveLength(1)
  expect(panelIds()).toHaveLength(1)
})

it('goes back to another job, with what it showed, and forward again, without new entries', async () => {
  await render('#job=job-a&p=table(/roll)')
  // Another job, opened by a link in this tab: an entry after this one.
  window.history.pushState(null, '', '#job=job-b')
  await act(async () => window.dispatchEvent(new PopStateEvent('popstate')))
  await settle()
  expect(window.location.hash).toBe('#job=job-b')
  const length = window.history.length
  await act(async () => window.history.back())
  await settle()
  expect(window.location.hash).toBe('#job=job-a&p=table(/roll)&layout=0')
  expect(panelIds()).toHaveLength(1)
  await act(async () => window.history.forward())
  await settle()
  expect(window.location.hash).toBe('#job=job-b')
  expect(window.history.length).toBe(length)
})

it('opens a history entry afresh on back, its panels with their ids, without a new entry', async () => {
  await render('#job=job-a&p=table(/roll)')
  const first = { state: window.history.state, hash: window.location.hash, ids: panelIds() }
  expect(first.ids).toHaveLength(1)
  // Another entry: the page as a link opened later in the same tab names it.
  await traverse(null, '#job=job-a&p=table(/roll)&pin=(/inner,seed=b)')
  expect(panelIds()).toHaveLength(2)
  expect(panelIds()).not.toContain(first.ids[0])
  const length = window.history.length
  await traverse(first.state, first.hash)
  expect(panelIds()).toEqual(first.ids)
  expect(window.location.hash).toBe(first.hash)
  expect(window.history.length).toBe(length)
})

describe('the URL writer', () => {
  const at = () => ({ hash: window.location.hash, state: window.history.state as unknown })
  // At the end of the history, whatever the tests before left after it, so a push adds an entry.
  beforeEach(() => window.history.pushState(null, '', '/'))

  it("makes no entries for what a job's view shows: every change replaces the current one", async () => {
    window.history.replaceState(null, '', '/#job=a')
    const writer = new UrlWriter()
    const start = window.history.length
    for (const hash of ['#job=a&slice=(seed=a)', '#job=a&slice=(seed=a)&p=(/roll)', '#job=a&w=flowchart(/inner,seed=a)&p=table(/roll)', '#job=a']) {
      writer.show('a', hash, { hash })
      await settle()
    }
    expect(window.history.length).toBe(start)
    expect(at()).toEqual({ hash: '#job=a', state: { hash: '#job=a' } })
  })

  it('makes one entry per job switched to, whichever of the job and its view says so first', async () => {
    window.history.replaceState(null, '', '/#job=a')
    const writer = new UrlWriter()
    const start = window.history.length
    writer.showJob('b')
    writer.show('b', '#job=b&p=(/roll)', 'b')
    expect(window.history.length).toBe(start + 1)
    expect(at()).toEqual({ hash: '#job=b&p=(/roll)', state: 'b' })
    await settle()
    writer.show('c', '#job=c', 'c')
    writer.showJob('c')
    expect(window.history.length).toBe(start + 2)
    expect(at()).toEqual({ hash: '#job=c', state: 'c' })
  })

  it('writes the URL alone when the browser refuses the snapshot', () => {
    window.history.replaceState(null, '', '/#job=a')
    const writer = new UrlWriter()
    const replace = window.history.replaceState.bind(window.history)
    vi.spyOn(window.history, 'replaceState').mockImplementation((state, unused, url) => {
      if (state !== null) throw new DOMException('too large', 'DataCloneError')
      replace(state, unused, url)
    })
    writer.show('a', '#job=a&p=(/roll)', { huge: true })
    expect(at()).toEqual({ hash: '#job=a&p=(/roll)', state: null })
  })

  it("names the job shown in a link naming none, keeping the rest, without an entry", () => {
    window.history.replaceState('kept', '', '/#p=(/roll)')
    const writer = new UrlWriter()
    const start = window.history.length
    writer.showJob('a')
    expect(window.history.length).toBe(start)
    expect(at()).toEqual({ hash: '#job=a&p=(/roll)', state: 'kept' })
  })

  it("throttles replaces, writes the one waiting before a job's entry is pushed, and drops it on back", async () => {
    window.history.replaceState(null, '', '/#job=a')
    const writer = new UrlWriter()
    writer.show('a', '#job=a&w=flowchart(/)', 1)
    // A column dragged: changes many times a second replace at once, then once when it settles.
    const replace = vi.spyOn(window.history, 'replaceState')
    for (const width of [500, 510, 520, 530]) {
      writer.show('a', `#job=a&w=flowchart(/)&layout=0@${width}`, width)
      await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    }
    expect(replace).toHaveBeenCalledTimes(0)
    await settle()
    expect(replace).toHaveBeenCalledTimes(1)
    expect(at()).toEqual({ hash: '#job=a&w=flowchart(/)&layout=0@530', state: 530 })
    // Another job: the entry left gets what waits first.
    writer.show('a', '#job=a&layout=0@540', 540)
    writer.show('a', '#job=a&layout=0@545', 545)
    writer.showJob('b')
    expect(at()).toEqual({ hash: '#job=b', state: null })
    window.history.back()
    await act(async () => { await vi.advanceTimersByTimeAsync(50) })
    expect(at()).toEqual({ hash: '#job=a&layout=0@545', state: 545 })
    // Back: what waits is of the entry left.
    writer.popped()
    await settle()
    writer.show('a', '#job=a&layout=0@550', 550)
    writer.show('a', '#job=a&layout=0@555', 555)
    writer.popped()
    await settle()
    expect(at().hash).toBe('#job=a&layout=0@550')
  })
})
