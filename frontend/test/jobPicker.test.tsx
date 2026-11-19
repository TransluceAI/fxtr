// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import { JobPicker, PREVIEW_DELAY } from '../src/components/JobPicker'
import * as time from '../src/lib/time'
import type { JobSummary } from '../src/types'
import type { Starred } from '../src/api/jobs'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))

const summary = (id: string, extra: Partial<JobSummary> = {}): JobSummary => ({
  id,
  workflowName: 'sweep',
  root: 'root',
  createdAt: '2026-11-10T00:00:00Z',
  state: 'succeeded',
  failed: false,
  failedSteps: [],
  inputs: [],
  changedInputs: [],
  latest: true,
  ...extra,
})

const JOBS = [
  summary('job-new', { inputs: [{ name: 'model', cid: 'cid-a', single: true, text: 'opus' }] }),
  summary('job-old', { state: 'stopped', failed: true, failedSteps: ['judge'], inputs: [{ name: 'model', cid: 'cid-b', single: true, text: 'haiku' }] }),
  summary('job-side', { workflowName: 'probe' }),
]

const HISTORY = {
  branches: [{ name: 'main', default: true }, { name: 'feature', default: false }],
  commits: [
    { hash: 'c2', subject: 'Try a probe', time: '2026-11-11T00:00:00Z', parents: ['c1'], branch: 'feature' },
    { hash: 'c1', subject: 'Add the sweep', time: '2026-11-10T00:00:00Z', parents: [], branch: 'main' },
  ],
  jobs: [
    { id: 'job-side', workflowName: 'probe', createdAt: '2026-11-11T00:00:00Z', commit: 'c2', uncommitted: true },
    { id: 'job-new', workflowName: 'sweep', createdAt: '2026-11-10T01:00:00Z', commit: 'c1', uncommitted: false, launchedAt: { hash: 'c0ffee1234', subject: 'Other work' } },
    { id: 'job-old', workflowName: 'sweep', createdAt: '2026-11-10T00:00:00Z', commit: 'c1', uncommitted: false },
  ],
  hidden: 2,
  truncated: false,
}

afterEach(() => {
  document.body.innerHTML = ''
  localStorage.clear()
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

/** The picker over a backend that has the commit view and starred jobs (the local viewer), or
 *  neither (`local` false: a hosted one), opened. */
async function openPicker({ local = true, history = HISTORY, getJobs = () => JOBS, initialStars = {}, starStatus = () => 200, summaryError = () => false }: {
  local?: boolean
  history?: typeof HISTORY
  getJobs?: () => JobSummary[]
  initialStars?: Starred
  starStatus?: (method: string) => number
  summaryError?: () => boolean
} = {}) {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const viewport = mockViewport()
  // A window wide enough for the preview.
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: true, media: query, addEventListener: () => {}, removeEventListener: () => {} }))
  let starred = initialStars
  const requests: string[] = []
  const summaryRequests: string[][] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const json = (data: unknown, status = 200) => ({ ok: status < 400, status, statusText: '', json: async () => structuredClone(data), text: async () => '' })
    if (url.startsWith('/api/job_commits')) return local ? json(history) : json(null, 404)
    if (url.startsWith('/api/starred')) {
      if (!local) return json(null, 404)
      const id = url.split('/')[3]
      requests.push(`${init?.method ?? 'GET'} ${url}`)
      const status = starStatus(init?.method ?? 'GET')
      if (status >= 400) return json(null, status)
      if (init?.method === 'PUT') starred = { ...starred, [id]: { name: JSON.parse(String(init.body)).name, starredAt: '2026-11-12T00:00:00Z' } }
      if (init?.method === 'DELETE') starred = Object.fromEntries(Object.entries(starred).filter(([key]) => key !== id))
      return json({ jobs: starred })
    }
    const { query, variables } = JSON.parse(String(init!.body))
    if (query.includes('jobSummariesByIds')) {
      summaryRequests.push(variables.ids)
      if (summaryError()) return json({ errors: [{ message: 'Could not read job summaries' }] })
      return json({ data: { jobSummariesByIds: getJobs().filter((job) => variables.ids.includes(job.id)) } })
    }
    if (query.includes('jobWorkflows')) return json({ data: { jobWorkflows: ['probe', 'sweep'] } })
    if (query.includes('jobSummaries')) {
      return json({ data: { jobSummaries: { items: getJobs().filter((job) => variables.workflow === null || job.workflowName === variables.workflow), next: null } } })
    }
    return json({ errors: [{ message: 'not in this test' }] })
  }))
  const onSelect = vi.fn()
  const host = document.body.appendChild(document.createElement('div'))
  const root = createRoot(host)
  await act(async () => root.render(<JobPicker jobs={[JOBS[0]]} selectedId="job-new" onSelect={onSelect} onOpenStep={vi.fn()} />))
  await act(async () => host.querySelector<HTMLElement>('[aria-label="Choose job"]')!.click())
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)) })
  const row = (id: string) => document.querySelector<HTMLElement>(`[data-jobs] [data-job-id="${id}"]`)!
  return { root, onSelect, requests, summaryRequests, row, viewport }
}

