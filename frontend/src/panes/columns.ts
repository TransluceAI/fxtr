/**
 * The detail view's layout: columns, left to right, each a stack of panels top to bottom and a
 * width of its own (`DEFAULT_COLUMN_WIDTH` until resized), and the panels' **sequence**. Every
 * operation here is a pure rewrite; an emptied column is dropped. A panel is as tall as its
 * content, so heights are not part of it.
 *
 * **The sequence.** Every open panel has a place in one sequence (`prevId`: the panel before it).
 * A **browsing** panel follows the panel before it (the first one, the roots): it stores what it
 * shows relative to that panel's address (`rel`; after the roots, absolute) and the params it binds
 * itself (`own`), and its bindings are that panel's with `own` over them (`useColumns.ts`). A **pinned** panel stores its
 * address whole and every binding it has, so nothing changes it but its own controls; the browsing
 * panels after it follow it. A click in a view changes the browsing panel right after it, or opens
 * one there, just before a pinned panel or the end (`useColumns`). Closing a panel closes the
 * browsing panels after it, up to the next pinned one. Where a panel sits on screen never changes
 * the sequence.
 *
 * **Navigating** changes what a panel shows in place, remembering what it showed for back and
 * forward. When it moves to another kind of thing (`useColumns` decides), the browsing panels after
 * it close, and are kept with what it showed (`Visit`): going back there brings them back.
 */

import type { Params, Rel } from './address'

/** The roots of the viewer that open panels: the trace (the centre: cards and their tables, the
 *  flowchart, a view of the level's workflow; and the minimap), at the centre's
 *  address, and the rail (the experiment, pages), at none. Each binds the slice, and comes before every
 *  panel in the sequence. */
export const TRACE = '@trace'
export const RAIL = '@rail'
export function isRoot(id: string): boolean {
  return id === TRACE || id === RAIL
}

/** What a panel stores about what it shows, and what back and forward return to. */
export interface Stored {
  rel: Rel
  own: Params
}

/** `to` as a panel stores it, with nothing else of the object it was read from. */
function storedOf(to: Stored): Stored {
  return { rel: to.rel, own: to.own }
}

/** A step of a panel's history: what it stored, and the browsing panels that followed it there and
 *  closed when it navigated to another kind of thing, in sequence, each with where it was. */
export interface Visit extends Stored {
  followers: readonly Detached[]
}

/** A closed panel kept to be opened again, with the column it was in and its index there. */
export interface Detached {
  panel: PanelInstance
  columnId: string
  index: number
}

export interface PanelInstance extends Stored {
  id: string
  /** The panel before it in the sequence; null for the first, which comes after the roots. */
  prevId: string | null
  /** Changes only when the user changes it: its `rel` is absolute and its `own` every binding it
   *  has. Else it browses, following the panel before it. */
  pinned: boolean
  /** What it showed before (latest last) and after (next first) back and forward. */
  back: readonly Visit[]
  forward: readonly Visit[]
  /** Shrunk to its head. */
  collapsed?: boolean
}

/** How a browsing panel that is to follow `prevId` (null: the roots) in place of the panel it
 *  followed stores what it shows now. */
export type Rebase = (panel: PanelInstance, prevId: string | null) => Stored

export interface Column {
  id: string
  /** In px; never narrower than `MIN_COLUMN_WIDTH`. */
  width: number
  panels: readonly PanelInstance[]
}

export type Columns = readonly Column[]

/** How wide a new column is, in px. */
export const DEFAULT_COLUMN_WIDTH = 600
/** How narrow a column can be resized, in px. */
export const MIN_COLUMN_WIDTH = 400

/** Where a panel goes: into a column before the panel at `index` (its length: at the end), into a
 *  new column inserted at `index`, or in the place of an open panel. */
export type Placement =
  | { kind: 'into'; column: number; index: number }
  | { kind: 'newColumn'; index: number }
  | { kind: 'replace'; panelId: string }

let nextId = 0
export function newPanelId(): string {
  nextId += 1
  return `panel-${nextId}`
}

function newColumn(panel: PanelInstance): Column {
  nextId += 1
  return { id: `column-${nextId}`, width: DEFAULT_COLUMN_WIDTH, panels: [panel] }
}

/** Each column's panels rewritten by `change`, its id and width kept, an emptied column dropped. */
function mapPanels(columns: Columns, change: (panels: readonly PanelInstance[], column: number) => readonly PanelInstance[]): Columns {
  return columns.map((column, index) => ({ ...column, panels: change(column.panels, index) })).filter((column) => column.panels.length > 0)
}

export function locate(columns: Columns, panelId: string): { column: number; index: number } | null {
  for (let column = 0; column < columns.length; column++) {
    const index = columns[column]!.panels.findIndex((panel) => panel.id === panelId)
    if (index >= 0) return { column, index }
  }
  return null
}

export function findPanel(columns: Columns, panelId: string): PanelInstance | null {
  const at = locate(columns, panelId)
  return at ? columns[at.column]!.panels[at.index]! : null
}

export function allPanels(columns: Columns): PanelInstance[] {
  return columns.flatMap((column) => column.panels)
}

