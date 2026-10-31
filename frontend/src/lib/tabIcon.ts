import { useEffect } from 'react'
import type { Level } from '../types'
import { toneOf, workflowProgress, type Tone } from './progress'
import { arcPath } from './ring'

/** Each tone's colour on a light and on a dark tab strip: `styles/tokens.css`'s values, since an
 *  icon reads no stylesheet of the page. */
const TONES: Record<Tone, readonly [string, string]> = {
  ink: ['#1c1c1c', '#e4e4e1'],
  done: ['#a2a29e', '#6f6f6b'],
  quiet: ['#b4b4af', '#5f5f5b'],
  danger: ['#c93434', '#f06363'],
}

/** The unfilled part of the ring on a light and on a dark tab strip: faint black or white, so it
 *  takes the strip's colour and sets the filled arc off. */
const TRACK = ['rgba(0, 0, 0, 0.1)', 'rgba(255, 255, 255, 0.14)'] as const

const SIZE = 32
const STROKE = 5
const R = (SIZE - STROKE) / 2

/**
 * The tab's icon as a progress ring of `level`, the workflow on screen (`workflowProgress`): a
 * translucent track with an arc for how far it has got, coloured as its state glyph is (red for any
 * failure); and the page's blank icon back once the caller unmounts.
 */
export function useTabIcon(level: Level) {
  const href = ringIcon(toneOf(level.glyph), workflowProgress(level.steps.map((step) => step.progress), level.live))
  useEffect(() => {
    const link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
    if (!link) throw new Error('the page has no icon link (index.html)')
    const blank = link.href
    link.href = href
    return () => {
      link.href = blank
    }
  }, [href])
}

/** A ring `fraction` (0 to 1) full, in `tone`, as an SVG data URL. */
function ringIcon(tone: Tone, fraction: number): string {
  const [light, dark] = TONES[tone]
  const c = SIZE / 2
  const ring = `fill="none" stroke="currentColor" stroke-width="${STROKE}"`
  const arc = fraction >= 1 ? `<circle cx="${c}" cy="${c}" r="${R}" ${ring}/>`
    : fraction > 0 ? `<path d="${arcPath(c, c, R, fraction)}" ${ring} stroke-linecap="round"/>` : ''
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${SIZE}" height="${SIZE}" viewBox="0 0 ${SIZE} ${SIZE}">`
    + `<style>g{color:${light}}.track{stroke:${TRACK[0]}}`
    + `@media (prefers-color-scheme:dark){g{color:${dark}}.track{stroke:${TRACK[1]}}}</style>`
    + `<g><circle class="track" cx="${c}" cy="${c}" r="${R}" fill="none" stroke-width="${STROKE}"/>${arc}</g></svg>`
  return `data:image/svg+xml,${encodeURIComponent(svg)}`
}
