/**
 * Following launches in a page. A launcher announces each job it launches or resumes on the
 * viewer's events (`api/events.ts`). A page that follows launches switches to that job, and a toast
 * says why, offering to go back to the job it showed before. A page where someone picked a job (in
 * the job picker, or by changing the address's `#job=`) stays on it, and its toast offers the new
 * job instead. Showing the new job from that toast makes the page follow launches again; going
 * back makes it stop. Whether the page follows is kept in sessionStorage, so it lasts as long as
 * the tab, reloads included.
 *
 * Launches in quick succession update one toast rather than stacking several, and its way back
 * stays the job shown before the first of them.
 */

import { useEffect, useRef } from 'react'
import { Rocket, RotateCw } from 'lucide-react'
import { toast } from 'sonner'
import { onViewerEvent, type JobEvent } from '../api/events'
import { useStableCallback } from './store'

/** The sessionStorage key saying someone picked the job this tab shows. */
const PICKED_KEY = 'fxtr-picked-job'

/** How long a toast stays while the page is in view (it waits while the page is hidden). */
const SWITCHED_MS = 8_000
const OFFERED_MS = 12_000

function readPicked(): boolean {
  try {
    return sessionStorage.getItem(PICKED_KEY) !== null
  } catch {
    return false
  }
}

function writePicked(picked: boolean): void {
  try {
    if (picked) sessionStorage.setItem(PICKED_KEY, '1')
    else sessionStorage.removeItem(PICKED_KEY)
  } catch {
    // Storage can be unavailable (a private window, blocked site data); the choice then lasts
    // for this page only.
  }
}

/** The toast about launches that is open: its id, and the job shown before the first of them. */
interface OpenToast {
  readonly id: string
  readonly before: string | null
}

let toasts = 0

function LaunchIcon({ resumed }: { resumed: boolean }) {
  const Icon = resumed ? RotateCw : Rocket
  return <Icon size={14} aria-hidden />
}

/**
 * Follow launches in this page. `shown` is the id of the job on screen (`null` while there is
 * none) and `show` switches the page to another. Returns `pick`, which shows a job someone picked,
 * and so stops the page following launches.
 */
export function useFollowLaunches(
  shown: string | null,
  show: (jobId: string) => void,
): (jobId: string) => void {
  const picked = useRef<boolean | null>(null)
  if (picked.current === null) picked.current = readPicked()
  const current = useRef(shown)
  current.current = shown
  const open = useRef<OpenToast | null>(null)

  const setPicked = (value: boolean) => {
    picked.current = value
    writePicked(value)
  }
  /** The open toast, or a new one whose way back is the job on screen now. */
  const opened = (): OpenToast => (open.current ??= { id: `launch-${++toasts}`, before: current.current })
  /** Forget the toast `id` once it closes (not a newer one opened since). */
  const closed = (id: string) => () => {
    if (open.current?.id === id) open.current = null
  }

  const hear = useStableCallback((launch: JobEvent) => {
    if (launch.jobId === current.current) return
    const { id, before } = opened()
    const common = {
      id,
      icon: <LaunchIcon resumed={launch.resumed} />,
      onDismiss: closed(id),
      onAutoClose: closed(id),
    }
    if (picked.current) {
      toast(launch.resumed ? 'a job was resumed' : 'a new job was launched', {
        ...common,
        description: (
          <>
            <code>{launch.workflowName}</code> · this tab stays on the job you picked
          </>
        ),
        action: {
          label: 'show',
          onClick: () => {
            closed(id)()
            setPicked(false)
            show(launch.jobId)
          },
        },
        duration: OFFERED_MS,
      })
      return
    }
    show(launch.jobId)
    toast(`showing the ${launch.resumed ? 'resumed' : 'new'} job`, {
      ...common,
      description: (
        <>
          <code>{launch.workflowName}</code> was just {launch.resumed ? 'resumed' : 'launched'}, so
          this tab switched to it
        </>
      ),
      // Always given, so updating a toast that offered one replaces it.
      action:
        before !== null && before !== launch.jobId
          ? {
              label: 'go back',
              onClick: () => {
                closed(id)()
                setPicked(true)
                show(before)
              },
            }
          : undefined,
      duration: SWITCHED_MS,
    })
  })

  useEffect(
    () =>
      onViewerEvent((event) => {
        if (event.type === 'job') hear(event)
      }),
    [hear],
  )

  return useStableCallback((jobId: string) => {
    if (open.current !== null) {
      toast.dismiss(open.current.id)
      open.current = null
    }
    setPicked(true)
    show(jobId)
  })
}
