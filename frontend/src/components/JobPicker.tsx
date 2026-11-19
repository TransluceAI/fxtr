import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { ChevronDown, RotateCcwClock } from 'lucide-react'
import { toast } from 'sonner'
import { fetchJobPage, fetchJobWorkflows } from '../api/graphql'
import { fetchJobCommits, fetchStarred, setStarred, type Starred } from '../api/jobs'
import { useQuery } from '../api/browse'
import { formatRelative, formatTime } from '../lib/time'
import { useStableCallback } from '../lib/store'
import type { JobSummary } from '../types'
import { cx } from '../lib/cx'
import { JobCommitsView } from './JobCommitsView'
import { JobPreview } from './JobPreview'
import { JobRow } from './JobRow'
import { JobSearch } from './JobSearch'
import { useJobSummaries, useNearbyJobIds } from './jobPickerQueries'
import { RetryLine } from './Status'
import styles from './JobPicker.module.css'

interface Props {
  jobs: JobSummary[]
  selectedId: string | null
  /** Show a job, with its summary when the picker has read it, so the page need not read it again. */
  onSelect: (id: string, summary: JobSummary | undefined) => void
  /** Show a job at one of its root workflow's steps, by name; with its summary, as `onSelect`. */
  onOpenStep: (id: string, step: string, summary: JobSummary | undefined) => void
}

type View = 'commits' | 'recent' | 'starred'
const VIEW_KEY = 'fxtr-job-picker-view'
/** How long the pointer rests on a row before the preview opens, and how long after it leaves the
 *  picker the preview closes, in ms. */
export const PREVIEW_DELAY = 300
const PREVIEW_CLOSE_DELAY = 300
/** The narrowest window that shows the preview beside the list. */
const PREVIEW_MIN_WIDTH = 1080

/**
 * The current job's name, opening the job picker: the jobs in one of three tabs, by the commit each
 * ran at (where the backend reads the project's git repository), newest first, or starred (newest
 * star first). A click (or Enter) on a job switches to it. Resting the pointer on a job opens a
 * preview of it beside the list, which then follows the pointer from row to row until the
 * pointer leaves the picker; the arrow keys (or j and k) stepping from job to job open and move it
 * too. A window narrower than PREVIEW_MIN_WIDTH shows no preview. Opening the picker refreshes
 * the history and the summaries it needs; the commit list reads only rows in or near its viewport.
 *
 * A backend without the commit view (a hosted viewer) has no commit tab, and one without starred
 * jobs no stars and no starred tab.
 */
