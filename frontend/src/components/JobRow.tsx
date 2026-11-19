import { memo, useRef, useState } from 'react'
import { Star } from 'lucide-react'
import { shortName } from 'fxtr-view-cbor-tree'
import { jobGlyph } from '../lib/progress'
import { formatRelative, formatTime } from '../lib/time'
import type { JobSummary } from '../types'
import { cx } from '../lib/cx'
import { StateGlyph } from './StateGlyph'
import styles from './JobPicker.module.css'

/** What a row needs of a job before its summary has loaded. */
export interface JobRef {
  id: string
  workflowName: string
  createdAt: string
}

interface Props {
  job: JobRef
  /** Its state, failure, and inputs; undefined while they load. */
  summary: JobSummary | undefined
  /** The name it was starred under (empty for none); undefined when it is not starred. */
  starName: string | undefined
  /** Launched with staged changes that were never committed. */
  uncommitted?: boolean
  /** The job the page shows. */
  current: boolean
  previewed: boolean
  now: number
  onPreview: (id: string) => void
  onOpen: (id: string) => void
  /** Star or unstar it; undefined where jobs cannot be starred. */
  onStar?: (id: string, starred: boolean) => void
  /** Rename it, when starred; undefined where jobs cannot be starred. */
  onRename?: (id: string, name: string) => void
}

/**
 * One job in the picker: its workflow's name, how it went (nothing for a job that succeeded; a
 * spinner while it runs, `Failed in <step>`, or `Stopped`), its star, and when it was launched. A click, or Enter, opens it; Space previews it. A starred job's name (the one
 * it was starred under) is gold beside its star; clicking it, or starring the job, turns it into a
 * box: Enter or leaving the box keeps the name typed, Escape the one it had. A starred job with no
 * name shows a faint `Name` there on hover to click instead.
 */
export const JobRow = memo(function JobRow({ job, summary, starName, uncommitted = false, current, previewed, now, onPreview, onOpen, onStar, onRename }: Props) {
  const starred = starName !== undefined
  /** The name being typed while renaming; null otherwise. */
  const [draft, setDraft] = useState<string | null>(null)
  // Whether the rename is still open: the box losing focus as it goes, after Enter or Escape
  // finished it, must not finish it again.
  const renaming = useRef(false)
  const row = useRef<HTMLDivElement>(null)
  const startRename = (name: string) => {
    renaming.current = true
    setDraft(name)
  }
  const finish = (keep: boolean) => {
    if (!renaming.current || draft === null) return
    renaming.current = false
    if (keep && draft.trim() !== (starName ?? '')) onRename?.(job.id, draft.trim())
    setDraft(null)
    // Focus stays in the picker, on the row, rather than falling to the page.
    row.current?.focus({ preventScroll: true })
  }
  return (
    <div
      ref={row}
      role="option"
      aria-selected={previewed}
      aria-current={current ? 'true' : undefined}
      tabIndex={0}
      data-job-id={job.id}
      className={cx(styles.job, current && styles.isCurrent, previewed && styles.isPreviewed)}
      title={`${job.workflowName} · ${job.id} · ${formatTime(job.createdAt)}`}
      onClick={() => {
        if (draft === null) onOpen(job.id)
      }}
      onKeyDown={(event) => {
        if (draft !== null) return
        if (event.key === 'Enter') onOpen(job.id)
        if (event.key === ' ') {
          event.preventDefault()
          onPreview(job.id)
        }
      }}
    >
      <span className={styles.jobName}>{job.workflowName}</span>
      {uncommitted && <span className={styles.note}>Uncommitted changes</span>}
      {summary && <JobOutcome job={summary} />}
      <span className={styles.actions}>
        {draft !== null ? (
          <input
            className={styles.rename}
            data-rename
            autoFocus
            value={draft}
            placeholder="Name this job…"
            aria-label="Name of the starred job"
            onFocus={(event) => event.currentTarget.select()}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => finish(true)}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === 'Enter') finish(true)
              if (event.key === 'Escape') finish(false)
            }}
          />
        ) : (
          starred &&
          onRename && (
            <button
              className={cx(styles.starLabel, !starName && styles.isEmpty)}
              title={starName ? 'Rename' : 'Name this starred job'}
              onClick={(event) => {
                event.stopPropagation()
                startRename(starName)
              }}
            >
              {starName || 'Name'}
            </button>
          )
        )}
        {onStar && (
          <button
            className={cx(styles.star, starred && styles.isStarred)}
            aria-label={starred ? 'Remove star' : 'Star job'}
            aria-pressed={starred}
            title={starred ? 'Remove star' : 'Star this job'}
            onClick={(event) => {
              event.stopPropagation()
              onStar(job.id, !starred)
              // A job just starred asks for its name at once; one unstarred drops a name being typed.
              if (!starred && onRename) startRename('')
              else {
                renaming.current = false
                setDraft(null)
              }
            }}
          >
            <Star size={12} fill={starred ? 'currentColor' : 'none'} />
          </button>
        )}
      </span>
      <span className={styles.jobTime}>{formatRelative(job.createdAt, now)}</span>
    </div>
  )
})

/** How a failed job failed: `Failed in` the steps it failed in, by their short names. */
export function failureText(job: JobSummary): string {
  return job.failedSteps.length > 0 ? `Failed in ${job.failedSteps.map(shortName).join(', ')}` : 'Failed'
}

/** A spinner while a job runs; nothing once it has finished, its outcome being in words where it
 *  did not succeed (JobOutcome). */
function JobState({ job }: { job: JobSummary }) {
  if (job.state !== 'running') return null
  return <StateGlyph glyph={jobGlyph(job.state, job.failed)} spin />
}

/** How a job went, on the right of its row: nothing for one that succeeded, a spinner while it
 *  runs, then `Failed in <step>` once any of it failed, or `Stopped` for one stopped without
 *  failing. */
function JobOutcome({ job }: { job: JobSummary }) {
  return (
    <>
      {job.state === 'running' && (
        <span className={styles.jobState}>
          <JobState job={job} />
        </span>
      )}
      {job.failed ? (
        <span className={styles.failure} title={failureText(job)}>
          {failureText(job)}
        </span>
      ) : (
        job.state === 'stopped' && <span className={styles.stopped}>Stopped</span>
      )}
    </>
  )
}
