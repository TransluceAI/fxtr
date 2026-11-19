/**
 * The GraphQL documents the app reads, over plain `fetch`. There are no subscriptions: a job in
 * progress is kept current by polling overviews, and `invocationVersion` for complete records
 * (see live.ts).
 * Response shapes are hand-written to match the backend's `debug_web/schema.graphql`.
 */

import { viewerConfig } from '../config'
import type { ArrayTypeJson, InvocationStatus, JobSummary, KeyValue } from '../types'

/** A request in flight, shared by every caller that asked for the same document and variables
 *  while it was running; aborted once each of them has let it go. */
interface SharedRequest {
  promise: Promise<unknown>
  controller: AbortController
  callers: number
}

const inFlight = new Map<string, SharedRequest>()

/** Send a GraphQL document to the backend and answer its data. */
export type Send = (query: string, variables: Record<string, unknown>, signal: AbortSignal) => Promise<unknown>

/** Answers every request in the backend's place, sending to it through `send` as it sees fit. */
export type RequestMiddleware = (
  query: string,
  variables: Record<string, unknown>,
  signal: AbortSignal,
  send: Send,
) => Promise<unknown>

let middleware: RequestMiddleware | null = null

/** Install `next` in front of the backend for every later request, or none. Only the dev server's
 *  run replay (`src/debug/`) installs one. */
export function setRequestMiddleware(next: RequestMiddleware | null) {
  middleware = next
}

/**
 * Run a GraphQL document. Identical requests made while one is in flight (a node's card, panel
 * head and panel body each reading its selected run on the same pick or refresh) share
 * its one HTTP request. Aborting `signal` rejects this caller at once; the request itself is
 * aborted when every caller sharing it has aborted, and a later identical request starts afresh.
 */
export function gql<T>(query: string, variables: Record<string, unknown> = {}, signal?: AbortSignal): Promise<T> {
  if (signal?.aborted) return Promise.reject(signal.reason)
  const body = JSON.stringify({ query, variables })
  let shared = inFlight.get(body)
  if (!shared) {
    const controller = new AbortController()
    const entry: SharedRequest = {
      controller,
      callers: 0,
      promise: (middleware
        ? middleware(query, variables, controller.signal, send)
        : request(body, controller.signal)
      ).finally(() => {
        if (inFlight.get(body) === entry) inFlight.delete(body)
      }),
    }
    inFlight.set(body, entry)
    shared = entry
  }
  const joined = shared
  joined.callers += 1
  return new Promise<T>((resolve, reject) => {
    let settled = false
    const onAbort = () => {
      if (settled) return
      settled = true
      reject(signal!.reason)
      joined.callers -= 1
      if (joined.callers === 0) {
        joined.controller.abort()
        if (inFlight.get(body) === joined) inFlight.delete(body)
      }
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    joined.promise.then(
      (value) => {
        if (settled) return
        settled = true
        signal?.removeEventListener('abort', onAbort)
        resolve(value as T)
      },
      (error: unknown) => {
        if (settled) return
        settled = true
        signal?.removeEventListener('abort', onAbort)
        reject(error)
      },
    )
  })
}

const send: Send = (query, variables, signal) => request(JSON.stringify({ query, variables }), signal)

async function request(body: string, signal: AbortSignal): Promise<unknown> {
  const response = await fetch(viewerConfig().graphqlUrl, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...viewerConfig().headers },
    body,
    signal,
  })
  // Ariadne can send resolver errors with HTTP 400; keep their actionable message (e.g. expiry).
  const data = ((await response.json().catch(() => null)) ?? {}) as {
    data?: unknown
    errors?: { message: string }[]
    error?: { message?: unknown }
  }
  if (data.errors?.length) throw new Error(data.errors.map((e) => e.message).join('; '))
  if (!response.ok) {
    throw new Error(typeof data.error?.message === 'string'
      ? data.error.message
      : `GraphQL request failed: ${response.status} ${response.statusText}`)
  }
  if (data.data === undefined) throw new Error('GraphQL response carried no data')
  return data.data
}

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