/** Every open panel, in sequence. */
export function sequence(columns: Columns): PanelInstance[] {
  const panels = allPanels(columns)
  const after = new Map<string | null, PanelInstance>()
  for (const panel of panels) {
    if (after.has(panel.prevId)) throw new Error(`two panels come after ${panel.prevId ?? 'the roots'}`)
    after.set(panel.prevId, panel)
  }
  const ordered: PanelInstance[] = []
  for (let next = after.get(null); next; next = after.get(next.id)) ordered.push(next)
  if (ordered.length !== panels.length) throw new Error('the panels are not one sequence')
  return ordered
}

/** Where view `viewId` is in `ordered`: -1 for a root, which comes before every panel. */
export function positionOf(ordered: readonly PanelInstance[], viewId: string): number {
  if (isRoot(viewId)) return -1
  const index = ordered.findIndex((panel) => panel.id === viewId)
  if (index < 0) throw new Error(`no panel ${viewId}`)
  return index
}

/** The browsing panels after `viewId` in `ordered`, up to the next pinned one: those following it. */
export function followersOf(ordered: readonly PanelInstance[], viewId: string): PanelInstance[] {
  const found: PanelInstance[] = []
  for (const panel of ordered.slice(positionOf(ordered, viewId) + 1)) {
    if (panel.pinned) break
    found.push(panel)
  }
  return found
}

/** Where each column starts, in px from the first one's left edge. */
export function columnLefts(columns: Columns): number[] {
  let left = 0
  return columns.map((column) => {
    const start = left
    left += column.width
    return start
  })
}

/** Resize a column (to no narrower than `MIN_COLUMN_WIDTH`); the columns right of it keep their
 *  widths, so they move. */
export function resizeColumn(columns: Columns, columnId: string, width: number): Columns {
  return columns.map((column) => (column.id === columnId ? { ...column, width: Math.max(MIN_COLUMN_WIDTH, width) } : column))
}

/** Close panels, and with each the browsing panels after it up to the next pinned one. Each panel
 *  left comes after the nearest panel before it left open. A `spare` panel stays open even among
 *  those after a closed one (a panel dropped in place of a panel it follows), and with it the
 *  panels after it; it follows the panel left before it instead (`rebase`). */
export function closePanels(columns: Columns, ids: ReadonlySet<string>, spare?: { id: string; rebase: Rebase }): Columns {
  const ordered = sequence(columns)
  const closing = new Set<string>()
  for (const id of ids) {
    closing.add(id)
    for (const follower of followersOf(ordered, id)) {
      if (follower.id === spare?.id) break
      closing.add(follower.id)
    }
  }
  const prevOf = new Map(ordered.map((panel) => [panel.id, panel.prevId]))
  const openBefore = (prevId: string | null): string | null => {
    let at = prevId
    while (at !== null && closing.has(at)) at = prevOf.get(at)!
    return at
  }
  return mapPanels(columns, (stack) => stack.filter((panel) => !closing.has(panel.id)).map((panel) => {
    const prevId = openBefore(panel.prevId)
    if (prevId === panel.prevId) return panel
    if (panel.pinned) return { ...panel, prevId }
    if (panel.id !== spare?.id) throw new Error(`browsing panel ${panel.id} lost the panel it follows`)
    return { ...panel, ...storedOf(spare.rebase(panel, prevId)), prevId, back: [], forward: [] }
  }))
}

/** Pin a panel where it is in the sequence, storing `stored`: its address whole and every binding
 *  it has now. Its history is dropped, since what it stored was relative to the panel before it.
 *  There is no unpinning: a pinned panel's place in the sequence says only when it was opened, not
 *  which view it would follow. */
export function pinPanel(columns: Columns, panelId: string, stored: Stored): Columns {
  return updatePanel(columns, panelId, (panel) => ({ ...panel, ...storedOf(stored), pinned: true, back: [], forward: [] }))
}

/** Put `panel` where `placement` says on screen, and nothing else: replacing a panel only puts it
 *  just after that one. */
function place(columns: Columns, placement: Placement, panel: PanelInstance): Columns {
  switch (placement.kind) {
    case 'into': {
      if (placement.column >= columns.length) return [...columns, newColumn(panel)]
      return mapPanels(columns, (stack, column) =>
        column === placement.column ? [...stack.slice(0, placement.index), panel, ...stack.slice(placement.index)] : stack,
      )
    }
    case 'newColumn': {
      const index = Math.min(placement.index, columns.length)
      return [...columns.slice(0, index), newColumn(panel), ...columns.slice(index)]
    }
    case 'replace': {
      const at = locate(columns, placement.panelId)
      if (!at) throw new Error(`no panel ${placement.panelId} to replace`)
      return mapPanels(columns, (stack, column) =>
        column === at.column ? [...stack.slice(0, at.index + 1), panel, ...stack.slice(at.index + 1)] : stack,
      )
    }
  }
}

/** Open `panel`: in the sequence after `panel.prevId` (the panel there before now coming after it),
 *  and on screen where `placement` says. Replacing a panel closes that one (`closePanels`). */
