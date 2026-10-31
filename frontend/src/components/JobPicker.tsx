import { useEffect, useState } from 'react'
import * as Popover from '@radix-ui/react-popover'
import { Check, ChevronDown, Search } from 'lucide-react'
import { fetchJobPage } from '../api/graphql'
import { useQuery } from '../api/browse'
import { jobGlyph } from '../lib/progress'
import type { JobSummary } from '../types'
import { formatTime } from '../lib/time'
import { StateGlyph } from './StateGlyph'
import { cx } from '../lib/cx'
import styles from './JobPicker.module.css'

interface Props {
  jobs: JobSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
}

/**
 * The current job's name, opening a searchable list of every job to switch to. Each job in the
 * list leads with its state glyph (the text beside it stays the authority); the button shows none.
 * Opening the list refetches it.
 */
export function JobPicker({ jobs, selectedId, onSelect }: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const selected = jobs.find((job) => job.id === selectedId) ?? null

  const [pages, setPages] = useState<(string | null)[]>([null])
  const [needle, setNeedle] = useState('')
  const after = pages[pages.length - 1]
  useEffect(() => { const timer = setTimeout(() => { setNeedle(query); setPages([null]) }, 250); return () => clearTimeout(timer) }, [query])
  const results = useQuery(JSON.stringify(['job-page', needle, after, open]), (signal) => fetchJobPage(needle, after, 25, signal), {enabled: open})
  const shown = results.value?.items ?? []

  const choose = (id: string) => {
    onSelect(id)
    setOpen(false)
    setQuery('')
  }

  return (
    <Popover.Root
      open={open}
      onOpenChange={(next) => {
        if (next) results.reload()
        setOpen(next)
      }}
    >
      <Popover.Trigger asChild>
        <button className={styles.trigger} aria-label="Choose job" title={selected ? jobTitle(selected) : undefined}>
          <span className={styles.name}>
            {selected ? selected.workflowName : 'no job'}
          </span>
          <span className={styles.sub}>
            {selected ? `${selected.id.slice(0, 8)} · ${formatTime(selected.createdAt)}` : `${jobs.length} available`}
          </span>
          <ChevronDown className={styles.chevron} size={13} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className={cx('popover', styles.menu)} side="bottom" align="start" sideOffset={2}>
          <label className={styles.search}>
            <Search size={12} />
            <input
              autoFocus
              value={query}
              placeholder="Filter jobs…"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && shown.length > 0) choose(shown[0].id)
              }}
            />
          </label>
          <ul className={styles.list}>
            {results.loading && <li>loading…</li>}
            {!!results.error && <li><button onClick={results.reload}>retry jobs</button></li>}
            {shown.map((job) => (
              <li key={job.id}>
                <button
                  className={cx(styles.item, job.id === selectedId && styles.isSelected)}
                  aria-current={job.id === selectedId ? 'true' : undefined}
                  onClick={() => choose(job.id)}
                  title={jobTitle(job)}
                >
                  <span className={styles.itemName}>
                    <JobState job={job} reserve />
                    {job.workflowName}
                  </span>
                  <span className={styles.itemMeta}>
                    <span className={styles.itemId}>{job.id.slice(0, 8)}</span>
                    <span>{formatTime(job.createdAt)}</span>
                    <span className={cx(styles.state, job.failed && styles.isFailed)}>
                      {job.failed ? 'failed' : job.state}
                    </span>
                  </span>
                </button>
              </li>
            ))}
            {!results.loading && !results.error && shown.length === 0 && <li className={styles.none}>no jobs match</li>}
          </ul>
          <button disabled={pages.length === 1 || results.loading} onClick={() => setPages(pages.slice(0, -1))}>previous</button>
          <button disabled={!results.value?.next || results.loading} onClick={() => setPages([...pages, results.value!.next])}>next</button>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}

/** A job's state: a check for one that succeeded, else its state glyph. */
function JobState({ job, reserve = false }: { job: JobSummary; reserve?: boolean }) {
  if (job.state === 'succeeded') return <Check size={11} strokeWidth={2.2} className={styles.succeeded} aria-label="succeeded" />
  return <StateGlyph glyph={jobGlyph(job.state, job.failed)} reserve={reserve} />
}

const jobTitle = (job: JobSummary) => `${job.workflowName} · ${job.id} · ${formatTime(job.createdAt)}`
