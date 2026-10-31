// @vitest-environment jsdom
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { atom } from 'jotai'
import { InvocationPanel, renderer, type JsonValue, type Spawn } from 'fxtr-view'
import { RegistryProvider, RenderInvocation, dispatchRegistry } from 'fxtr-view-shell-util'
import { ViewsFollower, useLiveViews, type ViewsEvent } from '../src/views/liveViews'
import { loadViews, type ViewsLoad } from '../src/views/registry'
import { ViewsNotice } from '../src/components/ViewsNotice'

/** A load of a bundle whose one renderer draws `text`. */
function bundle(version: string, text: string): ViewsLoad {
  const Overview = () => <b>{text}</b>
  return {
    views: { registry: dispatchRegistry([renderer(InvocationPanel, 'wf', 'overview', Overview)]), problem: null, version },
    stylesheet: null,
  }
}

const failedLoad: ViewsLoad = {
  views: { registry: dispatchRegistry([]), problem: 'SyntaxError: bad bundle', version: null },
  stylesheet: null,
}

const event = (version: string | null, state: ViewsEvent['state'] = 'ready', error: string | null = null): ViewsEvent =>
  ({ type: 'views', version, state, error })

function Counter() {
  const [count, setCount] = useState(0)
  return <button onClick={() => setCount(count + 1)}>clicked {count}</button>
}

const SPAWN: Spawn = { key: {}, ref: { jobId: 'job', kind: 'workflow', pathname: 'root' }, output: null }
const storage = atom<JsonValue>(null)

/** The registry in the store, with a renderer dispatched through it beside some state of the
 *  page's own. */
function Page() {
  const { views } = useLiveViews()
  return (
    <RegistryProvider registry={views.registry}>
      <Counter />
      <ViewsNotice problem={views.problem} />
      <RenderInvocation slot={InvocationPanel} name="wf" renderer="overview" invocations={[SPAWN]} storage={storage} />
    </RegistryProvider>
  )
}

afterEach(() => vi.unstubAllGlobals())

it('swaps in a rebuilt bundle, keeping the page\'s own state', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const load = vi.fn(async (version: string) => bundle(version, `build ${version}`))
  const follower = new ViewsFollower(bundle('v1', 'build v1'), load)
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<Page />))
    expect(host.textContent).toContain('build v1')
    await act(async () => host.querySelector('button')!.click())
    expect(host.textContent).toContain('clicked 1')

    // The version already loaded: nothing to do.
    await act(() => follower.announce(event('v1')))
    expect(load).not.toHaveBeenCalled()

    // Building: said, nothing loaded.
    await act(() => follower.announce(event('v1', 'building')))
    expect(load).not.toHaveBeenCalled()

    await act(() => follower.announce(event('v2')))
    expect(load).toHaveBeenCalledWith('v2')
    expect(host.textContent).toContain('build v2')
    expect(host.textContent).toContain('clicked 1')
  } finally {
    await act(async () => root.unmount())
  }
})

it('keeps the renderers in use when a rebuilt bundle does not load, and tries the next build', async () => {
  const load = vi.fn(async (version: string) => (version === 'v2' ? failedLoad : bundle(version, version)))
  const follower = new ViewsFollower(bundle('v1', 'v1'), load)
  let live: ReturnType<typeof useLiveViews> | null = null
  function Reader() {
    live = useLiveViews()
    return null
  }
  const root = createRoot(document.createElement('div'))
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  try {
    await act(async () => root.render(<Reader />))
    await act(() => follower.announce(event('v2')))
    expect(live!.views.version).toBe('v1')
    expect(live!.views.problem).toMatch(/rebuilt renderers did not load.*SyntaxError: bad bundle/)
    // Announced again, the same build is not loaded again.
    await act(() => follower.announce(event('v2')))
    expect(load).toHaveBeenCalledTimes(1)
    await act(() => follower.announce(event('v3')))
    expect(live!.views.problem).toBe(null)
    expect(live!.views.version).toBe('v3')
    expect(live!.updates).toBe(1)
  } finally {
    await act(async () => root.unmount())
  }
})

