import { memo, useMemo } from 'react'
import type { JobCommit, JobCommits, PlacedJob, Starred } from '../api/jobs'
import { keepCommits, layoutGraph, type GraphLine, type GraphRow } from '../lib/commitGraph'
import { formatTime } from '../lib/time'
import type { JobSummary } from '../types'
import { cx } from '../lib/cx'
import { JobRow } from './JobRow'
import styles from './JobPicker.module.css'

/** The width of one lane of the graph, the height of a commit's own line (`.commit-head`'s, a
 *  little under a job row's, so the line sits close over its jobs), and where on it the commit's
 *  dot sits. */
const LANE = 14
const HEAD_HEIGHT = 20
const DOT_Y = 10
const DOT_R = 4.5
/** The space above each commit but the first, wider than the space under its line, so the line
 *  reads as heading its own jobs rather than trailing the jobs above. It is inside the commit (its
 *  graph runs through it unbroken). */
const COMMIT_GAP = 10
/** How many `--branch-<n>` colours there are. */
const BRANCH_COLORS = 4

/** How a branch's commits and lines are coloured: each branch but the default a branch colour in
 *  order (`--branch-<n>`, repeating), and the default branch, like a commit on no branch, in ink. */
export function branchColors(branches: JobCommits['branches']): (branch: string | null) => string {
  const others = branches.filter((branch) => !branch.default).map((branch) => branch.name)
  return (branch) => {
    const index = branch === null ? -1 : others.indexOf(branch)
    return index === -1 ? 'var(--state-ink)' : `var(--branch-${(index % BRANCH_COLORS) + 1})`
  }
}

interface Props {
  history: JobCommits
  summaries: ReadonlyMap<string, JobSummary>
  starred: Starred | null
  /** Lower-cased search text; empty for none. */
  needle: string
  /** Only this branch's commits; null for every commit. */
  branch: string | null
  /** Show only one branch's commits; null to show every commit. */
  onBranch: (branch: string | null) => void
  /** Only this workflow's jobs; null for every job. */
  workflow: string | null
  currentId: string | null
  previewId: string | null
  now: number
  onPreview: (id: string) => void
  onOpen: (id: string) => void
  onStar?: (id: string, starred: boolean) => void
  /** Rename a starred job; undefined where jobs cannot be starred. */
  onRename?: (id: string, name: string) => void
}

/**
 * The commits the project's jobs ran at, newest first, each with its jobs under it and a graph of
 * how the commits connect down the left edge. A search keeps the jobs it matches (by workflow,
 * id, or starred name) and every job of a commit it matches (by message, hash, or branch). Over
 * the commits, a line names each branch with a job the search and workflow keep, in its colour;
 * clicking one shows that branch's commits alone, and clicking it again (or `Clear`) shows every
 * commit again. A commit's own line is a heading over its jobs, never to be taken for a job: grey and not to be
 * clicked, set off by a gap above it, and led by its short hash, which a dashed line joins to its
 * dot across the graph.
 */