export interface GqlNodeInput {
  label: string | number
  identity: string
}

export interface GqlNode {
  identity: string
  kind: string
  name: string | null
  type: ArrayTypeJson
  /** The node's parameters: run nodes carry `step_name` / `workflow_name`, `map_over`, `replicas`. */
  params: Record<string, unknown>
  inputs: GqlNodeInput[]
  valueCid: string | null
}

export interface GqlSpawn {
  nodeIdentity: string
  key: KeyValue[]
  childPathname: string | null
  stepCacheKey: string | null
  resultCid: string | null
  /** The spawned target's derived state: the child invocation's, or the step's `StepState`. */
  state: string
}

export interface GqlAttemptError {
  typeName: string
  message: string
}

/** Which code ran an attempt: the commit its job attempt's process pinned, and the job that ran it
 *  (another job's, for a step result reused from the cache). */
export interface GqlProvenance {
  jobId: string
  commit: string
  /** Whether fxtr made the commit from uncommitted changes, so it exists only under fxtr's pin ref. */
  ephemeral: boolean
}

export interface GqlAttempt {
  kind: string
  state: string
  startedAt: string
  finishedAt: string | null
  error: GqlAttemptError | null
  provenance: GqlProvenance
  events: { kind: string; identity: string }[]
}

export interface GqlInvocation {
  pathname: string
  workflowName: string
  state: string
  resultCid: string | null
  error: GqlAttemptError | null
  /** The code that produced the result: its completed attempt's; null until it has one. */
  provenance: GqlProvenance | null
  nodes: GqlNode[]
  spawns: GqlSpawn[]
  attempts: GqlAttempt[]
}

export type StateCounts = Record<'queued' | 'running' | 'completed' | 'failed' | 'interrupted' | 'conflict' | 'stopped', number>

/** When a run's invocations ran, counting only attempts the job ran itself; times are the
 *  database's, ISO. */
export interface GqlRunActivity {
  firstStartedAt: string | null
  lastFinishedAt: string | null
  /** How many invocations the job finished, completed or failed. */
  finished: number
  /** When the longest-running invocation still running started. */
  oldestRunningSince: string | null
}

/** A run's failed invocations sharing an error type, with one of their messages. */
export interface GqlFailureGroup {
  typeName: string
  message: string
  count: number
}

/** A compact graph node: replica names and status totals, with no replica value lists. */
export interface OverviewNode extends GqlNode {
  version: string
  mapOver: string[]
  replicaDims: string[]
  counts: StateCounts | null
  /** Null, like `failures`, for a node that was not run. */
  activity: GqlRunActivity | null
  /** Most frequent first. */
  failures: GqlFailureGroup[] | null
}

/** The trace's complete contract. Full invocation records belong to custom renderers. */
export interface LevelOverview extends InvocationStatus {
  pathname: string
  workflowName: string
  resultCid: string | null
  nodes: OverviewNode[]
  attemptCount: number
  /** The code version the latest attempt ran, as a CID: what code links and docs follow. Null
   *  before an attempt. */
  codeVersion: string | null
  returnedNode: string | null
  awaitingNode: string | null
  version: string
  /** The database's time when the overview was read: the clock its timestamps are on. */
  asOf: string
}

export interface GqlArrayRow {
  key: KeyValue[]
  /** The value as base64 DRISL bytes, decoded client-side. */
  value: string
}

export interface GqlStepPin {
  stepName: string
  requestCid: string
  state: string
  /** The step's result: the one the job took, else one an equal request left on offer. The step
   *  is `completed` with either. */
  resultCid: string | null
  /** Whether the job has taken `resultCid`. False for a result on offer (`resultNotTaken`), and
   *  without a result. */
  resultTaken: boolean
  /** The code that produced the result (for a cache hit, another job's); null without one. */
  provenance: GqlProvenance | null
  attempt: {
    state: string
    startedAt: string
    finishedAt: string | null
    error: GqlAttemptError | null
    provenance: GqlProvenance
  } | null
  /** The `ctx.log` lines of the attempt behind the pin, in order. */
  logs: GqlLogLine[]
  /** The latest `ctx.preview` of the attempt behind the pin, without its bytes; null if none. */
  preview: GqlPreview | null
}

