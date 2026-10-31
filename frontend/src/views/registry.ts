/**
 * The registry this app dispatches through: the viewer's own renderers (the defaults, compiled in)
 * plus the renderers of the project the backend serves, loaded at runtime from the project's view
 * bundle (see fxtr-view's view_bundle.ts). Nothing in the app names a project.
 *
 * `loadViews` fetches the bundle's manifest from the backend, checks the contract version before
 * running any of the bundle's code, links its stylesheet, imports its module, and calls the
 * module's factory with this app's own instances of the shared modules. A project without views has
 * no bundle and nothing is said; any other failure leaves the app with the defaults alone and a
 * `problem` saying why. The manifest also names the bundle's
 * `version` (`X-Fxtr-Views-Version`), a hash of its built files.
 *
 * The first load runs before the app renders, so the first paint never shows the defaults in
 * place of the project's renderers. After that the bundle is loaded again whenever the backend
 * announces a new version (`liveViews.ts`): the module's URL carries the version, so the browser
 * runs the new code rather than its cached module, and the new stylesheet has loaded before the
 * registry is swapped (`activateStylesheet`). Failed candidates are removed without changing the current styles.
 */

import * as react from 'react'
import * as jsxRuntime from 'react/jsx-runtime'
import * as jotai from 'jotai'
import * as fxtrView from 'fxtr-view'
import { VIEW_API_VERSION, VIEW_BUNDLE_MANIFEST_FILENAME, type AnyRenderer, type SharedModules, type ViewBundleFactory, type ViewBundleManifest } from 'fxtr-view'
import { dispatchRegistry, type DispatchRegistry } from 'fxtr-view-shell-util'
import { defaultRenderers } from 'fxtr-view-default-renderers'
import { VIEWS_BASE } from '../config'

const ASSET_TIMEOUT_MS = 15_000
const VIEWS_VERSION_HEADER = 'X-Fxtr-Views-Version'

/** What the app dispatches through, why the project's renderers are missing when they are,
 *  and which build of them loaded. */
export interface LoadedViews {
  readonly registry: DispatchRegistry
  readonly problem: string | null
  /** The loaded bundle's version, null when none loaded. */
  readonly version: string | null
}

/** A completed load and its inactive stylesheet, ready to commit with `activateStylesheet`. */
export interface ViewsLoad {
  readonly views: LoadedViews
  readonly stylesheet: HTMLLinkElement | null
}

const SHARED: SharedModules = { react, 'react/jsx-runtime': jsxRuntime, jotai, 'fxtr-view': fxtrView }

/** The viewer's own renderers alone, with why the project's are missing (null: it has none). */
export function defaultViews(problem: string | null): LoadedViews {
  return { registry: dispatchRegistry(defaultRenderers), problem, version: null }
}

type BundleModule = { default: ViewBundleFactory }
type ImportBundle = (url: string) => Promise<BundleModule>
const importBundle: ImportBundle = (url) => import(/* @vite-ignore */ url) as Promise<BundleModule>

/** Load a complete bundle. Expired builds retry the current manifest; asset failures retry only
 *  when that manifest names another version. Each version is attempted at most once per load. */
export async function loadViews(version: string | null = null, importer: ImportBundle = importBundle): Promise<ViewsLoad> {
  const failed = (problem: string | null): ViewsLoad => ({ views: defaultViews(problem), stylesheet: null })
  const manifestUrl = new URL(`${VIEWS_BASE}/${VIEW_BUNDLE_MANIFEST_FILENAME}`, window.location.href)
  const fetchManifest = (wanted: string | null) => {
    const url = new URL(manifestUrl)
    if (wanted !== null) url.searchParams.set('v', wanted)
    return fetch(url, { cache: 'no-cache', signal: AbortSignal.timeout(ASSET_TIMEOUT_MS) })
  }
  try {
    let response = await fetchManifest(version)
    if (response.status === 410) response = await fetchManifest(null)
    const attempted = new Set<string>()
    for (;;) {
      if (response.status === 404) return failed(null)
      if (!response.ok) return failed(await response.text())
      const loaded = response.headers.get(VIEWS_VERSION_HEADER)
      if (loaded === null) return failed('the view bundle manifest has no version')
      attempted.add(loaded)
      try {
        return await prepareBundle(response, manifestUrl, loaded, importer)
      } catch (error) {
        // Import/link failures do not expose HTTP status. A fresh manifest tells us whether
        // this build was superseded; never retry the same failing module URL indefinitely.
        const latest = await fetchManifest(null)
        const next = latest.headers.get(VIEWS_VERSION_HEADER)
        if (!latest.ok || next === null || attempted.has(next)) {
          return failed(`the project's view bundle did not load: ${String(error)}`)
        }
        response = latest
      }
    }
  } catch (error) {
    return failed(`could not load the project's view bundle (${String(error)})`)
  }
}

