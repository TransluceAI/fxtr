// @vitest-environment jsdom
import {act} from 'react'
import {createRoot} from 'react-dom/client'
import {afterEach, expect, it, vi} from 'vitest'
import {CodeBody, OpenInEditor} from '../src/components/CodePane'
import {LevelHeader} from '../src/components/LevelHeader'
import {OpenEntityContext} from '../src/lib/openEntity'
import {LevelStateContext} from '../src/lib/levelState'
import {formatRel} from '../src/panes/address'
import {levelState, recordingOpener} from './fixtures'
import {glyphOf} from '../src/lib/progress'
import type {Level} from '../src/types'

afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals(); vi.restoreAllMocks() })

const SOURCE = {
  file: 'src/jokes/experiment.py',
  repoRoot: '/home/me/jokes',
  symbol: 'tell',
  startLine: 3,
  endLine: 4,
  docstring: 'Tell a joke.',
  commit: 'c151a9d9efcfd5d178e21204f0a68ef92d66bcab',
}
const TEXT = 'import x\n\n@step(name="tell")\ndef tell():\n    pass\n'

/** The backend, answering the source and file routes; every request it saw is in `urls`. */
function stubBackend() {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    urls.push(url)
    if (url.startsWith('/api/source/')) return new Response(JSON.stringify(SOURCE))
    if (url.startsWith('/api/file?')) return new Response(JSON.stringify({path: SOURCE.file, commit: SOURCE.commit, text: TEXT}))
    return new Response('not found', {status: 404})
  }))
  return urls
}

async function mount(element: React.ReactElement) {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const host = document.body.appendChild(document.createElement('div'))
  const root = createRoot(host)
  await act(async () => root.render(element))
  // Let the suspended loads settle.
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)) })
  return {host, root}
}

it('shows the file at the job commit with the function marked', async () => {
  const urls = stubBackend()
  const {host, root} = await mount(<CodeBody name="pane.tell" codeVersion="bafycv" />)
  expect(urls).toContain('/api/source/pane.tell?codeVersion=bafycv')
  expect(urls).toContain(`/api/file?path=src%2Fjokes%2Fexperiment.py&commit=${SOURCE.commit}`)
  expect(host.textContent).toContain('src/jokes/experiment.py:3–4 at commit c151a9d')
  const marked = [...host.querySelectorAll('[data-marked]')].map((line) => line.textContent)
  expect(marked).toEqual(['3@step(name="tell")', '4def tell():'])
  await act(async () => root.unmount())
})

it('opens code in the last editor chosen from the Open menu, and keeps the choice', async () => {
  stubBackend()
  const kept = new Map<string, string>()
  vi.stubGlobal('localStorage', {getItem: (key: string) => kept.get(key) ?? null, setItem: (key: string, value: string) => kept.set(key, value)})
  const {host, root} = await mount(<OpenInEditor name="picker.tell" codeVersion={null} />)
  const open = () => host.querySelector('a')!
  expect(open().textContent).toBe('Open')
  expect(open().getAttribute('href')).toBe('cursor://file/home/me/jokes/src/jokes/experiment.py:3')

  await act(async () => host.querySelector<HTMLButtonElement>('button[aria-label="Choose the editor to open in"]')!.click())
  const items = [...document.querySelectorAll<HTMLAnchorElement>('[aria-label="Open in"] a')]
  expect(items.map((item) => item.textContent)).toEqual(['Cursor', 'VS Code', 'VS Code Insiders', 'Windsurf', 'Positron'])
  expect(items.find((item) => item.getAttribute('aria-current'))?.textContent).toBe('Cursor')
  const vscode = items[1]!
  expect(vscode.getAttribute('href')).toBe('vscode://file/home/me/jokes/src/jokes/experiment.py:3')
  // Following the link would leave the page; the choice is what the click records.
  vscode.addEventListener('click', (event) => event.preventDefault())
  await act(async () => vscode.click())

  expect(document.querySelector('[aria-label="Open in"]')).toBeNull()
  expect(open().getAttribute('href')).toBe('vscode://file/home/me/jokes/src/jokes/experiment.py:3')
  expect(kept.get('fxtr-editor')).toBe('vscode')
  await act(async () => root.unmount())
})

it('gives the workflow on screen its docs and a code chip naming the commit', async () => {
  const urls = stubBackend()
  const {opener, follows} = recordingOpener()
  const level = {
    jobId: 'job',
    pathname: 'joke-contest',
    workflowName: 'header.contest',
    codeVersion: 'bafycv',
    status: {state: 'completed', startedAt: null, finishedAt: null, error: null},
    attemptCount: 1,
    steps: [],
    result: null,
    resultCid: null,
    glyph: glyphOf('done'),
    live: false,
    awaiting: null,
  } satisfies Level
  const {host, root} = await mount(
    <LevelStateContext.Provider value={levelState()}>
      <OpenEntityContext.Provider value={opener}>
        <LevelHeader level={level} steps={new Map()} onSelectStep={() => {}} />
      </OpenEntityContext.Provider>
    </LevelStateContext.Provider>,
  )
  expect(urls).toContain('/api/source/header.contest?codeVersion=bafycv')
  expect(host.textContent).toContain('Tell a joke.')
  const chip = host.querySelector<HTMLButtonElement>('button.chip')!
  expect(chip.textContent).toBe('src/jokes/experiment.py:3–4 · c151a9d')
  expect(chip.title).toContain(`at commit ${SOURCE.commit}`)
  await act(async () => chip.click())
  // The root workflow's code, which the panel resolves to the level's function and code version.
  expect(follows.map((target) => formatRel(target.rel, target.params))).toEqual(['code(/)'])
  await act(async () => root.unmount())
})