/** A step attempt's latest preview: its bytes are at the viewer's `previewsBase` + `/{attemptId}`, and a newer
 *  preview has a larger `seq`. */
export interface GqlPreview {
  attemptId: string
  seq: number
  at: string
}

export interface GqlLogLine {
  seq: number
  message: string
  at: string
}

/** The timing and outcome of one invocation, from its step pin or child invocation. */
export type GqlInvocationStatus = InvocationStatus

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

const PROVENANCE_FIELDS = `jobId commit ephemeral`

const INVOCATION = `
  query ($jobId: ID!, $pathname: String!) {
    invocation(jobId: $jobId, pathname: $pathname) {
      pathname
      workflowName
      state
      resultCid
      error { typeName message }
      provenance { ${PROVENANCE_FIELDS} }
      nodes {
        identity
        kind
        name
        type
        params
        inputs { label identity }
        valueCid
      }
      spawns { nodeIdentity key childPathname stepCacheKey resultCid state }
      attempts {
        kind
        state
        startedAt
        finishedAt
        error { typeName message }
        provenance { ${PROVENANCE_FIELDS} }
        events { kind identity }
      }
    }
  }
`

const STEP = `
  query ($jobId: ID!, $cacheKey: String!) {
    step(jobId: $jobId, cacheKey: $cacheKey) {
      stepName
      requestCid
      state
      resultCid
      resultTaken
      provenance { ${PROVENANCE_FIELDS} }
      attempt { state startedAt finishedAt error { typeName message } provenance { ${PROVENANCE_FIELDS} } }
      logs { seq message at }
      preview { attemptId seq at }
    }
  }
`

export interface Page<T> { items: T[]; next: string | null }
export interface CountedPage<T> extends Page<T> { total: number }

export const SPAWN_FIELDS = `nodeIdentity key childPathname stepCacheKey resultCid state`
export const COUNT_FIELDS = `queued running completed failed interrupted conflict stopped`
const JOB_FIELDS = `id workflowName root createdAt state failed failedSteps inputs { name cid single text } changedInputs latest`

/** A summary as the app reads it: the lists are present even when the answer left them out. */
const jobSummary = (job: JobSummary): JobSummary => ({ ...job, failedSteps: job.failedSteps ?? [], inputs: job.inputs ?? [], changedInputs: job.changedInputs ?? [], latest: job.latest ?? true })

/** The jobs matching `search`, of `workflow` alone when it is given, newest first. */
export const fetchJobPage = (search = '', after: string | null = null, first = 25, signal?: AbortSignal, workflow: string | null = null) =>
  gql<{ jobSummaries: Page<JobSummary> }>(`query($search: String!, $after: String, $first: Int!, $workflow: String) {
    jobSummaries(search: $search, after: $after, first: $first, workflow: $workflow) { items { ${JOB_FIELDS} } next }
  }`, { search, after, first, workflow }, signal).then(({ jobSummaries: page }) => ({ ...page, items: page.items.map(jobSummary) }))

/** The names of the workflows the project has jobs of, in order. */
export const fetchJobWorkflows = (signal?: AbortSignal) =>
  gql<{ jobWorkflows: string[] }>('{ jobWorkflows }', {}, signal).then((data) => data.jobWorkflows)

export const fetchJobSummary = (id: string, signal?: AbortSignal) =>
  gql<{ jobSummary: JobSummary | null }>(`query($id: ID!) { jobSummary(id: $id) { ${JOB_FIELDS} } }`, { id }, signal)
    .then((data) => data.jobSummary && jobSummary(data.jobSummary))

/** The summaries of the jobs named (at most 200 a request), in the order named; a job the project
 *  does not have is left out. */