it('says a failed build and its output while the last good bundle stays', async () => {
  const follower = new ViewsFollower(bundle('v1', 'v1'), vi.fn())
  let live: ReturnType<typeof useLiveViews> | null = null
  function Reader() {
    live = useLiveViews()
    return null
  }
  const root = createRoot(document.createElement('div'))
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  try {
    await act(async () => root.render(<Reader />))
    await act(() => follower.announce(event('v1', 'failed', 'src/index.tsx:3 error')))
    expect(live!.build).toEqual({ state: 'failed', error: 'src/index.tsx:3 error' })
    expect(live!.views.version).toBe('v1')
  } finally {
    await act(async () => root.unmount())
  }
})

it.each([false, true])('recovers from a startup build failure with a previous bundle: %s', async (hasBundle) => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  const load = vi.fn(async (version: string) => bundle(version, 'fixed renderers'))
  const follower = new ViewsFollower(hasBundle ? bundle('v1', 'previous renderers') : failedLoad, load)
  const host = document.createElement('div')
  const root = createRoot(host)
  try {
    await act(async () => root.render(<Page />))
    await act(() => follower.announce(event(hasBundle ? 'v1' : null, 'failed', 'src/index.tsx:3 error')))
    expect(host.textContent).toContain('src/index.tsx:3 error')
    expect(host.textContent?.includes('the last build is still shown')).toBe(hasBundle)
    expect(host.textContent?.includes('previous renderers')).toBe(hasBundle)
    expect(load).not.toHaveBeenCalled()
    await act(async () => host.querySelector('button')!.click())

    await act(() => follower.announce(event('v2')))
    expect(host.textContent).toContain('fixed renderers')
    expect(host.textContent).toContain('clicked 1')
    expect(host.querySelector('[role="alert"]')).toBe(null)
  } finally {
    await act(async () => root.unmount())
  }
})

it('loads only the latest of the builds announced during a load', async () => {
  let release: () => void = () => {}
  const load = vi.fn(async (version: string) => {
    if (version === 'v2') await new Promise<void>((resolve) => { release = resolve })
    return bundle(version, version)
  })
  const follower = new ViewsFollower(bundle('v1', 'v1'), load)
  const first = follower.announce(event('v2'))
  const second = follower.announce(event('v3'))
  const third = follower.announce(event('v4'))
  release()
  await Promise.all([first, second, third])
  expect(load.mock.calls.map(([version]) => version)).toEqual(['v2', 'v4'])
})

it('asks for a build by version, and reads the version the manifest names', async () => {
  const fetch = vi.fn(async () => new Response(JSON.stringify({ apiVersion: -1, module: 'bundle.js' }), {
    headers: { 'X-Fxtr-Views-Version': 'abc' },
  }))
  vi.stubGlobal('fetch', fetch)
  const { views } = await loadViews('abc')
  expect(String((fetch.mock.calls[0] as unknown[])[0])).toMatch(/\/api\/views\/fxtr-view-bundle-manifest\.json\?v=abc$/)
  // Another contract version is refused before any of its code runs.
  expect(views.problem).toMatch(/view API version -1/)
})

it('handles an announcement once when expiration recovery loads a newer version', async () => {
  const load = vi.fn(async () => bundle('v4', 'v4'))
  const follower = new ViewsFollower(bundle('v1', 'v1'), load)
  await follower.announce(event('v2'))
  await follower.announce(event('v2'))
  await follower.announce(event('v4'))
  expect(load).toHaveBeenCalledTimes(1)
})

it('does not lose an announcement immediately after an already-loaded version', async () => {
  const load = vi.fn(async (version: string) => bundle(version, version))
  const follower = new ViewsFollower(bundle('v1', 'v1'), load)
  await Promise.all([follower.announce(event('v1')), follower.announce(event('v2'))])
  expect(load).toHaveBeenCalledTimes(1)
  expect(load).toHaveBeenCalledWith('v2')
})