/** Prepare both resources, leaving the stylesheet inactive until the caller commits the load. */
async function prepareBundle(response: Response, base: URL, version: string, importer: ImportBundle): Promise<ViewsLoad> {
  const manifest = (await response.json()) as ViewBundleManifest
  if (manifest.apiVersion !== VIEW_API_VERSION) {
    throw new Error(`the project's view bundle is at view API version ${manifest.apiVersion}; this viewer supports ${VIEW_API_VERSION}`)
  }
  const versioned = (name: string) => {
    const url = new URL(name, base)
    url.searchParams.set('v', version)
    return url.href
  }
  const stylesheet = manifest.stylesheet === undefined ? null : document.createElement('link')
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const stylesReady = new Promise<void>((resolve, reject) => {
      if (stylesheet === null) { resolve(); return }
      stylesheet.rel = 'stylesheet'
      stylesheet.media = 'not all'
      stylesheet.href = versioned(manifest.stylesheet!)
      stylesheet.setAttribute(STYLESHEET_ATTRIBUTE, '')
      stylesheet.onload = () => resolve()
      stylesheet.onerror = () => reject(new Error(`could not load stylesheet ${stylesheet.href}`))
      document.head.append(stylesheet)
    })
    const expired = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('loading the view bundle timed out')), ASSET_TIMEOUT_MS)
    })
    const [module] = await Promise.race([
      Promise.all([Promise.resolve().then(() => importer(versioned(manifest.module))), stylesReady]),
      expired,
    ])
    const renderers = module.default(SHARED).renderers
    return {
      views: { registry: dispatchRegistry([...defaultRenderers, ...renderers]), problem: null, version },
      stylesheet,
    }
  } catch (error) {
    stylesheet?.remove()
    throw error
  } finally {
    clearTimeout(timer)
    if (stylesheet !== null) { stylesheet.onload = null; stylesheet.onerror = null }
  }
}

/** Whether a renderer is the project's own rather than one of the viewer's defaults: the views a
 *  dropdown lists under "from this project" and the step card's custom-view icon counts. */
export function isProjectRenderer(renderer: AnyRenderer): boolean {
  return !defaultRenderers.includes(renderer)
}

/** Whether a view is a detail, reached only through a link that binds it: one declared within
 *  another, or one that needs params bound. A list of views to open (the rail, a step card's views
 *  button) leaves these out, since opening one from there binds nothing. */
export function isDetailView(renderer: AnyRenderer): boolean {
  return renderer.within !== undefined || (renderer.params ?? []).length > 0
}

/** Whether the renderer with this id is the project's own (`isProjectRenderer`), for an address
 *  that names its view by id alone. */
export function isProjectRendererId(id: string): boolean {
  return !defaultRenderers.some((renderer) => renderer.id === id)
}

/** The attribute marking the stylesheets a load has linked. */
const STYLESHEET_ATTRIBUTE = 'data-fxtr-views'

/** Activate a prepared bundle's styles and retire the previous bundle's styles together. */
export function activateStylesheet(keep: HTMLLinkElement | null): void {
  if (keep !== null) keep.media = 'all'
  for (const link of document.head.querySelectorAll<HTMLLinkElement>(`link[${STYLESHEET_ATTRIBUTE}]`)) {
    if (link !== keep) link.remove()
  }
}