export const fetchJobSummariesByIds = (ids: readonly string[], signal?: AbortSignal) =>
  gql<{ jobSummariesByIds: JobSummary[] }>(`query($ids: [ID!]!) { jobSummariesByIds(ids: $ids) { ${JOB_FIELDS} } }`, { ids }, signal)
    .then((data) => data.jobSummariesByIds.map(jobSummary))

/** Under one run node of a job's root workflow: how many step results the job ran itself and how
 *  many it took from the cache. */
export interface NodeReuse { identity: string; ran: number; reused: number }

export const fetchJobReuse = (jobId: string, signal?: AbortSignal) =>
  gql<{ jobReuse: NodeReuse[] }>(`query($jobId: ID!) { jobReuse(jobId: $jobId) { identity ran reused } }`, { jobId }, signal)
    .then((data) => data.jobReuse)

export const LEVEL_OVERVIEW = `query($jobId: ID!, $pathname: String!) {
  levelOverview(jobId: $jobId, pathname: $pathname) {
    pathname workflowName state resultCid error { typeName message }
    startedAt finishedAt attemptCount codeVersion returnedNode awaitingNode version asOf
    nodes {
      identity kind name type params inputs { label identity } valueCid mapOver replicaDims version
      counts { ${COUNT_FIELDS} }
      activity { firstStartedAt lastFinishedAt finished oldestRunningSince }
      failures { typeName message count }
    }
  }
}`

export async function fetchOverview(jobId: string, pathname: string, previous?: LevelOverview): Promise<LevelOverview> {
  const { levelOverview: value } = await gql<{ levelOverview: LevelOverview | null }>(LEVEL_OVERVIEW, { jobId, pathname })
  if (!value) throw new Error(`no invocation ${pathname} in job ${jobId}`)
  return { ...value, nodes: inDependencyOrder(previous ? keepingOrderOf(previous.nodes, value.nodes) : value.nodes) }
}

/**
 * HACK: works around a backend bug, client-side. Remove this once the backend is fixed.
 *
 * The invocation's nodes in dependency order: every node after the nodes it reads.
 *
 * The backend's `load_nodes` orders nodes by `nodes.created_at`, which is the timestamp of the
 * transaction that inserted them, so nodes written together share a timestamp and come back in an
 * arbitrary order among themselves. The viewer numbers and lays out step cards straight from that
 * order, so cards can appear out of order.
 *
 * The edges carry the real order, so a stable topological sort recovers one: nodes whose inputs
 * are all placed are taken in the order the backend gave them, which leaves an already-correct
 * order untouched. Edges to nodes outside the list, and any node left over by a cycle, are ignored
 * so a malformed DAG still renders.
 *
 * Two reasons this is a stopgap and not the fix. It only corrects what this viewer shows, and any
 * other reader of `load_nodes` still gets the arbitrary order. And a DAG is only a partial order,
 * so this recovers *a* valid order, not the one the workflow body emitted: independent siblings
 * keep whatever order Postgres happened to return. The real fix is a monotonic tiebreaker column
 * on `nodes` (a per-invocation sequence) in the backend's `ORDER BY`.
 */
function inDependencyOrder<T extends GqlNode>(nodes: T[]): T[] {
  const present = new Set(nodes.map((node) => node.identity))
  const placed = new Set<string>()
  const ordered: T[] = []
  const pending = [...nodes]
  while (pending.length > 0) {
    const next = pending.findIndex((node) =>
      node.inputs.every((edge) => !present.has(edge.identity) || placed.has(edge.identity)),
    )
    if (next === -1) {
      ordered.push(...pending)
      break
    }
    const [node] = pending.splice(next, 1)
    ordered.push(node)
    placed.add(node.identity)
  }
  return ordered
}

/**
 * One workflow invocation as recorded. `previous`, an earlier fetch of the same invocation, fixes
 * the order of the nodes it already had: the backend's order among nodes sharing a timestamp is
 * arbitrary and can change between fetches, and cards must not reshuffle while a job is watched.
 * Nodes new since then follow in the backend's order, and `inDependencyOrder` runs over the result.
 */
