/**
 * The project's renderers as they are rebuilt while the page is open. `fxtr view` rebuilds the
 * project's view bundle as its sources change and announces each state of it on the viewer's events
 * (`api/events.ts`): `{type: "views", version, state, error}`, where `version` is the bundle served
 * (a hash of its built files) and `state` is `building`, `failed` (with the error and the end of the
 * build's output; the last good bundle is still served), or `ready`.
 *
 * `startLiveViews` follows those events. Whenever they name a version other than the one loaded,
 * it loads that build beside the current one (`loadViews`); once the new module has run and its
 * stylesheet has loaded, it replaces the views in the store at once, and removes the old
 * stylesheet. A load that fails keeps the current renderers and says why. One load runs at a time;
 * versions announced meanwhile are caught up with after it, the latest only. The page reads the
 * store with `useLiveViews`: replacing the registry re-renders dispatch with the new components
 * (fxtr-view-shell-util remounts everything dispatched through the old registry), while the app's
 * own state (the job, level, panes, the entity cache) stays as it is.
 *
 * The events reconnect when the server goes away, and the server sends the current state first on
 * every connection, so a page left open across a restart of `fxtr view` catches up with what was
 * built meanwhile.
 */

import { useSyncExternalStore } from 'react'
import { onViewerEvent } from '../api/events'
import { Store } from '../lib/store'
import { defaultViews, loadViews, activateStylesheet, type LoadedViews, type ViewsLoad } from './registry'

/** The state of the project's view bundle's build, as the backend last said. */
export interface ViewsBuild {
  readonly state: 'ready' | 'building' | 'failed'
  /** Why the last build failed, with the end of its output. */
  readonly error: string | null
}

export interface LiveViews {
  readonly views: LoadedViews
  readonly build: ViewsBuild
  /** How many times the renderers were replaced since the page loaded. */
  readonly updates: number
}

/** The view bundle's event on the viewer's events. */
export interface ViewsEvent {
  readonly type: 'views'
  readonly version: string | null
  readonly state: ViewsBuild['state']
  readonly error: string | null
}

const READY: ViewsBuild = { state: 'ready', error: null }

const store = new Store<LiveViews>({ views: defaultViews(null), build: READY, updates: 0 })

/** The renderers in use and the state of their build. */
export function useLiveViews(): LiveViews {
  return useSyncExternalStore(store.subscribe, store.get)
}

/** The loading and swapping, apart from where the events come from (tests drive `announce`). */
export class ViewsFollower {
  /** The version the backend last said it serves. */
  private wanted: string | null = null
  /** The last announcement handled, even if its expired build loaded a newer one. */
  private handled: string | null = null
  private loading: Promise<void> | null = null

  constructor(initial: ViewsLoad, private readonly load: (version: string) => Promise<ViewsLoad> = loadViews) {
    store.set({ views: initial.views, build: READY, updates: 0 })
    activateStylesheet(initial.stylesheet)
  }

  /** Take in one event; resolves once any load it starts has finished (for tests). */
  announce(event: ViewsEvent): Promise<void> {
    store.update((live) => ({ ...live, build: { state: event.state, error: event.error } }))
    if (event.version !== null && event.version !== this.wanted) {
      this.wanted = event.version
      this.handled = null
    }
    return this.catchUp()
  }

  private async catchUp(): Promise<void> {
    for (;;) {
      // Recheck after the pending load settles, including announcements received during its
      // completion microtask. A completed no-op must never swallow the next announcement.
      if (this.loading !== null) {
        await this.loading
        continue
      }
      const version = this.wanted
      if (version === null || version === store.get().views.version || version === this.handled) return
      this.loading = this.replace(version).finally(() => { this.loading = null })
    }
  }

  private async replace(version: string): Promise<void> {
    const { views, stylesheet } = await this.load(version)
    this.handled = version
    if (views.version === null) {
      const problem = `the rebuilt renderers did not load, so the previous ones are still shown: ${views.problem ?? 'the project has no view bundle now'}`
      store.update((live) => ({ ...live, views: { ...live.views, problem } }))
      return
    }
    if (views.version === store.get().views.version) {
      stylesheet?.remove()
      return
    }
    store.update((live) => ({ ...live, views, updates: live.updates + 1 }))
    activateStylesheet(stylesheet)
  }
}

/** Start following the view bundle's events, from the views the page first loaded. A project with
 *  no renderers of its own (no problem, and no version) has nothing to follow. */
export function startLiveViews(initial: ViewsLoad): void {
  const follower = new ViewsFollower(initial)
  if (initial.views.problem === null && initial.views.version === null) return
  onViewerEvent((event) => {
    if (event.type === 'views') void follower.announce(event)
  })
}
