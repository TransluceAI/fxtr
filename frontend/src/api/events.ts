/**
 * The viewer's events: one WebSocket per page to the backend's `/api/events` (debug_web's
 * events.py), on which `fxtr view` sends JSON events. `views` is the state of the project's view
 * bundle as `fxtr view` rebuilds it (`views/liveViews.ts` follows it); the server sends the current
 * one first on every connection, so a page that reconnects catches up. `job` is a job its launcher
 * just announced (`lib/launches.tsx` follows it).
 *
 * `onViewerEvent` hears them; `connectViewerEvents` opens the socket and reopens it whenever it
 * closes (the server restarting, or dropping a socket that fell behind), waiting half a second at
 * first and twice as long after each failure, up to ten, and at once when the page comes back into
 * view. It is a WebSocket rather than an HTTP stream because a browser opens at most six HTTP/1.1
 * connections to one origin across all its tabs: a stream held by every open page would leave none
 * for the pages' requests once six were open.
 */

import { EVENTS_URL } from '../config'
import type { ViewsEvent } from '../views/liveViews'

/** A job its launcher just announced: `resumed` when it ran before (it has an earlier attempt). */
export interface JobEvent {
  readonly type: 'job'
  readonly jobId: string
  readonly workflowName: string
  readonly resumed: boolean
}

export type ViewerEvent = ViewsEvent | JobEvent

type Listener = (event: ViewerEvent) => void

const listeners = new Set<Listener>()

/** Hear every event from now on; returns the function that stops. */
export function onViewerEvent(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

/** The part of a WebSocket the events use (tests pass their own). */
export interface EventSocket {
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent<string>) => void) | null
  onclose: ((event: CloseEvent) => void) | null
  close(): void
}

const FIRST_RETRY_MS = 500
const LAST_RETRY_MS = 10_000

/** The events socket's URL: `EVENTS_URL` against the page's address, as `ws:` or `wss:`. */
export function eventsSocketUrl(page: string = window.location.href): string {
  const url = new URL(EVENTS_URL, page)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.toString()
}

/** Open the events socket and keep it open; returns the function that closes it for good. */
export function connectViewerEvents(
  open: (url: string) => EventSocket = (url) => new WebSocket(url),
): () => void {
  let socket: EventSocket | null = null
  let retryMs = FIRST_RETRY_MS
  let retry: number | undefined
  let stopped = false

  const connect = () => {
    window.clearTimeout(retry)
    retry = undefined
    const opened = open(eventsSocketUrl())
    socket = opened
    opened.onopen = () => {
      retryMs = FIRST_RETRY_MS
    }
    opened.onmessage = (message) => dispatch(message.data)
    opened.onclose = () => {
      if (socket !== opened) return
      socket = null
      if (stopped) return
      retry = window.setTimeout(connect, retryMs)
      retryMs = Math.min(retryMs * 2, LAST_RETRY_MS)
    }
  }
  const reconnectWhenSeen = () => {
    if (document.visibilityState === 'visible' && socket === null && !stopped) connect()
  }
  document.addEventListener('visibilitychange', reconnectWhenSeen)
  connect()
  return () => {
    stopped = true
    window.clearTimeout(retry)
    document.removeEventListener('visibilitychange', reconnectWhenSeen)
    const closing = socket
    socket = null
    closing?.close()
  }
}

function dispatch(data: string): void {
  let event: ViewerEvent
  try {
    event = JSON.parse(data) as ViewerEvent
  } catch {
    return
  }
  for (const listener of [...listeners]) listener(event)
}
