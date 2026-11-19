// @vitest-environment jsdom
import { expect, it, vi } from 'vitest'
import { ACTION_MESSAGE, SHOWN_MESSAGE, CONFIG_MESSAGE, hostedHeaders, hostedViewerConfig, isHosted, READY_MESSAGE, receiveHostedConfig, type HostedConfig } from '../src/hosted'
import { configureViewer, LOCAL_CONFIG, renewViewerHeaders, viewerConfig } from '../src/config'

const CONFIG: HostedConfig = {
  apiBase: 'https://content.hub.test/v1/viewer/pub/api/',
  token: 'viewer-token',
  jobId: 'job',
  bundle: { baseUrl: 'https://content.hub.test/v1/viewer/pub/assets/cap/' },
  bundleNote: 'different-commit',
}

/** A window whose parent the test plays: it records what the frame posts to it. The frame is
 *  the hub's viewer page, on the origin `CONFIG` names. */
function aFrame(href = 'https://content.hub.test/viewer/?hosted') {
  const listeners = new Set<(event: MessageEvent) => void>()
  const parent = { postMessage: vi.fn() }
  const self = {
    parent,
    location: { href },
    setTimeout: (fn: () => void, ms: number) => window.setTimeout(fn, ms),
    clearTimeout: (id: number) => window.clearTimeout(id),
    addEventListener: (_: string, fn: (event: MessageEvent) => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: (event: MessageEvent) => void) => listeners.delete(fn),
  }
  const deliver = (source: unknown, data: unknown) => {
    for (const fn of [...listeners]) fn({ source, data } as MessageEvent)
  }
  return { self: self as unknown as Window, parent, deliver, listeners }
}

it('asks its parent for a configuration and takes only the parent window answer', async () => {
  const frame = aFrame()
  const received = receiveHostedConfig(frame.self, { timeoutMs: 1000 })
  expect(frame.parent.postMessage).toHaveBeenCalledWith({ type: READY_MESSAGE }, '*')
  // Another window, a malformed answer, and an unrelated message are all ignored.
  frame.deliver({}, { type: CONFIG_MESSAGE, config: { ...CONFIG, token: 'from-elsewhere' } })
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: { apiBase: 1 } })
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: { ...CONFIG, bundleNote: 'unknown' } })
  frame.deliver(frame.parent, { type: 'something-else' })
  // Fields the frame does not know are ignored, not refused: the parent page keeps its own.
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: { ...CONFIG, expiresAt: '2026-10-19T00:00:00Z' } })
  expect(await received).toEqual({ ...CONFIG, expiresAt: '2026-10-19T00:00:00Z' })
})

it('gives up when the parent never answers', async () => {
  const frame = aFrame()
  await expect(receiveHostedConfig(frame.self, { timeoutMs: 10 })).rejects.toThrow('did not configure')
  expect(frame.listeners.size).toBe(0)
})

it('refuses an API or a bundle on another origin, as the viewer would run its code here', async () => {
  const frame = aFrame('http://127.0.0.1:8000/?hosted')
  const foreign = [
    { ...CONFIG, bundle: null }, // the API alone, on the hub's origin
    { ...CONFIG, apiBase: 'http://127.0.0.1:8000/api' }, // the bundle still on the hub's
    { ...CONFIG, apiBase: 'https://', bundle: null }, // not a URL at all
  ]
  const timedOut = receiveHostedConfig(frame.self, { timeoutMs: 20 })
  for (const config of foreign) frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config })
  await expect(timedOut).rejects.toThrow('did not configure')

  // Resolved against the document, a relative URL is on its origin; `location.origin` is not
  // consulted, since the sandbox makes it "null".
  const local = { ...CONFIG, apiBase: '/api', bundle: { baseUrl: '/assets/' } }
  const received = receiveHostedConfig(frame.self, { timeoutMs: 1000 })
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: foreign[0] })
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: local })
  expect(await received).toEqual(local)
})

it('keeps listening for a renewed token for the same job', async () => {
  const frame = aFrame()
  const onRenewal = vi.fn()
  const received = receiveHostedConfig(frame.self, { timeoutMs: 1000, onRenewal })
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: CONFIG })
  expect(await received).toEqual(CONFIG)
  const renewal = { ...CONFIG, token: 'viewer-token-2' }
  // The same checks as the first answer: not from another window, nor for another origin, nor,
  // being a renewal, for another job.
  frame.deliver({}, { type: CONFIG_MESSAGE, config: renewal })
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: { ...renewal, apiBase: 'https://elsewhere.test/api' } })
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: { ...renewal, jobId: 'other' } })
  expect(onRenewal).not.toHaveBeenCalled()
  frame.deliver(frame.parent, { type: CONFIG_MESSAGE, config: renewal })
  expect(onRenewal).toHaveBeenCalledTimes(1)
  expect(onRenewal).toHaveBeenCalledWith(renewal)
  // What main.tsx does with a renewal: the headers change in place, the rest stays as installed.
  const installed = hostedViewerConfig(CONFIG)
  configureViewer(installed)
  renewViewerHeaders(hostedHeaders(renewal))
  expect(viewerConfig()).toEqual({ ...installed, headers: { authorization: 'Bearer viewer-token-2' } })
  expect(Object.isFrozen(viewerConfig())).toBe(true)
  configureViewer(LOCAL_CONFIG)
})

it('configures authenticated reads of a hub job without local endpoints or events', () => {
  expect(isHosted({ search: '?hosted' } as Location)).toBe(true)
  expect(isHosted({ search: '' } as Location)).toBe(false)
  const parent = { postMessage: vi.fn() }
  const config = hostedViewerConfig(CONFIG, parent)
  expect(config.graphqlUrl).toBe('https://content.hub.test/v1/viewer/pub/api/graphql/')
  expect(config.entitiesUrl).toBe('https://content.hub.test/v1/viewer/pub/api/entities')
  expect(config.viewsBase).toBe('https://content.hub.test/v1/viewer/pub/assets/cap')
  expect(config.headers).toEqual({ authorization: 'Bearer viewer-token' })
  expect([config.sourceBase, config.fileUrl, config.experimentReadmeUrl, config.eventsUrl]).toEqual([null, null, null, null])
  expect([config.previewsBase, config.jobCommitsUrl, config.starredUrl]).toEqual([null, null, null])
  expect(config.notice).toContain('different commit')
  expect(parent.postMessage).not.toHaveBeenCalled()
  expect(config.detailsAction?.label).toBe('Job details and sharing')
  config.detailsAction?.run()
  expect(parent.postMessage).toHaveBeenCalledWith({ type: ACTION_MESSAGE, action: 'details' }, '*')
  config.onShown?.()
  expect(parent.postMessage).toHaveBeenCalledWith({ type: SHOWN_MESSAGE }, '*')
})

it('describes missing renderers and leaves matching renderers without a warning', () => {
  const fallback = hostedViewerConfig({ ...CONFIG, bundle: null })
  expect(fallback.viewsBase).toBeNull()
  expect(fallback.notice).toContain('default renderers')
  expect(hostedViewerConfig({ ...CONFIG, bundleNote: 'commit' }).notice).toBeNull()
  expect(hostedViewerConfig({ ...CONFIG, bundleNote: 'unknown-commit' }).notice).toContain('build commit is unknown')
})
