/**
 * Where the backend lives and where code links point. Everything here is overridable with Vite env
 * vars (`VITE_…`, read at build time). The defaults are origin-relative: `fxtr view` serves the
 * built viewer and its API (under `/api`) from one origin, and the dev server proxies `/api` to it
 * (see vite.config.ts).
 */

// The trailing slash is the GraphQL mount's canonical path (see debug_web/app.py).
export const GRAPHQL_HTTP: string = import.meta.env.VITE_GRAPHQL_HTTP ?? '/api/graphql/'

/** Where the backend serves entities' bytes, many per request (debug_web's `POST /api/entities`). */
export const ENTITIES_URL: string = import.meta.env.VITE_ENTITIES_URL ?? '/api/entities'

/** Where the backend serves the project's view bundle (debug_web's `GET /api/views/{name}`). */
export const VIEWS_BASE: string = import.meta.env.VITE_VIEWS_BASE ?? '/api/views'

/**
 * The checkout the code links point into. When unset, the backend reports the repository it found
 * the file in, which is right when the backend and the editor share a machine.
 */
export const REPO_ROOT_OVERRIDE: string | undefined = import.meta.env.VITE_REPO_ROOT

/** Where the backend says a step or workflow is implemented (debug_web's `GET /api/source/{name}`). */
export const SOURCE_BASE: string = import.meta.env.VITE_SOURCE_BASE ?? '/api/source'

/** The project's EXPERIMENT.md (debug_web's `GET /api/experiment_readme`: the project root's file). */
export const EXPERIMENT_README_URL: string = import.meta.env.VITE_EXPERIMENT_README_URL ?? '/api/experiment_readme'

/** A file of the project's repository, at a commit or in the working tree (debug_web's
 *  `GET /api/file`), which the code pane shows. */
export const FILE_URL: string = import.meta.env.VITE_FILE_URL ?? '/api/file'

/**
 * The backend's events WebSocket (debug_web's `/api/events`): the state of the project's view
 * bundle as it is rebuilt, and the jobs launchers announce. `api/events.ts` opens it as `ws:` or
 * `wss:` against the page's address.
 */
export const EVENTS_URL: string = import.meta.env.VITE_EVENTS_URL ?? '/api/events'
