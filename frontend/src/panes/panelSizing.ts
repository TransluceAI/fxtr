/**
 * How tall the panels in a column are. Two ways, chosen by `PANEL_SIZING`:
 *
 * - `grow`: each panel is as tall as its content, however tall; the column scrolls, and a panel's
 *   head sticks to its top. Nothing scrolls inside a panel.
 * - `fit`: the panels never add up to more than the column. While their content fits, each is as
 *   tall as its content. Once it does not, each panel is capped (`allotHeights`): short ones keep
 *   their height and the tall ones share the rest evenly, scrolling inside; and the boundaries
 *   between them can be dragged, which fixes the heights of the column's panels until a panel is
 *   added to or removed from it.
 *
 * In either, a collapsed panel is its head alone.
 */

export type PanelSizing = 'fit' | 'grow'

export const PANEL_SIZING: PanelSizing = 'fit'

/** The least a capped panel is given, in px (a collapsed one is its head, whatever that is). */
export const MIN_PANEL_HEIGHT = 90

/** The scrollers a panel's content scrolls in, when capped: how much they hold beyond what they
 *  show is how much taller the panel would be uncapped. */
const SCROLLERS = '.detail-scroll, .table-scroll'

/** How tall a panel's card would be uncapped: as tall as it is, plus what its content's scrollers
 *  hide (the largest of them, as they are mostly nested in one another or alone), or as tall as
 *  content that does not shrink and is clipped instead (so a cap on it is not read back as its
 *  height, which would lift the cap). */
export function naturalHeight(card: HTMLElement): number {
  let hidden = 0
  for (const scroller of card.querySelectorAll<HTMLElement>(SCROLLERS))
    hidden = Math.max(hidden, scroller.scrollHeight - scroller.clientHeight)
  return Math.max(card.offsetHeight + hidden, card.scrollHeight)
}

/**
 * Each panel's cap in a column `available` px tall, or null for none: no caps while the panels'
 * natural heights fit. Otherwise a panel with a height fixed by a drag gets that; the rest share
 * what is left by water-filling (each panel shorter than an even share keeps its height, and the
 * others split the remainder evenly), none less than `MIN_PANEL_HEIGHT` (or its natural height,
 * when that is less). Collapsed panels are never capped.
 */
export function allotHeights(
  naturals: readonly number[],
  fixed: readonly (number | undefined)[],
  collapsed: readonly boolean[],
  available: number,
): (number | null)[] {
  const total = naturals.reduce((sum, height) => sum + height, 0)
  if (total <= available) return naturals.map(() => null)
  const caps: (number | null)[] = naturals.map(() => null)
  let rest = available
  const open: number[] = []
  naturals.forEach((natural, index) => {
    if (collapsed[index]) rest -= natural
    else if (fixed[index] !== undefined) {
      caps[index] = fixed[index]!
      rest -= fixed[index]!
    } else open.push(index)
  })
  const byHeight = [...open].sort((a, b) => naturals[a]! - naturals[b]!)
  for (let at = 0; at < byHeight.length; at++) {
    const index = byHeight[at]!
    const share = rest / (byHeight.length - at)
    const natural = naturals[index]!
    if (natural <= share) {
      rest -= natural
      continue
    }
    for (const capped of byHeight.slice(at)) caps[capped] = Math.max(Math.min(MIN_PANEL_HEIGHT, naturals[capped]!), share)
    break
  }
  return caps
}