/** jsdom has no layout: initially the first twenty rendered rows are near the viewport. */
function mockViewport() {
  let nearby = (_id: string, index: number) => index < 20
  const observers: Observer[] = []
  class Observer {
    rows: HTMLElement[] = []
    active = true
    queued = false
    constructor(private callback: (entries: { target: HTMLElement; isIntersecting: boolean }[]) => void) {
      observers.push(this)
    }
    observe(row: HTMLElement) {
      this.rows.push(row)
      if (this.queued) return
      this.queued = true
      queueMicrotask(() => this.emit())
    }
    emit() {
      this.queued = false
      if (this.active) this.callback(this.rows.map((target, index) => ({ target, isIntersecting: nearby(target.dataset.jobId!, index) })))
    }
    disconnect() { this.active = false }
  }
  vi.stubGlobal('IntersectionObserver', Observer)
  return (ids: string[]) => act(async () => {
    nearby = (id) => ids.includes(id)
    for (const observer of observers) observer.emit()
  })
}

it('lists jobs under the commits they ran at, with what tells them apart', async () => {
  const { root, row } = await openPicker()
  const text = document.querySelector('[data-jobs]')!.textContent!
  // Newest commit first, under a line naming the branches; no commit names one, or HEAD.
  expect(text.indexOf('Try a probe')).toBeLessThan(text.indexOf('Add the sweep'))
  expect(branchNames()).toEqual(['main', 'feature'])
  expect(text.indexOf('Branches')).toBeLessThan(text.indexOf('Try a probe'))
  expect(text.match(/main/g)).toHaveLength(1)
  expect(text.match(/feature/g)).toHaveLength(1)
  expect(text).not.toContain('HEAD')
  // Rows name no inputs (the preview does), and a failed job names its step.
  expect(row('job-new').textContent).not.toContain('model')
  expect(row('job-old').textContent).not.toContain('model')
  expect(row('job-old').textContent).toContain('Failed in judge')
  expect(row('job-side').textContent).toContain('Uncommitted changes')
  expect(text).toContain('2 jobs are not shown here')
  await act(async () => root.unmount())
})

/** The pointer moved to (x, y), over `target`. */
const pointAt = (target: Element, x: number, y: number) =>
  act(async () => target.dispatchEvent(new MouseEvent('pointermove', { bubbles: true, clientX: x, clientY: y })))
const wait = (ms: number) => act(async () => { await new Promise((resolve) => setTimeout(resolve, ms)) })
const key = (target: Element, name: string) => act(async () => target.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true })))
const searchBox = () => document.querySelector<HTMLInputElement>('input[role="combobox"]')!
const typeInto = (input: HTMLInputElement, text: string) =>
  act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, text)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
const workflowOptions = () => [...document.querySelectorAll<HTMLElement>('[aria-label="Workflows"] [role="option"]')]
const workflowNames = () => workflowOptions().map((each) => each.textContent)
const workflowOption = (name: string) => workflowOptions().find((each) => each.textContent === name)
const branchButtons = () => [...document.querySelectorAll<HTMLElement>('[aria-label="Branches"] button')]
const branchNames = () => branchButtons().map((each) => each.textContent)
const branchButton = (name: string) => branchButtons().find((each) => each.textContent === name)

