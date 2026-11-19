/** `1m05s` or `12s` between two ISO timestamps; null when either is missing. */
export function formatDuration(from: string | null, to: string | null): string | null {
  if (!from || !to) return null
  return formatSeconds((Date.parse(to) - Date.parse(from)) / 1000)
}

/** How long something has run: from `from` to `to`, or to `now` while it is still going. */
export function formatElapsed(from: string | null, to: string | null, now: number): string | null {
  if (!from) return null
  return formatSeconds(((to ? Date.parse(to) : now) - Date.parse(from)) / 1000)
}

/** `1h05m`, `1m05s` or `12s`. */
export function formatSeconds(seconds: number): string {
  const total = Math.max(0, Math.round(seconds))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h) return `${h}h${String(m).padStart(2, '0')}m`
  return m ? `${m}m${String(s).padStart(2, '0')}s` : `${s}s`
}

/** A timestamp in the viewer's locale, to the second. */
export function formatTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

/** A rough duration, for an estimate: `~40s` (to 5 seconds), `~6 min`, `~2h05m`. */
export function formatEstimate(seconds: number): string {
  if (seconds < 120) return `~${Math.max(5, Math.round(seconds / 5) * 5)}s`
  if (seconds < 3600) return `~${Math.round(seconds / 60)} min`
  return `~${formatSeconds(Math.round(seconds / 60) * 60)}`
}

/** A time of day in the viewer's locale, to the minute (`Date.now()`-style milliseconds). */
export function formatClock(ms: number): string {
  return new Date(ms).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/**
 * When something happened, as someone would say it: `just now`, `2 min ago`, `3 hr ago` (the same
 * day, or within twelve hours), `yesterday`, then the date (`Oct 14`, with the year once it is not
 * this one). `now` is `Date.now()`-style milliseconds.
 */
export function formatRelative(iso: string | null, now: number): string {
  if (!iso) return '—'
  const then = new Date(iso)
  if (Number.isNaN(then.getTime())) return iso
  const seconds = (now - then.getTime()) / 1000
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  const today = new Date(now)
  const days = Math.round((startOfDay(today) - startOfDay(then)) / 86_400_000)
  if (days === 0 || seconds < 12 * 3600) return `${Math.floor(seconds / 3600)} hr ago`
  if (days === 1) return 'yesterday'
  const sameYear = then.getFullYear() === today.getFullYear()
  return then.toLocaleDateString('en-US', { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }) })
}

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}
