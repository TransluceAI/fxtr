/**
 * What the job picker reads beside job summaries (`graphql.ts`): the commits the jobs ran at and
 * the starred jobs. Both are plain routes of the local viewer's backend. A backend without one
 * (the commit view needs the project's git repository) answers 404, and a viewer configured
 * without one (a hub's) has no address for it. These read both as `null`: the picker then leaves
 * that part out. Other failures throw so the caller can report them.
 */

import { viewerConfig } from '../config'

/** A commit shown in the commit view: one a job is shown under. */
export interface JobCommit {
  hash: string
  subject: string
  time: string
  /** Its parents among the shown commits (the commits between are left out). */
  parents: string[]
  /** The branch it is shown on; null for a commit no branch contains any more. */
  branch: string | null
}

/** A job as the commit view places it. */
export interface PlacedJob {
  id: string
  workflowName: string
  createdAt: string
  /** The commit it is shown under. */
  commit: string
  /** Launched with staged changes that were never committed as they were. */
  uncommitted: boolean
  /** The commit it launched at, where it is shown under another: the newest commit at or before
   *  it that changed the project's directory, in a repository the project is part of. */
  launchedAt?: { hash: string; subject: string } | null
}

/** debug_web's `git_history.read_history`, which says where each job is shown and why. */
export interface JobCommits {
  /** The branches some shown commit belongs to. */
  branches: { name: string; default: boolean }[]
  /** Newest first, a commit before its parents. */
  commits: JobCommit[]
  /** Newest first. */
  jobs: PlacedJob[]
  /** How many jobs ran at commits this repository does not have. */
  hidden: number
  /** Whether the project has more jobs than the newest ones placed here. */
  truncated: boolean
}

export async function fetchJobCommits(signal?: AbortSignal): Promise<JobCommits | null> {
  const { jobCommitsUrl, headers } = viewerConfig()
  if (jobCommitsUrl === null) return null
  const response = await fetch(jobCommitsUrl, { signal, headers })
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`reading the jobs' commits failed: ${response.status} ${response.statusText}`)
  return (await response.json()) as JobCommits
}

/** The starred jobs by job id, each with the name it was given (empty for none). */
export type Starred = Record<string, { name: string; starredAt: string }>

export async function fetchStarred(signal?: AbortSignal): Promise<Starred | null> {
  const { starredUrl, headers } = viewerConfig()
  if (starredUrl === null) return null
  const response = await fetch(starredUrl, { signal, headers })
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`reading the starred jobs failed: ${response.status} ${response.statusText}`)
  return ((await response.json()) as { jobs: Starred }).jobs
}

/** Star a job under `name` (renaming one already starred), or with `name` null remove its star;
 *  the starred jobs afterwards. */
export async function setStarred(jobId: string, name: string | null): Promise<Starred> {
  const { starredUrl, headers } = viewerConfig()
  if (starredUrl === null) throw new Error('jobs cannot be starred in this viewer')
  const response = await fetch(`${starredUrl}/${encodeURIComponent(jobId)}`, name === null
    ? { method: 'DELETE', headers }
    : { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify({ name }) })
  if (!response.ok) throw new Error((await response.text().catch(() => '')) || `starring the job failed: ${response.status}`)
  return ((await response.json()) as { jobs: Starred }).jobs
}