export function insertPanel(columns: Columns, placement: Placement, panel: PanelInstance): Columns {
  const linked = mapPanels(columns, (stack) => stack.map((other) => (other.prevId === panel.prevId ? { ...other, prevId: panel.id } : other)))
  const placed = place(linked, placement, panel)
  return placement.kind === 'replace' ? closePanels(placed, new Set([placement.panelId])) : placed
}

/** Move an open panel on screen to `placement`, which is read against the layout with the panel
 *  still in it; its place in the sequence stays. Replacing a panel closes that one (`closePanels`),
 *  sparing the moved one when it follows it. */
export function movePanel(columns: Columns, panelId: string, placement: Placement, rebase: Rebase): Columns {
  const panel = findPanel(columns, panelId)
  if (!panel) throw new Error(`no panel ${panelId} to move`)
  if (placement.kind === 'replace' && placement.panelId === panelId) return columns
  const from = locate(columns, panelId)!
  // Leave a hole in place of the panel (so indices stay as the placement read them), place it, then
  // drop the hole and any column it empties.
  const hole = `${panelId}:moving`
  const withHole = mapPanels(columns, (stack, column) =>
    column === from.column ? stack.map((p, index) => (index === from.index ? { ...p, id: hole } : p)) : stack,
  )
  const moved = mapPanels(place(withHole, placement, panel), (stack) => stack.filter((p) => p.id !== hole))
  return placement.kind === 'replace' ? closePanels(moved, new Set([placement.panelId]), { id: panelId, rebase }) : moved
}

export function updatePanel(columns: Columns, panelId: string, update: (panel: PanelInstance) => PanelInstance): Columns {
  return mapPanels(columns, (stack) => stack.map((panel) => (panel.id === panelId ? update(panel) : panel)))
}

export function toggleCollapsed(columns: Columns, panelId: string): Columns {
  return updatePanel(columns, panelId, (panel) => ({ ...panel, collapsed: !panel.collapsed }))
}

/** The browsing panels following `panelId`, closed and kept with where each was. */
function detachFollowers(columns: Columns, panelId: string): { columns: Columns; followers: Detached[] } {
  const followers = followersOf(sequence(columns), panelId).map((panel): Detached => {
    const at = locate(columns, panel.id)!
    return { panel, columnId: columns[at.column]!.id, index: at.index }
  })
  if (followers.length === 0) return { columns, followers }
  return { columns: closePanels(columns, new Set([followers[0]!.panel.id])), followers }
}

/** Open `followers` again after `panelId`, in sequence, each in the column it was in (at its index,
 *  or the end) while that column is open, else in a new column right of the panel before it. */
function restoreFollowers(columns: Columns, panelId: string, followers: readonly Detached[]): Columns {
  const replaced = new Map<string, string>()
  let current = columns
  let before = panelId
  for (const { panel, columnId, index } of followers) {
    const column = current.findIndex((candidate) => candidate.id === (replaced.get(columnId) ?? columnId))
    const placement: Placement = column >= 0
      ? { kind: 'into', column, index: Math.min(index, current[column]!.panels.length) }
      : { kind: 'newColumn', index: locate(current, before)!.column + 1 }
    current = insertPanel(current, placement, panel)
    if (column < 0) replaced.set(columnId, current[locate(current, panel.id)!.column]!.id)
    before = panel.id
  }
  return current
}

/** Navigate a panel to `to`, remembering what it showed for back. `leaving`: it moves to another
 *  kind of thing, so the browsing panels following it close, kept with what it showed. */
export function navigate(columns: Columns, panelId: string, to: Stored, leaving = false): Columns {
  const { columns: left, followers } = leaving ? detachFollowers(columns, panelId) : { columns, followers: [] }
  return updatePanel(left, panelId, (panel) => ({
    ...panel, ...storedOf(to), back: [...panel.back, { ...storedOf(panel), followers }], forward: [],
  }))
}

/** What a panel would show stepping back (`-1`) or forward (`1`); undefined at the end. */
export function historyStep(panel: PanelInstance, direction: -1 | 1): Visit | undefined {
  return direction === -1 ? panel.back.at(-1) : panel.forward[0]
}

/** Step a panel back (`-1`) or forward (`1`) through what it showed. `leaving`: what it steps to is
 *  another kind of thing than what it shows, so the browsing panels following it close, kept with
 *  what it showed, and those kept with what it steps to open again. */
export function stepHistory(columns: Columns, panelId: string, direction: -1 | 1, leaving = false): Columns {
  const panel = findPanel(columns, panelId)
  const to = panel && historyStep(panel, direction)
  if (!panel || !to) return columns
  const { columns: left, followers } = leaving ? detachFollowers(columns, panelId) : { columns, followers: [] }
  const here: Visit = { ...storedOf(panel), followers }
  const stepped = updatePanel(left, panelId, (current) => ({
    ...current, ...storedOf(to),
    ...(direction === -1
      ? { back: current.back.slice(0, -1), forward: [here, ...current.forward] }
      : { back: [...current.back, here], forward: current.forward.slice(1) }),
  }))
  return leaving ? restoreFollowers(stepped, panelId, to.followers) : stepped
}