it('previews the job the pointer rests on, following it from job to job, and opens a job on a click', async () => {
  const { root, onSelect, row } = await openPicker()
  const preview = () => document.querySelector('[role="region"][aria-label="Preview"]')
  // The picker opens with no preview; a pointer passing over a row opens none.
  expect(preview()).toBeNull()
  await pointAt(row('job-new'), 10, 10)
  expect(preview()).toBeNull()
  await wait(PREVIEW_DELAY + 100)
  // The job shown under a commit other than its own says where it launched.
  expect(document.body.textContent).toContain('Launched at c0ffee1 · Other work')
  // A succeeded job's state is a job's, not a step's: the preview goes on to read its workflow.
  expect(document.body.textContent).not.toContain('unknown invocation or step state')
  // Once open, the preview moves with the pointer at once.
  await pointAt(row('job-old'), 10, 40)
  expect(row('job-old').getAttribute('aria-selected')).toBe('true')
  expect(document.body.textContent).not.toContain('Launched at')
  // A commit's line is no row to preview: the preview stays on the job.
  const commitLine = [...document.querySelectorAll('[role="group"]')].find((each) => each.getAttribute('aria-label') === 'Try a probe')!.firstElementChild!.nextElementSibling!
  await pointAt(commitLine, 10, 70)
  expect(row('job-old').getAttribute('aria-selected')).toBe('true')
  expect(commitLine.getAttribute('tabindex')).toBeNull()
  expect(onSelect).not.toHaveBeenCalled()
  await act(async () => row('job-old').click())
  expect(onSelect).toHaveBeenCalledWith('job-old', expect.objectContaining({ id: 'job-old' }))
  await act(async () => root.unmount())
})

/** Type `name` into a starred job's name box, then press `key` in it. */
async function typeName(box: HTMLInputElement, name: string, key: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(box, name)
    box.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await act(async () => box.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })))
  await wait(0)
}

it("asks for a job's name as it is starred, and renames it from the name beside its star", async () => {
  const { root, requests, row } = await openPicker()
  const puts = () => requests.filter((each) => each === 'PUT /api/starred/job-old').length
  await act(async () => row('job-old').querySelector<HTMLElement>('[aria-label="Star job"]')!.click())
  await wait(0)
  expect(puts()).toBe(1)
  // Starring it focuses its name box; Escape leaves it starred without a name.
  const box = row('job-old').querySelector<HTMLInputElement>('[data-rename]')!
  expect(document.activeElement).toBe(box)
  await typeName(box, 'haiku run', 'Escape')
  expect(puts()).toBe(1)
  expect(row('job-old').querySelector('[data-rename]')).toBeNull()
  // With no name, a faint one to click names it.
  await act(async () => row('job-old').querySelector<HTMLElement>('[title="Name this starred job"]')!.click())
  await typeName(row('job-old').querySelector<HTMLInputElement>('[data-rename]')!, 'haiku run', 'Enter')
  expect(puts()).toBe(2)
  expect(row('job-old').querySelector('[title="Rename"]')!.textContent).toBe('haiku run')
  // Escape keeps the name it had, and the picker open.
  await act(async () => row('job-old').querySelector<HTMLElement>('[title="Rename"]')!.click())
  await typeName(row('job-old').querySelector<HTMLInputElement>('[data-rename]')!, 'other', 'Escape')
  expect(puts()).toBe(2)
  expect(row('job-old').querySelector('[title="Rename"]')!.textContent).toBe('haiku run')
  await act(async () => root.unmount())
})