export function JobCommitsView({ history, summaries, starred, needle, branch, onBranch, workflow, currentId, previewId, now, onPreview, onOpen, onStar, onRename }: Props) {
  const colorOf = useMemo(() => branchColors(history.branches), [history.branches])
  const shown = useMemo(() => {
    const jobsAt = new Map<string, PlacedJob[]>()
    for (const job of history.jobs) {
      const at = jobsAt.get(job.commit)
      if (at) at.push(job)
      else jobsAt.set(job.commit, [job])
    }
    const matchesCommit = (commit: JobCommit) =>
      !needle || commit.subject.toLowerCase().includes(needle) || commit.hash.startsWith(needle) || (commit.branch ?? '').toLowerCase().includes(needle)
    const matchesJob = (job: PlacedJob) =>
      job.workflowName.toLowerCase().includes(needle) || job.id.includes(needle) || (starred?.[job.id]?.name ?? '').toLowerCase().includes(needle)
    const jobsOf = (commit: JobCommit) => {
      const jobs = (jobsAt.get(commit.hash) ?? []).filter((job) => workflow === null || job.workflowName === workflow)
      return matchesCommit(commit) ? jobs : jobs.filter(matchesJob)
    }
    const withJobs = history.commits.filter((commit) => jobsOf(commit).length > 0)
    const matching = new Set(withJobs.map((commit) => commit.hash))
    const commits = keepCommits(history.commits, (commit) => matching.has(commit.hash) && (branch === null || commit.branch === branch))
    // Every branch the search and workflow leave a job on, whichever branch is chosen, so another
    // can be chosen; and the chosen one, so it can be cleared.
    const branches = history.branches.filter((each) => each.name === branch || withJobs.some((commit) => commit.branch === each.name))
    return { commits, jobsOf, branches, layout: layoutGraph(commits) }
  }, [history, needle, branch, workflow, starred])

  const width = shown.layout.lanes * LANE + 8

  const branchRow = shown.branches.length > 0 && (
    <div className={styles.branches} role="group" aria-label="Branches">
      <span className={styles.branchesTitle}>Branches</span>
      {shown.branches.map((each) => (
        <button
          key={each.name}
          className={cx(styles.branch, each.name === branch && styles.isChosen)}
          style={{ '--branch-color': colorOf(each.name) } as React.CSSProperties}
          aria-pressed={each.name === branch}
          title={each.name === branch ? `Showing only ${each.name}` : `Show only ${each.name}`}
          onClick={() => onBranch(each.name === branch ? null : each.name)}
        >
          {each.name}
        </button>
      ))}
      {branch !== null && (
        <button className={styles.clearFilter} onClick={() => onBranch(null)}>
          Clear
        </button>
      )}
    </div>
  )
  return (
    <>
      {branchRow}
      <div role="listbox" aria-label="Jobs by commit">
        {shown.commits.length === 0 && <div className={styles.none}>{needle || branch || workflow ? 'No jobs match' : history.hidden > 0 ? 'No jobs with available commits' : 'No jobs yet'}</div>}
        {shown.commits.map((commit, index) => {
          const jobs = shown.jobsOf(commit)
          const top = index === 0 ? 0 : COMMIT_GAP
          return (
            <div key={commit.hash} role="group" aria-label={commit.subject} className={styles.commit} style={{ paddingLeft: width, paddingTop: top }}>
              <CommitGraphRow row={shown.layout.rows[index]} width={width} top={top} color={colorOf(commit.branch)} colorOf={colorOf} />
              <div className={styles.commitHead} title={`${commit.hash} · ${formatTime(commit.time)}`} aria-hidden>
                <span className={styles.hash}>{commit.hash.slice(0, 7)}</span>
                <span className={styles.subject}>{commit.subject}</span>
              </div>
              {jobs.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  summary={summaries.get(job.id)}
                  starName={starred?.[job.id]?.name}
                  uncommitted={job.uncommitted}
                  current={job.id === currentId}
                  previewed={job.id === previewId}
                  now={now}
                  onPreview={onPreview}
                  onOpen={onOpen}
                  onStar={onStar}
                  onRename={onRename}
                />
              ))}
            </div>
          )
        })}
        {history.hidden > 0 && (
          <div className={styles.footnote}>
            {history.hidden === 1 ? '1 job is' : `${history.hidden} jobs are`} not shown here: {history.hidden === 1 ? 'its commit is' : 'their commits are'} not in
            this repository. The recent list has every job.
          </div>
        )}
        {history.truncated && <div className={styles.footnote}>Only the newest jobs are placed on commits. The recent list has every job.</div>}
      </div>
    </>
  )
}

/** One commit's part of the graph, as tall as the commit and its jobs, starting `top` above its
 *  own line (the gap over it): its dot on the commit's own line, a dashed line from the dot to the
 *  line's start, the lines into it from above, and the lines out of it and past it running to the
 *  bottom. The dashed line goes under the branches' lines it crosses. */
const CommitGraphRow = memo(function CommitGraphRow({ row, width, top, color, colorOf }: { row: GraphRow; width: number; top: number; color: string; colorOf: (branch: string | null) => string }) {
  const x = (lane: number) => lane * LANE + LANE / 2 + 2
  const stroke = (line: GraphLine) => colorOf(line.branch)
  const dotY = top + DOT_Y
  const headBottom = top + HEAD_HEIGHT
  return (
    <svg className={styles.graph} width={width} height="100%" aria-hidden>
      {/* To just short of the hash, which starts 8px into the commit's line. */}
      <line className={styles.leader} x1={x(row.lane) + DOT_R + 2} x2={width + 6} y1={dotY} y2={dotY} />
      {row.through.map((line) => (
        <line key={`through-${line.from}`} x1={x(line.from)} x2={x(line.from)} y1={0} y2="100%" stroke={stroke(line)} />
      ))}
      {row.into.map((line) => (
        <path key={`into-${line.from}`} d={curve(x(line.from), 0, x(line.to), dotY)} stroke={stroke(line)} />
      ))}
      {row.out.map((line) => (
        <g key={`out-${line.to}`} stroke={stroke(line)}>
          <path d={curve(x(line.from), dotY, x(line.to), headBottom)} />
          <line x1={x(line.to)} x2={x(line.to)} y1={headBottom} y2="100%" />
        </g>
      ))}
      <circle cx={x(row.lane)} cy={dotY} r={DOT_R} fill={color} stroke="var(--bg)" strokeWidth={1.5} />
    </svg>
  )
})

/** A line straight down, or an S-curve between two lanes. */
function curve(x1: number, y1: number, x2: number, y2: number): string {
  if (x1 === x2) return `M ${x1} ${y1} L ${x2} ${y2}`
  const mid = (y1 + y2) / 2
  return `M ${x1} ${y1} C ${x1} ${mid} ${x2} ${mid} ${x2} ${y2}`
}
