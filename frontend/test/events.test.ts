// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import {
  connectViewerEvents,
  eventsSocketUrl,
  onViewerEvent,
  type EventSocket,
  type ViewerEvent,
} from '../src/api/events'

/** A socket the test plays the server of. */
class FakeSocket implements EventSocket {
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  closed = false
  constructor(readonly url: string) {}
  close() {
    this.closed = true
  }
  send(data: unknown) {
    const text = typeof data === 'string' ? data : JSON.stringify(data)
    this.onmessage?.(new MessageEvent('message', { data: text }))
  }
  open() {
    this.onopen?.(new Event('open'))
  }
  drop() {
    this.onclose?.(new CloseEvent('close'))
  }
}

function connect(): { sockets: FakeSocket[]; stop: () => void } {
  const sockets: FakeSocket[] = []
  const stop = connectViewerEvents((url) => {
    const socket = new FakeSocket(url)
    sockets.push(socket)
    return socket
  })
  return { sockets, stop }
}

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('opens the socket at the page’s own address', () => {
  expect(eventsSocketUrl('http://127.0.0.1:18000/#job=j')).toBe('ws://127.0.0.1:18000/api/events')
  expect(eventsSocketUrl('https://viewer.example/')).toBe('wss://viewer.example/api/events')
  const { sockets, stop } = connect()
  expect(sockets.map((s) => s.url)).toEqual([eventsSocketUrl()])
  stop()
})

it('hands each event to every listener, and ignores what is not JSON', () => {
  const heard: ViewerEvent[] = []
  const stopHearing = onViewerEvent((event) => heard.push(event))
  const { sockets, stop } = connect()
  const job: ViewerEvent = { type: 'job', jobId: 'j', workflowName: 'w', resumed: false }
  sockets[0].send(job)
  sockets[0].send('not json')
  stopHearing()
  sockets[0].send(job)
  expect(heard).toEqual([job])
  stop()
})

it('reconnects, waiting longer after each failure, and at once when the page comes back', () => {
  vi.useFakeTimers()
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible')
  const { sockets, stop } = connect()
  sockets[0].drop()
  vi.advanceTimersByTime(499)
  expect(sockets).toHaveLength(1)
  vi.advanceTimersByTime(1)
  expect(sockets).toHaveLength(2)
  sockets[1].drop()
  vi.advanceTimersByTime(999)
  expect(sockets).toHaveLength(2)
  vi.advanceTimersByTime(1)
  expect(sockets).toHaveLength(3)
  sockets[2].open() // connected: the next wait starts over
  sockets[2].drop()
  vi.advanceTimersByTime(500)
  expect(sockets).toHaveLength(4)
  sockets[3].drop() // a second's wait, cut short by the page coming back into view
  document.dispatchEvent(new Event('visibilitychange'))
  expect(sockets).toHaveLength(5)
  vi.advanceTimersByTime(60_000)
  expect(sockets).toHaveLength(5)
  stop()
})

it('stops for good: the socket is closed and not reopened', () => {
  vi.useFakeTimers()
  const { sockets, stop } = connect()
  stop()
  expect(sockets[0].closed).toBe(true)
  sockets[0].drop()
  document.dispatchEvent(new Event('visibilitychange'))
  vi.advanceTimersByTime(60_000)
  expect(sockets).toHaveLength(1)
})