export function JobPicker({ jobs, selectedId, onSelect, onOpenStep }: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [needle, setNeedle] = useState('')
  const [wanted, setWanted] = useState<View>(() => {
    const kept = readView()
    return kept === 'recent' || kept === 'starred' ? kept : 'commits'
  })
  // An explicit choice in this opening can inspect even an empty commit view.
  const [picked, setPicked] = useState(false)
  const [branch, setBranch] = useState<string | null>(null)
  const [workflow, setWorkflow] = useState<string | null>(null)
  /** Whether the preview beside the list is shown. */
  const [pane, setPane] = useState(false)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const previewJob = useCallback((id: string) => {
    setPreviewId(id)
    setPane(true)
  }, [])
  const hover = useHoverPreview(pane ? previewId : null, previewJob, () => setPane(false))
  const selected = jobs.find((job) => job.id === selectedId) ?? null
  const now = useMinute()
  const [list, setList] = useState<HTMLDivElement | null>(null)

  useEffect(() => {
    const timer = setTimeout(() => setNeedle(query.trim().toLowerCase()), 150)
    return () => clearTimeout(timer)
  }, [query])

  const history = useQuery('job-commits', (signal) => fetchJobCommits(signal), { enabled: open })
  const stars = useStarred(open)
  // A tab the backend cannot fill falls back to the recent list; the starred one stays while the
  // stars load.
  const missingCommits = history.value && history.value.jobs.length === 0 && history.value.hidden > 0
  const view: View = wanted === 'starred' ? (stars.loaded && stars.starred === null ? 'recent' : 'starred') : wanted === 'commits' && history.value && (picked || !missingCommits) ? 'commits' : 'recent'
  const wide = useWide()
  const workflowNames = useQuery('job-workflows', (signal) => fetchJobWorkflows(signal), { enabled: open })
  const workflows = workflowNames.value ?? []
  // A filter naming what is no longer there filters nothing.
  const shownWorkflow = workflow !== null && workflows.includes(workflow) ? workflow : null
  const waitingForCommits = wanted === 'commits' && history.value === undefined && !history.error
  const recent = useRecentJobs(needle, shownWorkflow, open && view === 'recent' && !waitingForCommits)

  const shownBranch = history.value?.branches.some((each) => each.name === branch) ? branch : null
  // These are exactly the inputs that change which rows the commit list renders.
  const contents = useMemo(() => [history.value, needle, shownBranch, shownWorkflow, stars.starred],
    [history.value, needle, shownBranch, shownWorkflow, stars.starred])
  const nearbyIds = useNearbyJobIds(open && view === 'commits' ? list : null, contents)
  // Stars have no workflow/name metadata until their summaries load; read them only in that tab.
  const wantedIds = useMemo(() => view === 'starred' ? Object.keys(stars.starred ?? {}) : nearbyIds,
    [view, stars.starred, nearbyIds])
  const read = useJobSummaries(wantedIds, pane && wide && view !== 'recent' ? previewId : null, open)
  // Each tab uses its own current read. An inactive tab's retained page may be older.
  const summaries = useMemo(() => view === 'recent'
    ? new Map(recent.items.map((job) => [job.id, job]))
    : read.summaries, [view, read.summaries, recent.items])

  const choose = useStableCallback((id: string) => {
    onSelect(id, summaries.get(id))
    setOpen(false)
    setQuery('')
  })
  const chooseStep = useStableCallback((id: string, step: string) => {
    onOpenStep(id, step, summaries.get(id))
    setOpen(false)
    setQuery('')
  })
  const pickView = (next: View) => {
    // Only the commit view has branches to keep.
    if (next !== 'commits') setBranch(null)
    setWanted(next)
    setPicked(true)
    writeView(next)
  }
  const starJob = useCallback((id: string, starred: boolean) => stars.set(id, starred ? '' : null), [stars.set])
  const onStar = stars.starred === null ? undefined : starJob
  const onRename = stars.starred === null ? undefined : stars.set
  const tabs = ([['commits', 'By commit'], ['recent', 'Recent'], ['starred', 'Starred']] as const).filter(
    ([each]) => (each === 'commits' ? !!history.value : each === 'starred' ? stars.starred !== null || !!stars.error : true),
  )
  const preview = previewId ? summaries.get(previewId) : undefined
  /**
   * Up and down arrows (and j and k outside a text box) preview the job above or below, skipping
   * commits. The job previewed is focused, so Enter then opens it.
   */
  const step = (event: React.KeyboardEvent) => {
    const target = event.target as HTMLElement
    // A starred job's name box keeps its own keys.
    if (!event.currentTarget.contains(target) || target.dataset.rename !== undefined) return
    const typing = target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement
    const by =
      event.key === 'ArrowDown' || (event.key === 'j' && !typing) ? 1 : event.key === 'ArrowUp' || (event.key === 'k' && !typing) ? -1 : 0
    if (by === 0 || event.altKey || event.ctrlKey || event.metaKey || target instanceof HTMLSelectElement) return
    const rows = [...(list?.querySelectorAll<HTMLElement>('[data-job-id]') ?? [])]
    if (rows.length === 0) return
    event.preventDefault()
    let at = rows.indexOf(target)
    if (at === -1) at = rows.findIndex((row) => row.dataset.jobId === previewId)
    const next = rows[at === -1 ? (by > 0 ? 0 : rows.length - 1) : Math.min(rows.length - 1, Math.max(0, at + by))]
    next.focus({ preventScroll: true })
    next.scrollIntoView({ block: 'nearest' })
    previewJob(next.dataset.jobId!)
  }
  /** Whether a newer job of the shown job's workflow exists: read afresh each time the picker
   *  opens, so a job launched since counts too. */
  const older = ((selectedId !== null ? summaries.get(selectedId) : undefined) ?? selected)?.latest === false
  /** Enter in the search box opens the first job listed under it. */
  const openFirst = () => {
    const first = list?.querySelector<HTMLElement>('[data-job-id]')?.dataset.jobId
    if (first) choose(first)
  }

  return (
    <Popover.Root
      open={open}
      onOpenChange={(next) => {
        if (next) {
          history.reload()
          workflowNames.reload()
          stars.reload()
          setPane(false)
          setPreviewId(null)
          setPicked(false)
        }
        hover.reset()
        setOpen(next)
      }}
    >
      <Popover.Trigger asChild>
        <button className={styles.trigger} aria-label="Choose job" title={selected ? `${selected.workflowName} · ${selected.id} · ${formatTime(selected.createdAt)}${older ? ' · a newer job of this workflow exists' : ''}` : undefined}>
          <span className={styles.name}>
            <span className={styles.nameText}>{selected ? selected.workflowName : 'No job'}</span>
            {older && (
              <RotateCcwClock size={12} className={styles.older} aria-label="Not the latest job of this workflow" />
            )}
          </span>
          <span className={styles.sub}>{selected ? `${selected.id.slice(0, 8)} · ${formatRelative(selected.createdAt, now)}` : `${jobs.length} available`}</span>
          <ChevronDown className={styles.chevron} size={13} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className={cx('popover', styles.menu)}
          side="bottom"
          align="start"
          sideOffset={2}
          onPointerEnter={hover.enter}
          onPointerLeave={hover.leave}
          // Escape in a starred job's name box cancels the rename, and in the search box with its
          // workflows listed closes the list, not the picker.
          onEscapeKeyDown={(event) => {
            const target = event.target
            if (target instanceof HTMLElement && (target.dataset.rename !== undefined || target.getAttribute('aria-expanded') === 'true')) event.preventDefault()
          }}
        >
          <div className={styles.browse} onKeyDown={step}>
            <div className={styles.controls}>
              <JobSearch
                query={query}
                onQuery={setQuery}
                placeholder={view === 'commits' ? 'Filter jobs and commits…' : 'Filter jobs…'}
                workflows={workflows}
                workflow={shownWorkflow}
                onWorkflow={setWorkflow}
                onEnter={openFirst}
              />
              {tabs.length > 1 && (
                <div className={styles.segments} role="tablist" aria-label="How jobs are listed">
                  {tabs.map(([each, label]) => (
                    <button key={each} role="tab" aria-selected={view === each} className={cx(view === each && styles.isOn)} onClick={() => pickView(each)}>
                      {label}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className={styles.list} ref={setList} onPointerMove={hover.move}>
              {!!stars.error && <RetryLine error={stars.error} onRetry={stars.reload} className={styles.none} />}
              {view !== 'recent' && !!read.error && <RetryLine error={read.error} onRetry={read.reload} className={styles.none} />}
              <div data-jobs>
              {view === 'commits' && history.value ? (
                <JobCommitsView
                  history={history.value}
                  summaries={summaries}
                  starred={stars.starred}
                  needle={needle}
                  branch={shownBranch}
                  onBranch={setBranch}
                  workflow={shownWorkflow}
                  currentId={selectedId}
                  previewId={previewId}
                  now={now}
                  onPreview={previewJob}
                  onOpen={choose}
                  onStar={onStar}
                  onRename={onRename}
                />
              ) : view === 'starred' ? (
                <StarredJobs
                  starred={stars.starred}
                  summaries={summaries}
                  resolved={read.resolved}
                  error={stars.error || read.error}
                  needle={needle}
                  workflow={shownWorkflow}
                  currentId={selectedId}
                  previewId={previewId}
                  now={now}
                  onPreview={previewJob}
                  onOpen={choose}
                  onStar={onStar}
                  onRename={onRename}
                />
              ) : wanted === 'commits' && history.loading && history.value === undefined && !history.error ? (
                <div className={styles.none}>Loading…</div>
              ) : (
                <RecentJobs recent={recent} starred={stars.starred} currentId={selectedId} previewId={previewId} now={now} onPreview={previewJob} onOpen={choose} onStar={onStar} onRename={onRename} />
              )}
              </div>
            </div>
          </div>
          {pane && wide && (
          <div className={styles.side} role="region" aria-label="Preview">
            {preview ? (
              <JobPreview
                key={preview.id}
                job={preview}
                launchedAt={view === 'commits' ? history.value?.jobs.find((each) => each.id === preview.id)?.launchedAt : undefined}
                onOpenStep={chooseStep}
              />
            ) : null /* Blank while the job's summary loads, never an earlier job's. */}
          </div>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

interface RowHandlers {
  starred: Starred | null
  currentId: string | null
  previewId: string | null
  now: number
  onPreview: (id: string) => void
  onOpen: (id: string) => void
  onStar?: (id: string, starred: boolean) => void
  onRename?: (id: string, name: string) => void
}

/** The starred jobs, newest star first: those matching the search (by workflow, id, or the name
 *  starred under) and of the workflow chosen. A job shows once its summary has been read. */
function StarredJobs({ starred, summaries, resolved, error, needle, workflow, currentId, previewId, now, onPreview, onOpen, onStar, onRename }: RowHandlers & {
  summaries: ReadonlyMap<string, JobSummary>
  resolved: ReadonlySet<string>
  error: unknown
  needle: string
  workflow: string | null
}) {
  const entries = Object.entries(starred ?? {}).sort(([, a], [, b]) => b.starredAt.localeCompare(a.starredAt))
  const rows = entries.flatMap(([id, star]) => {
    const summary = summaries.get(id)
    if (!summary || (workflow !== null && summary.workflowName !== workflow)) return []
    const matches = !needle || summary.workflowName.toLowerCase().includes(needle) || id.includes(needle) || star.name.toLowerCase().includes(needle)
    return matches ? [{ summary, star }] : []
  })
  if (rows.length === 0 && error) return null // The failed read and retry are shown above the list.
  if (starred === null || (rows.length === 0 && entries.some(([id]) => !summaries.has(id) && !resolved.has(id)))) return <div className={styles.none}>Loading…</div>
  const missing = entries.filter(([id]) => resolved.has(id) && !summaries.has(id)).length
  return (
    <div role="listbox" aria-label="Starred jobs">
      {rows.length === 0 && <div className={styles.none}>{entries.length === 0 ? 'No starred jobs. Star a job to keep it here.' : needle || workflow ? 'No jobs match' : 'No starred jobs are available in this database.'}</div>}
      {rows.map(({ summary, star }) => (
        <JobRow
          key={summary.id}
          job={summary}
          summary={summary}
          starName={star.name}
          current={summary.id === currentId}
          previewed={summary.id === previewId}
          now={now}
          onPreview={onPreview}
          onOpen={onOpen}
          onStar={onStar}
          onRename={onRename}
        />
      ))}
      {missing > 0 && <div className={styles.footnote}>{missing === 1 ? '1 starred job is' : `${missing} starred jobs are`} unavailable in this database.</div>}
    </div>
  )
}

/** Every job, newest first, a page at a time. */
function RecentJobs({ recent, starred, currentId, previewId, now, onPreview, onOpen, onStar, onRename }: RowHandlers & { recent: RecentJobsState }) {
  return (
    <div role="listbox" aria-label="Jobs, newest first">
      {recent.items.map((job) => (
        <JobRow
          key={job.id}
          job={job}
          summary={job}
          starName={starred?.[job.id]?.name}
          current={job.id === currentId}
          previewed={job.id === previewId}
          now={now}
          onPreview={onPreview}
          onOpen={onOpen}
          onStar={onStar}
          onRename={onRename}
        />
      ))}
      {recent.loading && <div className={styles.none}>Loading…</div>}
      {!!recent.error && (
        <div className={styles.none}>
          <button onClick={recent.retry}>Retry jobs</button>
        </div>
      )}
      {!recent.loading && !recent.error && recent.items.length === 0 && <div className={styles.none}>No jobs match</div>}
      {recent.more && !recent.loading && (
        <button className={styles.more} onClick={recent.more}>
          Show more
        </button>
      )}
    </div>
  )
}

interface RecentJobsState {
  items: JobSummary[]
  loading: boolean
  error: unknown
  /** Reads the next page; undefined when there is none. */
  more: (() => void) | undefined
  retry: () => void
}

/** The jobs matching `search` (of `workflow` alone, when given), newest first: the first page, then each further one asked for.
 *  Enabling it (the picker opening on this list) or changing the search starts again. */
function useRecentJobs(search: string, workflow: string | null, enabled: boolean): RecentJobsState {
  const [state, setState] = useState<{ items: JobSummary[]; next: string | null; loading: boolean; error: unknown }>({ items: [], next: null, loading: false, error: null })
  const [request, setRequest] = useState<{ after: string | null; attempt: number }>({ after: null, attempt: 0 })
  useEffect(() => setRequest((previous) => ({ after: null, attempt: previous.attempt + 1 })), [search, workflow, enabled])
  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    setState((previous) => ({ ...previous, loading: true, error: null }))
    fetchJobPage(search, request.after, 25, controller.signal, workflow).then(
      (page) => setState((previous) => ({ items: request.after === null ? page.items : [...previous.items, ...page.items], next: page.next, loading: false, error: null })),
      (error: unknown) => {
        if (!controller.signal.aborted) setState((previous) => ({ ...previous, loading: false, error }))
      },
    )
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `search`, `workflow` and `enabled` restart through `request`
  }, [request])
  const next = state.next
  return {
    items: state.items,
    loading: state.loading,
    error: state.error,
    more: next === null ? undefined : () => setRequest((previous) => ({ after: next, attempt: previous.attempt })),
    retry: () => setRequest((previous) => ({ ...previous, attempt: previous.attempt + 1 })),
  }
}

/** The starred jobs, read when the picker opens, and how to star, rename, or unstar one. `starred`
 *  is null where the backend keeps none. */
function useStarred(open: boolean) {
  const read = useQuery('starred-jobs', (signal) => fetchStarred(signal), { enabled: open })
  const [starred, setLocal] = useState<Starred | null>(null)
  useEffect(() => {
    if (read.value !== undefined) setLocal(read.value)
  }, [read.value])
  const set = useCallback((id: string, name: string | null) => {
    // Shown at once, then replaced by what the backend kept.
    setLocal((previous) => {
      if (previous === null) return previous
      const { [id]: existing, ...rest } = previous
      return name === null ? rest : { ...rest, [id]: { name, starredAt: existing?.starredAt ?? new Date().toISOString() } }
    })
    setStarred(id, name).then(setLocal, (error: unknown) => {
      toast.error('Could not save starred jobs', { description: error instanceof Error ? error.message : String(error) })
      read.reload()
    })
  }, [read.reload])
  /** Whether the backend has answered: until then `starred` is null without meaning it keeps none. */
  return { starred, loaded: read.value !== undefined, error: read.error, set, reload: read.reload }
}

/**
 * The preview's pointer handling, given the job the preview shows, or null while it is hidden. With the preview hidden, the pointer resting on a row for
 * PREVIEW_DELAY opens it; while it is shown, it follows the pointer onto each row at once. The
 * pointer leaving the picker hides it, after PREVIEW_CLOSE_DELAY so overshooting an edge does not.
 * Only real movement counts: rows scrolling under a still pointer (as the arrow keys scroll the
 * list) leave the preview with the keys.
 */
function useHoverPreview(shown: string | null, show: (id: string) => void, hide: () => void) {
  const opening = useRef<{ id: string; timer: number } | null>(null)
  const closing = useRef<number | null>(null)
  const last = useRef<{ x: number; y: number } | null>(null)
  const cancelOpening = () => {
    if (opening.current) window.clearTimeout(opening.current.timer)
    opening.current = null
  }
  const cancelClosing = () => {
    if (closing.current !== null) window.clearTimeout(closing.current)
    closing.current = null
  }
  useEffect(() => () => {
    cancelOpening()
    cancelClosing()
  }, [])
  return {
    move: (event: React.PointerEvent) => {
      if (event.pointerType === 'touch') return
      if (last.current && last.current.x === event.clientX && last.current.y === event.clientY) return
      last.current = { x: event.clientX, y: event.clientY }
      // Anything but a job (a commit's line, the gaps) leaves the preview as it is.
      const id = (event.target as HTMLElement).closest<HTMLElement>('[data-job-id]')?.dataset.jobId
      if (id === undefined) {
        cancelOpening()
        return
      }
      if (shown !== null) {
        cancelOpening()
        if (shown !== id) show(id)
        return
      }
      if (opening.current?.id === id) return
      cancelOpening()
      opening.current = {
        id,
        timer: window.setTimeout(() => {
          opening.current = null
          show(id)
        }, PREVIEW_DELAY),
      }
    },
    enter: cancelClosing,
    leave: () => {
      cancelOpening()
      cancelClosing()
      last.current = null
      closing.current = window.setTimeout(() => {
        closing.current = null
        hide()
      }, PREVIEW_CLOSE_DELAY)
    },
    reset: () => {
      cancelOpening()
      cancelClosing()
      last.current = null
    },
  }
}

/** Whether the window is wide enough for the preview beside the list (PREVIEW_MIN_WIDTH). */
function useWide(): boolean {
  const query = `(min-width: ${PREVIEW_MIN_WIDTH}px)`
  // A window without media queries (jsdom) counts as wide.
  const [wide, setWide] = useState(() => window.matchMedia?.(query).matches ?? true)
  useEffect(() => {
    if (!window.matchMedia) return
    const media = window.matchMedia(query)
    const update = () => setWide(media.matches)
    media.addEventListener('change', update)
    update()
    return () => media.removeEventListener('change', update)
  }, [query])
  return wide
}

/** The time, for relative times: read again every half minute. */
function useMinute(): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])
  return now
}

function readView(): string | null {
  try {
    return localStorage.getItem(VIEW_KEY)
  } catch {
    return null
  }
}

function writeView(view: View) {
  try {
    localStorage.setItem(VIEW_KEY, view)
  } catch {
    // The choice then lasts only for this page.
  }
}