it('lists the starred jobs in a tab of their own, narrowed by the search', async () => {
  const { root, requests, row } = await openPicker()
  const tab = (name: string) => [...document.querySelectorAll<HTMLElement>('[role="tab"]')].find((each) => each.textContent === name)!
  await act(async () => tab('Starred').click())
  expect(document.body.textContent).toContain('No starred jobs')
  await act(async () => tab('By commit').click())
  for (const id of ['job-old', 'job-side']) {
    await act(async () => row(id).querySelector<HTMLElement>('[aria-label="Star job"]')!.click())
    await wait(0)
    await typeName(row(id).querySelector<HTMLInputElement>('[data-rename]')!, '', 'Escape')
  }
  expect(requests).toContain('PUT /api/starred/job-old')
  await act(async () => tab('Starred').click())
  const listed = () => [...document.querySelectorAll('[aria-label="Starred jobs"] [data-job-id]')].map((each) => each.getAttribute('data-job-id'))
  expect(listed().sort()).toEqual(['job-old', 'job-side'])
  // The search narrows them too.
  await act(async () => {
    const search = document.querySelector<HTMLInputElement>('input[placeholder^="Filter jobs"]')!
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'probe')
    search.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await wait(200)
  expect(listed()).toEqual(['job-side'])
  await act(async () => row('job-side').querySelector<HTMLElement>('[aria-label="Remove star"]')!.click())
  await wait(0)
  expect(document.body.textContent).toContain('No jobs match')
  await act(async () => root.unmount())
})

it('narrows the commits to a branch chosen over them, and widens them again', async () => {
  const { root } = await openPicker()
  const listed = () => [...document.querySelectorAll('[data-jobs] [data-job-id]')].map((each) => each.getAttribute('data-job-id'))
  await act(async () => branchButton('feature')!.click())
  expect(listed()).toEqual(['job-side'])
  // The other branches stay to choose.
  expect(branchNames()).toEqual(['main', 'feature', 'Clear'])
  expect(branchButton('feature')!.getAttribute('aria-pressed')).toBe('true')
  // Clicking the chosen branch again, or Clear, shows every branch.
  await act(async () => branchButton('feature')!.click())
  expect(listed()).toEqual(['job-side', 'job-new', 'job-old'])
  expect(branchButton('Clear')).toBeUndefined()
  await act(async () => branchButton('main')!.click())
  expect(listed()).toEqual(['job-new', 'job-old'])
  await act(async () => branchButton('Clear')!.click())
  expect(listed()).toEqual(['job-side', 'job-new', 'job-old'])
  await act(async () => root.unmount())
})

it('narrows the jobs to a workflow chosen in the search box, and widens them again', async () => {
  const { root } = await openPicker()
  const listed = () => [...document.querySelectorAll('[data-jobs] [data-job-id]')].map((each) => each.getAttribute('data-job-id'))
  const search = searchBox()
  // The workflows are listed only once the box is clicked.
  expect(workflowNames()).toEqual([])
  await act(async () => search.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  expect(workflowNames()).toEqual(['probe', 'sweep'])
  // Escape closes the list, not the picker.
  await key(search, 'Escape')
  expect(workflowNames()).toEqual([])
  expect(searchBox()).toBe(search)
  // Typing lists the workflows that match; the arrows and Enter choose one, which clears the box.
  await typeInto(search, 'swe')
  expect(workflowNames()).toEqual(['sweep'])
  await key(search, 'ArrowDown')
  await key(search, 'Enter')
  await wait(200)
  expect(search.value).toBe('')
  expect(listed()).toEqual(['job-new', 'job-old'])
  expect(document.querySelector('[aria-label^="Show every workflow"]')).not.toBeNull()
  // Only the branches with a job listed are named.
  expect(branchNames()).toEqual(['main'])
  // Backspace in the empty box shows every workflow's jobs again.
  await key(search, 'Backspace')
  expect(listed()).toEqual(['job-side', 'job-new', 'job-old'])
  // A workflow is chosen with a click too, and the box's × widens the jobs again.
  await act(async () => search.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  await act(async () => workflowOption('probe')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  expect(listed()).toEqual(['job-side'])
  await act(async () => document.querySelector<HTMLElement>('[aria-label^="Show every workflow"]')!.click())
  expect(listed()).toEqual(['job-side', 'job-new', 'job-old'])
  await act(async () => root.unmount())
})

it('narrows the recent list to a workflow, with no branches to choose, and forgets the branch chosen', async () => {
  const { root } = await openPicker()
  const listed = () => [...document.querySelectorAll('[data-jobs] [data-job-id]')].map((each) => each.getAttribute('data-job-id'))
  const tab = (name: string) => act(async () => [...document.querySelectorAll<HTMLElement>('[role="tab"]')].find((each) => each.textContent === name)!.click())
  await act(async () => branchButton('feature')!.click())
  await tab('Recent')
  await wait(20)
  expect(document.querySelector('[aria-label="Branches"]')).toBeNull()
  await act(async () => searchBox().dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  await act(async () => workflowOption('probe')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  await wait(20)
  expect(listed()).toEqual(['job-side'])
  await tab('By commit')
  expect(branchButton('feature')!.getAttribute('aria-pressed')).toBe('false')
  await act(async () => root.unmount())
})

it('steps from job to job with the arrow keys and j and k, skipping commits, and opens one on Enter', async () => {
  // jsdom lays nothing out, so has nothing to scroll.
  Element.prototype.scrollIntoView = vi.fn()
  const { root, onSelect, row } = await openPicker()
  const search = document.querySelector<HTMLInputElement>('input[placeholder^="Filter jobs"]')!
  // Down from the search box previews and focuses the first job.
  await key(search, 'ArrowDown')
  expect(row('job-side').getAttribute('aria-selected')).toBe('true')
  expect(document.activeElement).toBe(row('job-side'))
  await key(row('job-side'), 'j')
  await key(row('job-new'), 'j')
  expect(row('job-old').getAttribute('aria-selected')).toBe('true')
  await key(row('job-old'), 'k')
  expect(row('job-new').getAttribute('aria-selected')).toBe('true')
  // In the search box, j is typed rather than taken as a step.
  await key(search, 'j')
  expect(row('job-new').getAttribute('aria-selected')).toBe('true')
  await key(row('job-new'), 'Enter')
  expect(onSelect).toHaveBeenCalledWith('job-new', expect.objectContaining({ id: 'job-new' }))
  await act(async () => root.unmount())
})

it('lists jobs newest first, without the view switch or stars, where the backend has neither', async () => {
  const { root, row } = await openPicker({ local: false })
  expect(document.querySelector('[role="tablist"]')).toBeNull()
  expect(document.querySelector('[aria-label="Jobs, newest first"]')).not.toBeNull()
  expect(document.querySelector('[aria-label="Star job"]')).toBeNull()
  expect(row('job-old').textContent).toContain('Failed in judge')
  await act(async () => root.unmount())
})

it('uses fresh summaries in each tab even when another tab retains an older page', async () => {
  let jobs = JOBS.map((job) => ({ ...job }))
  const { root, row, onSelect } = await openPicker({ getJobs: () => jobs })
  const toggle = () => act(async () => document.querySelector<HTMLElement>('[aria-label="Choose job"]')!.click())
  const tab = (name: string) => act(async () => [...document.querySelectorAll<HTMLElement>('[role="tab"]')].find((each) => each.textContent === name)!.click())
  // Keep a succeeded summary in Recent, then refresh the commit view after the job fails.
  await tab('Recent')
  await tab('By commit')
  await toggle()
  jobs = jobs.map((job) => job.id === 'job-new' ? { ...job, state: 'stopped', failed: true, failedSteps: ['judge'] } : job)
  await toggle()
  expect(row('job-new').textContent).toContain('Failed in judge')
  await key(row('job-new'), ' ')
  expect(document.querySelector('[aria-label="Preview"]')!.textContent).toContain('Failed in judge')
  // In the other direction, Recent's own fresh read wins over a retained failed summary.
  jobs = jobs.map((job) => job.id === 'job-new' ? { ...job, state: 'succeeded', failed: false, failedSteps: [] } : job)
  await tab('Recent')
  expect(row('job-new').textContent).not.toContain('Failed')
  expect(document.querySelector('[aria-label="Preview"]')!.textContent).not.toContain('Failed in judge')
  await act(async () => row('job-new').click())
  expect(onSelect).toHaveBeenCalledWith('job-new', expect.objectContaining({ state: 'succeeded', failed: false }))
  await act(async () => root.unmount())
})

it('connects matching commits through commits hidden by a workflow or search filter', async () => {
  const history = {
    ...HISTORY,
    commits: ['c', 'b', 'a'].map((hash, index) => ({ hash, subject: hash, time: HISTORY.commits[0].time, parents: index < 2 ? ['b', 'a'].slice(index, index + 1) : [], branch: 'main' })),
    jobs: HISTORY.jobs.map((job, index) => ({ ...job, commit: ['c', 'b', 'a'][index], workflowName: index === 1 ? 'probe' : 'sweep' })),
  }
  const { root } = await openPicker({ history })
  const paths = () => document.querySelectorAll('[aria-label="Jobs by commit"] > [role="group"] > svg path').length
  await act(async () => searchBox().dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  await act(async () => workflowOption('sweep')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  expect(document.querySelectorAll('[data-jobs] [data-job-id]')).toHaveLength(2)
  expect(paths()).toBe(2) // One outgoing and one incoming path, joining c to a.
  await key(searchBox(), 'Backspace')
  await typeInto(searchBox(), 'sweep')
  await wait(200)
  expect(document.querySelectorAll('[data-jobs] [data-job-id]')).toHaveLength(2)
  expect(paths()).toBe(2)
  await act(async () => root.unmount())
})

it('rerenders only the changed highlights when moving a preview in a large list', async () => {
  Element.prototype.scrollIntoView = vi.fn()
  const jobs = Array.from({ length: 2000 }, (_, index) => summary(`large-${index}`, {
    createdAt: new Date(Date.UTC(2026, 9, 1, 0, 0, index)).toISOString(),
  }))
  const history = {
    ...HISTORY,
    commits: [HISTORY.commits[1]],
    jobs: jobs.map((job) => ({ ...job, commit: 'c1', uncommitted: false })),
  }
  const { root, row } = await openPicker({ history, getJobs: () => jobs })
  await key(row(jobs[0].id), ' ')
  const relative = vi.spyOn(time, 'formatRelative')
  await key(row(jobs[0].id), 'ArrowDown')
  const formatted = relative.mock.calls.map(([at]) => at)
  relative.mockRestore()
  expect(row(jobs[0].id).getAttribute('aria-selected')).toBe('false')
  expect(row(jobs[1].id).getAttribute('aria-selected')).toBe('true')
  expect(formatted).toContain(jobs[0].createdAt)
  expect(formatted).toContain(jobs[1].createdAt)
  const untouched = new Set(jobs.slice(2).map((job) => job.createdAt))
  expect(formatted.filter((at) => untouched.has(at!))).toEqual([])
  await act(async () => root.unmount())
})

it('reads nearby rows beyond the first 400 and reuses them when scrolling back', async () => {
  const jobs = Array.from({ length: 600 }, (_, index) => summary(`scroll-${index}`, {
    state: 'stopped', failed: true, failedSteps: [`step-${index}`],
  }))
  const history = { ...HISTORY, commits: [HISTORY.commits[1]], jobs: jobs.map((job) => ({ ...job, commit: 'c1', uncommitted: false })) }
  const { root, row, viewport, summaryRequests } = await openPicker({ history, getJobs: () => jobs })
  expect(summaryRequests.flat()).toEqual(jobs.slice(0, 20).map((job) => job.id))
  expect(row('scroll-550').textContent).not.toContain('Failed')
  await viewport(['scroll-550', 'scroll-551'])
  expect(summaryRequests.at(-1)).toEqual(['scroll-550', 'scroll-551'])
  expect(row('scroll-550').textContent).toContain('Failed in step-550')
  const count = summaryRequests.length
  await viewport(['scroll-0', 'scroll-1'])
  expect(summaryRequests).toHaveLength(count)
  expect(row('scroll-0').textContent).toContain('Failed in step-0')
  await act(async () => root.unmount())
})

it('observes the rows left by filters instead of loading hidden recent jobs', async () => {
  const jobs = Array.from({ length: 500 }, (_, index) => summary(`filter-${index}`))
  jobs.push(summary('old-filtered', { workflowName: 'probe', state: 'stopped', failed: true, failedSteps: ['judge'] }))
  const history = { ...HISTORY, commits: [HISTORY.commits[1]], jobs: jobs.map((job) => ({ ...job, commit: 'c1', uncommitted: false })) }
  const { root, row, summaryRequests } = await openPicker({ history, getJobs: () => jobs })
  await act(async () => searchBox().dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  await act(async () => workflowOption('probe')!.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
  expect(summaryRequests.at(-1)).toEqual(['old-filtered'])
  expect(summaryRequests.flat()).toHaveLength(21)
  expect(row('old-filtered').textContent).toContain('Failed in judge')
  await act(async () => root.unmount())
})

it('defaults to Recent when every commit is missing, while keeping the explanation accessible', async () => {
  const { root, row } = await openPicker({ history: { ...HISTORY, jobs: [], commits: [], hidden: 3 } })
  const tab = (name: string) => [...document.querySelectorAll<HTMLElement>('[role="tab"]')].find((each) => each.textContent === name)!
  expect(tab('Recent').getAttribute('aria-selected')).toBe('true')
  expect(row('job-old')).not.toBeNull()
  await act(async () => tab('By commit').click())
  expect(tab('By commit').getAttribute('aria-selected')).toBe('true')
  expect(document.querySelector('[data-jobs]')!.textContent).toContain('3 jobs are not shown here: their commits are not in this repository')
  expect(document.querySelector('[data-jobs]')!.textContent).not.toContain('No jobs yet')
  await act(async () => root.unmount())
})

it.each(['stars', 'summaries'])('offers retry after a failed %s read instead of loading forever', async (failure) => {
  localStorage.setItem('fxtr-job-picker-view', 'starred')
  let failing = true
  const { root, row } = await openPicker({
    initialStars: { 'job-old': { name: 'saved', starredAt: '2026-11-12T00:00:00Z' } },
    starStatus: () => failure === 'stars' && failing ? 500 : 200,
    summaryError: () => failure === 'summaries' && failing,
  })
  expect(document.querySelector('[role="alert"]')).not.toBeNull()
  expect(document.querySelector('[data-jobs]')!.textContent).not.toContain('Loading')
  failing = false
  await act(async () => document.querySelector<HTMLButtonElement>('[role="alert"] button')!.click())
  expect(document.querySelector('[role="alert"]')).toBeNull()
  expect(row('job-old').textContent).toContain('saved')
  await act(async () => root.unmount())
})

it('finishes loading absent starred jobs and can filter the remaining jobs to an empty result', async () => {
  localStorage.setItem('fxtr-job-picker-view', 'starred')
  const initialStars = {
    'job-deleted': { name: 'gone', starredAt: '2026-11-12T00:00:00Z' },
    'job-old': { name: 'saved', starredAt: '2026-11-12T00:00:00Z' },
  }
  let jobs: JobSummary[] = []
  const { root, row } = await openPicker({ initialStars, getJobs: () => jobs })
  expect(document.querySelector('[data-jobs]')!.textContent).toContain('2 starred jobs are unavailable')
  expect(document.querySelector('[data-jobs]')!.textContent).not.toContain('Loading')
  // Reopening rechecks missing ids, so changing databases can make a saved job available.
  await act(async () => document.querySelector<HTMLElement>('[aria-label="Choose job"]')!.click())
  jobs = JOBS
  await act(async () => document.querySelector<HTMLElement>('[aria-label="Choose job"]')!.click())
  expect(row('job-old')).not.toBeNull()
  expect(document.querySelector('[data-jobs]')!.textContent).toContain('1 starred job is unavailable')
  await typeInto(searchBox(), 'no-such-job')
  await wait(200)
  expect(document.querySelector('[data-jobs]')!.textContent).toContain('No jobs match')
  expect(document.querySelector('[data-jobs]')!.textContent).not.toContain('Loading')
  await act(async () => root.unmount())
})

it.each(['PUT', 'DELETE'])('reports a failed star %s and restores the saved state', async (method) => {
  const wasStarred = method === 'DELETE'
  const { root, row } = await openPicker({
    initialStars: wasStarred ? { 'job-old': { name: 'saved', starredAt: '2026-11-12T00:00:00Z' } } : {},
    starStatus: (request) => request === method ? 500 : 200,
  })
  const label = wasStarred ? 'Remove star' : 'Star job'
  await act(async () => row('job-old').querySelector<HTMLElement>(`[aria-label="${label}"]`)!.click())
  expect(toast.error).toHaveBeenCalledWith('Could not save starred jobs', expect.objectContaining({ description: expect.any(String) }))
  expect(row('job-old').querySelector(`[aria-label="${label}"]`)).not.toBeNull()
  await act(async () => root.unmount())
})
