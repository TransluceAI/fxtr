/** The viewer's endpoints and optional settings, installed before loading data or renderers.
 * Local defaults use the same origin as `fxtr view`; VITE_* variables override them at build time.
 * Consumers read this configuration when used, not at module import time. */

export interface ViewerConfig {
  readonly graphqlUrl: string
  readonly entitiesUrl: string
  readonly viewsBase: string | null
  readonly sourceBase: string | null
  readonly fileUrl: string | null
  readonly experimentReadmeUrl: string | null
  readonly eventsUrl: string | null
  /** A step attempt's latest preview as DAG-CBOR bytes (debug_web's
   *  `GET /api/previews/{attemptId}`); null in a viewer whose jobs do not run. */
  readonly previewsBase: string | null
  /** The commits the project's jobs ran at (debug_web's `GET /api/job_commits`), for the job
   *  picker's commit view; null in a viewer with no repository to read. */
  readonly jobCommitsUrl: string | null
  /** The starred jobs (debug_web's `/api/starred`, and `/api/starred/{jobId}` to star or unstar
   *  one); null in a viewer that keeps no stars. */
  readonly starredUrl: string | null
  /** Sent with every request; the only part of the configuration that changes while the page
   *  runs (`renewViewerHeaders`), so readers take it from `viewerConfig()` per request. */
  readonly headers: Readonly<Record<string, string>>
  readonly notice: string | null
  readonly detailsAction: { readonly label: string; readonly run: () => void } | null
  /** Called when the viewer first has something to show, for a page that frames it. */
  readonly onShown: (() => void) | null
}

export const LOCAL_CONFIG: ViewerConfig = {
  graphqlUrl: import.meta.env.VITE_GRAPHQL_HTTP ?? '/api/graphql/',
  entitiesUrl: import.meta.env.VITE_ENTITIES_URL ?? '/api/entities',
  viewsBase: import.meta.env.VITE_VIEWS_BASE ?? '/api/views',
  sourceBase: import.meta.env.VITE_SOURCE_BASE ?? '/api/source',
  fileUrl: import.meta.env.VITE_FILE_URL ?? '/api/file',
  experimentReadmeUrl: import.meta.env.VITE_EXPERIMENT_README_URL ?? '/api/experiment_readme',
  eventsUrl: import.meta.env.VITE_EVENTS_URL ?? '/api/events',
  previewsBase: import.meta.env.VITE_PREVIEWS_BASE ?? '/api/previews',
  jobCommitsUrl: import.meta.env.VITE_JOB_COMMITS_URL ?? '/api/job_commits',
  starredUrl: import.meta.env.VITE_STARRED_URL ?? '/api/starred',
  headers: {},
  notice: null,
  detailsAction: null,
  onShown: null,
}

/** Override the checkout path used for editor links when the editor runs on another machine. */
export const REPO_ROOT_OVERRIDE: string | undefined = import.meta.env.VITE_REPO_ROOT

let current: ViewerConfig = LOCAL_CONFIG

export function viewerConfig(): ViewerConfig {
  return current
}

/** Startup installs one configuration for the page. This does not reconfigure active loaders. */
export function configureViewer(config: ViewerConfig): void {
  current = Object.freeze(config)
}

/** Replace the request headers, keeping the rest of the configuration: a hosted viewer's bearer
 *  token expires while the page runs and the hub's page hands it a fresh one (hosted.ts). */
export function renewViewerHeaders(headers: Readonly<Record<string, string>>): void {
  current = Object.freeze({ ...current, headers: Object.freeze({ ...headers }) })
}
