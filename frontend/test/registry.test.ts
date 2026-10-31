// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { VIEW_API_VERSION } from 'fxtr-view'
import { activateStylesheet, loadViews } from '../src/views/registry'

const module = { default: () => ({ renderers: [] }) }
const importer = () => Promise.resolve(module)

function manifest(version: string, styles = true): Response {
  return new Response(JSON.stringify({
    apiVersion: VIEW_API_VERSION,
    module: 'bundle.js',
    ...(styles ? { stylesheet: 'bundle.css' } : {}),
  }), { headers: { 'X-Fxtr-Views-Version': version } })
}

async function candidate(): Promise<HTMLLinkElement> {
  let link: HTMLLinkElement | null = null
  await vi.waitFor(() => {
    link = document.head.querySelector<HTMLLinkElement>('link[data-fxtr-views][media="not all"]')
    expect(link).not.toBeNull()
  })
  return link!
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  document.head.querySelectorAll('link[data-fxtr-views]').forEach((link) => link.remove())
})

it('waits for both assets and leaves prepared styles inactive until commit', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => manifest('v2')))
  let release!: (value: typeof module) => void
  const importing = new Promise<typeof module>((resolve) => { release = resolve })
  const old = document.createElement('link')
  old.setAttribute('data-fxtr-views', '')
  document.head.append(old)
  const loading = loadViews('v2', () => importing)
  let completed = false
  void loading.then(() => { completed = true })
  const link = await candidate()
  link.dispatchEvent(new Event('load'))
  await Promise.resolve()
  expect(completed).toBe(false)
  expect(old.isConnected).toBe(true)
  release(module)
  const loaded = await loading
  expect(loaded.views.version).toBe('v2')
  expect(link.media).toBe('not all')
  expect(old.isConnected).toBe(true)
  activateStylesheet(loaded.stylesheet)
  expect(link.media).toBe('all')
  expect(old.isConnected).toBe(false)
})

it.each(['before', 'after'])('reports CSS failure %s module completion and permits another load', async (when) => {
  let current = 'v2'
  const fetch = vi.fn(async () => manifest(current))
  vi.stubGlobal('fetch', fetch)
  let release!: (value: typeof module) => void
  const importing = new Promise<typeof module>((resolve) => { release = resolve })
  const loading = loadViews('v2', () => importing)
  const link = await candidate()
  if (when === 'after') { release(module); await Promise.resolve() }
  link.dispatchEvent(new Event('error'))
  const failed = await loading
  expect(failed.views.version).toBeNull()
  expect(failed.views.problem).toContain('could not load stylesheet')
  expect(link.isConnected).toBe(false)
  expect(fetch).toHaveBeenCalledTimes(2) // Initial manifest, then one check for a newer build.
  release(module)
  current = 'v3'
  const next = loadViews('v3', importer)
  ;(await candidate()).dispatchEvent(new Event('load'))
  expect((await next).views.version).toBe('v3')
})

it('removes candidate styles when the module fails, retaining the active stylesheet', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => manifest('v2')))
  const old = document.createElement('link')
  old.setAttribute('data-fxtr-views', '')
  document.head.append(old)
  const result = await loadViews('v2', async () => { throw new Error('broken module') })
  expect(result.views.problem).toContain('broken module')
  expect(document.head.querySelectorAll('link[data-fxtr-views]')).toHaveLength(1)
  expect(old.isConnected).toBe(true)
})

it('recovers an expired manifest by loading the current version', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(new Response('expired', { status: 410 }))
    .mockResolvedValueOnce(manifest('v4', false))
  vi.stubGlobal('fetch', fetch)
  const loadModule = vi.fn(importer)
  expect((await loadViews('v1', loadModule)).views.version).toBe('v4')
  expect(new URL(String(fetch.mock.calls[1][0])).search).toBe('')
  expect(loadModule).toHaveBeenCalledWith(expect.stringContaining('bundle.js?v=v4'))
})

it('restarts the complete bundle when an asset expires during loading', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(manifest('v1'))
    .mockResolvedValueOnce(manifest('v4'))
  vi.stubGlobal('fetch', fetch)
  const loading = loadViews('v1', importer)
  const expired = await candidate()
  expired.dispatchEvent(new Event('error'))
  await vi.waitFor(() => expect(expired.isConnected).toBe(false))
  const current = await candidate()
  expect(current.href).toContain('?v=v4')
  current.dispatchEvent(new Event('load'))
  expect((await loading).views.version).toBe('v4')
})

it('settles and cleans up a stalled asset load', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('fetch', vi.fn(async () => manifest('v2')))
  const loading = loadViews('v2', () => new Promise(() => {}))
  await vi.advanceTimersByTimeAsync(16_000)
  expect((await loading).views.problem).toContain('timed out')
  expect(document.head.querySelector('link[data-fxtr-views]')).toBeNull()
})
