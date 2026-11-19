/**
 * The viewer framed by a hub: how it learns which job it shows.
 *
 * A hub's page for one job loads this viewer in an iframe with `sandbox="allow-scripts"`
 * and no `allow-same-origin`, on a domain of its own, so the frame's origin is opaque: it has no
 * cookies or storage and cannot reach the hub's pages. Its URL carries `?hosted`, and nothing
 * secret. The frame posts `{type: 'fxtr-viewer-ready'}` to its parent; the parent checks the
 * message came from that frame's window and answers `{type: 'fxtr-viewer-config', config}`. The
 * frame accepts configuration only from its own parent and renewals only for the same job,
 * so a page that frames the viewer elsewhere cannot hand it a configuration through another
 * window. The token in the configuration reads only that one job; the frame keeps it in
 * memory and never in its URL or storage.
 *
 * A configuration is also refused unless its API and its renderer bundle are on the viewer's own
 * origin (the origin of the document's URL; the sandbox makes `location.origin` the string
 * "null"). The bundle is arbitrary JavaScript that the viewer imports and runs at that origin, and
 * `fxtr view` serves this same build locally with nothing to tell it who framed it. A page
 * anywhere could otherwise frame `http://127.0.0.1:<port>/?hosted` and hand it a bundle of its own
 * to run there. A hub issues both URLs on the viewer's origin, so the rule costs it nothing.
 *
 * The token lasts minutes, not the time a reader spends on a page. Before it expires the parent
 * posts another `fxtr-viewer-config` message to the same frame, for the same job; the
 * frame keeps listening after the first and installs each renewal's token in place
 * (`renewViewerHeaders`), so the reader keeps their place. The bundle's URLs carry the token,
 * and the bundle module the frame has imported resolves its later imports against them, so a hub
 * renews by extending the token the frame holds; when it must issue another (the frame's expired
 * while the page was suspended), it replaces the frame instead of posting the new token into it.
 */

import type { ViewerConfig } from './config'
import { ACTION_MESSAGE, CONFIG_MESSAGE, READY_MESSAGE, SHOWN_MESSAGE, type HostedConfig } from 'fxtr-view-shell-util/hosted'

export { ACTION_MESSAGE, CONFIG_MESSAGE, READY_MESSAGE, SHOWN_MESSAGE, type HostedConfig }

/** Whether this page is a viewer framed by a hub (its URL has `?hosted`). */
export function isHosted(location: Location = window.location): boolean {
  return new URLSearchParams(location.search).has('hosted')
}

/** Ask the parent window for the configuration and wait for its answer (see the module
 *  docstring); rejects after `timeoutMs`. Every later configuration the parent posts for the same
 *  job is passed to `onRenewal`; one for another job is ignored. */
export function receiveHostedConfig(
  self: Window = window,
  { timeoutMs = 15_000, onRenewal = () => {} }: { timeoutMs?: number; onRenewal?: (config: HostedConfig) => void } = {},
): Promise<HostedConfig> {
  return new Promise((resolve, reject) => {
    let accepted: HostedConfig | null = null
    const timer = self.setTimeout(() => {
      self.removeEventListener('message', onMessage)
      reject(new Error('the hub page did not configure the viewer'))
    }, timeoutMs)
    function onMessage(event: MessageEvent) {
      if (event.source !== self.parent) return
      const data: unknown = event.data
      if (!isConfigMessage(data) || !onThisOrigin(data.config, self.location.href)) return
      if (accepted !== null) {
        if (data.config.jobId === accepted.jobId) onRenewal(data.config)
        return
      }
      accepted = data.config
      self.clearTimeout(timer)
      resolve(data.config)
    }
    self.addEventListener('message', onMessage)
    // The parent's origin is the hub's; the frame cannot name it in advance, and the message
    // carries nothing secret.
    self.parent.postMessage({ type: READY_MESSAGE }, '*')
  })
}

function isConfigMessage(data: unknown): data is { type: string; config: HostedConfig } {
  if (typeof data !== 'object' || data === null) return false
  const message = data as { type?: unknown; config?: unknown }
  if (message.type !== CONFIG_MESSAGE) return false
  const config = message.config as Partial<HostedConfig> | null | undefined
  return (
    typeof config === 'object' &&
    config !== null &&
    typeof config.apiBase === 'string' &&
    typeof config.token === 'string' &&
    typeof config.jobId === 'string' &&
    (config.bundle === null ||
      (typeof config.bundle === 'object' &&
        typeof config.bundle.baseUrl === 'string')) &&
    (config.bundleNote === null || config.bundleNote === 'commit' || config.bundleNote === 'different-commit' || config.bundleNote === 'unknown-commit')
  )
}

/** Whether the API and the bundle (when there is one) are on the origin of `documentUrl`, the
 *  viewer's own; see the module docstring for why anything else is refused. */
function onThisOrigin(config: HostedConfig, documentUrl: string): boolean {
  const origin = new URL(documentUrl).origin
  const urls = [config.apiBase, ...(config.bundle === null ? [] : [config.bundle.baseUrl])]
  return urls.every((url) => {
    try {
      return new URL(url, documentUrl).origin === origin
    } catch {
      return false
    }
  })
}

/** The headers every API request carries for `config`'s token. */
export function hostedHeaders(config: Pick<HostedConfig, 'token'>): Readonly<Record<string, string>> {
  return { authorization: `Bearer ${config.token}` }
}

/** Translate the embedding protocol into the same configuration a standalone viewer uses. */
export function hostedViewerConfig(config: HostedConfig, parent: Pick<Window, 'postMessage'> = window.parent): ViewerConfig {
  const base = config.apiBase.replace(/\/$/, '')
  const notice = config.bundle === null
    ? 'Shown with the default renderers: this job has no renderers of its own, or you chose not to load them.'
    : config.bundleNote === 'different-commit'
      ? 'Shown with renderers built at a different commit from the one this job ran at.'
      : config.bundleNote === 'unknown-commit'
        ? 'Shown with renderers whose build commit is unknown.'
        : null
  return {
    graphqlUrl: `${base}/graphql/`,
    entitiesUrl: `${base}/entities`,
    viewsBase: config.bundle === null ? null : config.bundle.baseUrl.replace(/\/$/, ''),
    sourceBase: null,
    fileUrl: null,
    experimentReadmeUrl: null,
    eventsUrl: null,
    previewsBase: null,
    jobCommitsUrl: null,
    starredUrl: null,
    headers: hostedHeaders(config),
    notice,
    detailsAction: {
      label: 'Job details and sharing',
      run: () => parent.postMessage({ type: ACTION_MESSAGE, action: 'details' }, '*'),
    },
    onShown: () => parent.postMessage({ type: SHOWN_MESSAGE }, '*'),
  }
}