export async function fetchInvocation(
  jobId: string,
  pathname: string,
  previous: GqlInvocation | null = null,
): Promise<GqlInvocation> {
  const data = await gql<{ invocation: GqlInvocation | null }>(INVOCATION, { jobId, pathname })
  if (!data.invocation) throw new Error(`no invocation ${pathname} in job ${jobId}`)
  // HACK: see keepingOrderOf. The backend's node order can change between fetches; pin it here.
  const nodes = previous ? keepingOrderOf(previous.nodes, data.invocation.nodes) : data.invocation.nodes
  // HACK: see inDependencyOrder. The backend's node order is unreliable; reorder it here.
  return { ...data.invocation, nodes: inDependencyOrder(nodes) }
}

/**
 * HACK: works around the same backend bug as `inDependencyOrder`, client-side. Remove it with that.
 *
 * `next` with the nodes `previous` had first, in `previous`'s order, then the rest in their own.
 * The backend's order among nodes written in one transaction is arbitrary and can differ between
 * two fetches of the same invocation, so without this the cards of a job in progress could swap
 * places on a refresh.
 */
function keepingOrderOf<T extends GqlNode>(previous: T[], next: T[]): T[] {
  const rank = new Map(previous.map((node, index) => [node.identity, index]))
  const known = next.filter((node) => rank.has(node.identity))
  known.sort((a, b) => rank.get(a.identity)! - rank.get(b.identity)!)
  return [...known, ...next.filter((node) => !rank.has(node.identity))]
}

/**
 * The version of each named invocation of a job, in one request (`null` for one the job does not
 * have). A version changes whenever the invocation's record would; it means nothing else.
 */
export async function fetchVersions(jobId: string, pathnames: readonly string[]): Promise<Map<string, string | null>> {
  if (pathnames.length === 0) return new Map()
  const params = pathnames.map((_, i) => `$p${i}: String!`).join(', ')
  const fields = pathnames.map((_, i) => `v${i}: invocationVersion(jobId: $j, pathname: $p${i})`).join('\n')
  const variables: Record<string, unknown> = { j: jobId }
  pathnames.forEach((pathname, i) => {
    variables[`p${i}`] = pathname
  })
  const data = await gql<Record<string, string | null>>(`query ($j: ID!, ${params}) {\n${fields}\n}`, variables)
  return new Map(
    pathnames.map((pathname, i) => {
      const version = data[`v${i}`]
      if (version === undefined) throw new Error(`the version query carried no answer for ${pathname}`)
      return [pathname, version]
    }),
  )
}

export async function fetchStep(jobId: string, cacheKey: string): Promise<GqlStepPin> {
  const data = await gql<{ step: GqlStepPin | null }>(STEP, { jobId, cacheKey })
  if (!data.step) throw new Error(`no step ${cacheKey} in job ${jobId}`)
  return data.step
}

/**
 * Whether a step has a result its job has not taken: the step ran to the end (it is `completed`)
 * and no workflow invocation of the job has used its output yet. Usually that lasts a moment, until
 * the invocation waiting on the step takes it. It lasts until the job is resumed when that
 * invocation ended first: another step of it failed, or the job was cancelled, while this one was
 * still running.
 */
export function resultNotTaken(pin: GqlStepPin): boolean {
  return pin.resultCid !== null && !pin.resultTaken
}

/** A step's status, from its pin's attempt. */
export function stepStatus(pin: GqlStepPin): GqlInvocationStatus {
  const attempt = pin.attempt
  return {
    state: pin.state,
    startedAt: attempt?.startedAt ?? null,
    finishedAt: attempt?.finishedAt ?? null,
    error: attempt?.error ?? null,
  }
}

/** A child workflow's status, from the last attempt of its invocation. */
export function invocationStatus(invocation: GqlInvocation): GqlInvocationStatus {
  const last = invocation.attempts[invocation.attempts.length - 1]
  return {
    state: invocation.state,
    startedAt: last?.startedAt ?? null,
    finishedAt: last?.finishedAt ?? null,
    error: invocation.error ?? last?.error ?? null,
  }
}
