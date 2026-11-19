/**
 * The viewer's state in the URL, so a link can be shared and a coding agent can write one that
 * shows the user something: the job, the workflow invocation shown and the view it is shown
 * through, the slice, and the panels. The format is not stable. Everything is in the hash, as `&`-separated `name=value`
 * parts (a value is everything after the first `=`):
 *
 * ```
 * #job=<id>&w=flowchart(/outer,elicitor=sonnet)&slice=(model=sonnet)
 *     &p=table(/judge,prompt=3)&p=.output.transcript&pin=tree(cid:bafy…)&layout=0,1|2@480&collapsed=2
 * ```
 *
 * - `job`: the job's id. Without one, the rest applies to the newest job.
 * - `w`: the workflow invocation shown, as an address (`panes/address.ts`): `trace`, `flowchart`, or a
 *   renderer of the level's workflow, at the level's step path from the root, with the keys of the
 *   mapped workflows on the way as params. Without one, the trace of the root.
 * - `slice`: the slice's picks, as a param list.
 * - `card`: a step of the level `w` shows, by its name (`return` for what the level returned): the
 *   trace scrolls to its card, and the flowchart selects it. Kept until the level changes, not
 *   updated as cards are selected: links to a card are written, not made by clicking.
 * - `p`, `pin`: the panels in sequence (each follows the one before it), `p` a browsing panel and
 *   `pin` a pinned one, each what the panel stores (`columns.ts`'s `Stored`) as a relative address
 *   (`formatRel`): the first browsing panel and every pinned one are absolute, and a browsing panel
 *   after another may be relative to it (`.output.transcript`, `../step`). A location alone is short for its default view: `p=/judge`
 *   is `p=(/judge)`; so is `w=/outer`.
 * - `layout`: optional. The columns, left to right, separated by `|`: the sequence indexes of each
 *   one's panels, top to bottom, then `@` and its width in px when it is not the default. Without
 *   it, the panels are placed as links opening them in sequence would place them (for links an
 *   agent writes: the viewer always writes it).
 * - `collapsed`: the sequence indexes of the panels shrunk to their heads.
 *
 * Only what a view is given (its address and params) is here, never a view's own state. Writing
 * percent-encodes only what a URL's hash would misread or mangle (`%`, `&`, `#`, `+`, quotes,
 * spaces, controls, and anything not ASCII), and addresses leave out the space after each comma,
 * so a link stays readable; reading decodes each value, so a link encoded throughout works too. A
 * part that does not read is reported and left out, never the whole link.
 *
 * The browser's history entries also hold a `Snapshot`: the panels exactly as they were (their
 * ids, so going back keeps what each one has on screen, and their own back and forward), which
 * back and forward restore when it is there; the URL is what a reload of a link without one, or a
 * pasted link, is read from.
 */

import {
  AddressError, formatAddress, formatRel, parseAddress, parseRel, type Address, type Params,
} from '../panes/address'
import {
  DEFAULT_COLUMN_WIDTH, insertPanel, newColumnId, newPanelId, sequence, type Columns, type PanelInstance, type Stored,
} from '../panes/columns'
import { autoPlacement, estimateHeight } from '../panes/placement'
import type { LevelRef } from '../types'

export interface UrlPanel {
  stored: Stored
  pinned: boolean
  collapsed: boolean
}

export interface UrlColumn {
  /** Sequence indexes of its panels, top to bottom. */
  panels: readonly number[]
  /** In px; null for the default. */
  width: number | null
}

/** The workflow invocation shown: its step path and view (`address`), and the keys of the mapped workflows on
 *  the way to it. */
export interface UrlWorkflow {
  address: Address
  keys: Params
}

export interface UrlState {
  job: string | null
  /** Null: the trace of the root. */
  workflow: UrlWorkflow | null
  slice: Params
  /** A step of the level shown, by name, to scroll to; null for none. */
  card: string | null
  panels: readonly UrlPanel[]
  /** Null: placed as links would place them. */
  layout: readonly UrlColumn[] | null
}

export const EMPTY_URL_STATE: UrlState = { job: null, workflow: null, slice: {}, card: null, panels: [], layout: null }

// ── encoding ──

/** `text` with what a hash would misread or mangle percent-encoded. */
function encode(text: string): string {
  return text.replace(/[%&#+"<>`]|[^\x21-\x7e]/gu, (char) => encodeURIComponent(char))
}

const COMPACT = { compact: true } as const

// ── writing ──

export function formatUrlState(state: UrlState): string {
  const parts: [string, string][] = []
  if (state.job !== null) parts.push(['job', state.job])
  if (state.workflow !== null) parts.push(['w', formatAddress(state.workflow.address, state.workflow.keys, COMPACT)])
  if (Object.keys(state.slice).length > 0) parts.push(['slice', formatAddress({ view: null, location: null }, state.slice, COMPACT)])
  if (state.card !== null) parts.push(['card', state.card])
  for (const panel of state.panels) parts.push([panel.pinned ? 'pin' : 'p', formatRel(panel.stored.rel, panel.stored.own, COMPACT)])
  if (state.layout !== null)
    parts.push(['layout', state.layout.map((column) =>
      `${column.panels.join(',')}${column.width === null ? '' : `@${column.width}`}`).join('|')])
  const collapsed = state.panels.flatMap((panel, index) => (panel.collapsed ? [index] : []))
  if (collapsed.length > 0) parts.push(['collapsed', collapsed.join(',')])
  return parts.length === 0 ? '' : `#${parts.map(([name, value]) => `${name}=${encode(value)}`).join('&')}`
}

/** `hash` naming job `job`, the rest of it kept: what a link naming no job (the newest) becomes
 *  once the job is known. */
export function withJob(hash: string, job: string): string {
  const rest = (hash.startsWith('#') ? hash.slice(1) : hash).split('&').filter((part) => part !== '' && !part.startsWith('job='))
  return `#${[`job=${encode(job)}`, ...rest].join('&')}`
}

/** The panels of `columns` as the URL has them: in sequence, and where each is on screen, always
 *  (placing them afresh could arrange them otherwise), widths in whole pixels; no layout when no
 *  panel is open. */
export function urlPanelsOf(columns: Columns): { panels: UrlPanel[]; layout: UrlColumn[] | null } {
  const ordered = sequence(columns)
  const index = new Map(ordered.map((panel, at) => [panel.id, at]))
  const panels = ordered.map((panel): UrlPanel => ({ stored: { rel: panel.rel, own: panel.own }, pinned: panel.pinned, collapsed: !!panel.collapsed }))
  const layout = columns.map((column): UrlColumn => {
    // A column dragged while the strip scrolls is resized by fractions of a pixel.
    const width = Math.round(column.width)
    return { panels: column.panels.map((panel) => index.get(panel.id)!), width: width === DEFAULT_COLUMN_WIDTH ? null : width }
  })
  return { panels, layout: layout.length === 0 ? null : layout }
}

// ── reading ──

/** The state a hash (`location.hash`, with or without its `#`) names, and what in it did not read. */
export function parseUrlState(hash: string): { state: UrlState; problems: string[] } {
  const problems: string[] = []
  let job: string | null = null
  let workflow: UrlWorkflow | null = null
  let slice: Params = {}
  let card: string | null = null
  const panels: UrlPanel[] = []
  let layoutText: string | null = null
  let collapsedText: string | null = null
  /** Whether the browsing panels from here on are left out: they would follow one that was. */
  let dropping = false
  const text = hash.startsWith('#') ? hash.slice(1) : hash
  for (const part of text.split('&')) {
    if (part === '') continue
    const at = part.indexOf('=')
    const name = at < 0 ? part : part.slice(0, at)
    let value: string
    try {
      value = decodeURIComponent(at < 0 ? '' : part.slice(at + 1))
    } catch {
      problems.push(`${name}: not percent-encoded properly`)
      if (name === 'p' || name === 'pin') dropping = true
      continue
    }
    try {
      switch (name) {
        case 'job':
          job = value === '' ? null : value
          break
        case 'w':
          workflow = parseWorkflow(value)
          break
        case 'slice': {
          const { address, params } = parseAddress(value)
          if (address.view !== null || address.location !== null) throw new AddressError('the slice is a list of params, `(dim=value,…)`')
          slice = params
          break
        }
        case 'card':
          card = value === '' ? null : value
          break
        case 'p':
        case 'pin': {
          const pinned = name === 'pin'
          if (dropping && !pinned) {
            problems.push(`${name}=${value}: left out with the panel before it, which it follows`)
            break
          }
          let parsed: ReturnType<typeof parseRel>
          try {
            parsed = parseRel(called(value))
          } catch (error) {
            // The browsing panels after a panel left out would follow the one before it instead.
            dropping = true
            throw error
          }
          dropping = false
          panels.push({ stored: { rel: parsed.rel, own: parsed.params }, pinned, collapsed: false })
          break
        }
        case 'layout':
          layoutText = value
          break
        case 'collapsed':
          collapsedText = value
          break
        default:
          problems.push(`${name}: not a part of the viewer's links`)
      }
    } catch (error) {
      if (!(error instanceof AddressError)) throw error
      problems.push(`${name}=${value}: ${error.message}`)
    }
  }
  let layout: UrlColumn[] | null = null
  if (layoutText !== null) {
    layout = parseLayout(layoutText, panels.length)
    if (layout === null) problems.push(`layout=${layoutText}: not every panel once, in columns \`0,1|2@480\``)
  }
  if (collapsedText !== null) {
    for (const item of collapsedText.split(',')) {
      const index = Number(item)
      if (Number.isInteger(index) && index >= 0 && index < panels.length) panels[index] = { ...panels[index]!, collapsed: true }
      else problems.push(`collapsed=${collapsedText}: ${item} is not a panel's index`)
    }
  }
  return { state: { job, workflow, slice, card, panels, layout }, problems }
}

/** A location alone (`.output`, `/judge`) as the call of the default view it is short for. */
function called(text: string): string {
  return text.includes('(') ? text : `(${text})`
}

function parseWorkflow(text: string): UrlWorkflow {
  const { address, params } = parseAddress(called(text))
  if (address.location?.root !== 'step' || address.location.path.length > 0)
    throw new AddressError('`w` shows a workflow: a step path from the root, `/` or `/outer/inner`')
  return { address, keys: params }
}

/** The columns `text` lays the panels out in, or null when it does not place each of `count`
 *  panels exactly once. */
function parseLayout(text: string, count: number): UrlColumn[] | null {
  const seen = new Set<number>()
  const columns: UrlColumn[] = []
  for (const columnText of text.split('|')) {
    const match = /^([\d,]+)(?:@(\d+))?$/.exec(columnText)
    if (!match) return null
    const panels = match[1]!.split(',').map(Number)
    for (const index of panels) {
      if (!Number.isInteger(index) || index >= count || seen.has(index)) return null
      seen.add(index)
    }
    columns.push({ panels, width: match[2] === undefined ? null : Number(match[2]) })
  }
  return seen.size === count ? columns : null
}

/** The layout `panels` open in: in `layout` when there is one, else placed one at a time, in
 *  sequence, as following links to them would place them, in columns `columnHeight` tall. Every
 *  panel and column is new. */
export function columnsFromUrl(panels: readonly UrlPanel[], layout: readonly UrlColumn[] | null, columnHeight: number): Columns {
  const instances = panels.map((panel): PanelInstance => ({
    id: newPanelId(), prevId: null, pinned: panel.pinned, ...panel.stored, back: [], forward: [],
    ...(panel.collapsed ? { collapsed: true } : {}),
  }))
  instances.forEach((panel, index) => {
    panel.prevId = index === 0 ? null : instances[index - 1]!.id
  })
  if (layout !== null)
    return layout.map((column) => ({
      id: newColumnId(),
      width: column.width ?? DEFAULT_COLUMN_WIDTH,
      panels: column.panels.map((index) => instances[index]!),
    }))
  // Before they render, each panel is as tall as its kind's estimate.
  const addressOf = (panel: PanelInstance): Address | null =>
    panel.rel.location.kind === 'absolute' ? { view: panel.rel.view, location: panel.rel.location.location } : null
  const heights = new Map(instances.map((panel) => [panel.id, estimateHeight(addressOf(panel))]))
  const geometry = { columnHeight, panelHeight: (id: string) => heights.get(id) ?? 0 }
  let columns: Columns = []
  for (const panel of instances) columns = insertPanel(columns, autoPlacement(columns, geometry, panel.prevId, addressOf(panel)), panel)
  return columns
}

// ── what a job's view shows ──

/**
 * What a job's view shows that its URL and history entries hold: the panels, the slice, the
 * levels opened (breadcrumbs) and the one shown, how it is shown, and a card to go to. `JobView`
 * opens with one (a history entry or a link changing opens it afresh) and writes one back as it
 * changes. A history entry holds one exactly as it was; a link, the part of one it can say
 * (`pageViewFrom`).
 */
export interface ViewState {
  columns: Columns
  slice: Params
  trail: readonly LevelRef[]
  depth: number
  pageView: string | null
  flowchartMode: boolean
  /** A step of the level shown, by name, to go to (`card` in the URL). */
  card: string | null
}

/** A history entry's state: the view as it was. Its version changes whenever `ViewState`, or
 *  anything it holds (`Columns`, `PanelInstance`), changes shape, since entries outlive a build. */
export interface Snapshot {
  version: typeof SNAPSHOT_VERSION
  job: string
  view: ViewState
}

const SNAPSHOT_VERSION = 3

export function snapshotState(job: string, view: ViewState): { fxtrViewer: Snapshot } {
  return { fxtrViewer: { version: SNAPSHOT_VERSION, job, view } }
}

/** The snapshot a history entry's state holds for `job`; null when it holds none (a link pasted or
 *  edited, another build's). */
export function readSnapshot(state: unknown, job: string): Snapshot | null {
  if (typeof state !== 'object' || state === null || !('fxtrViewer' in state)) return null
  const snapshot = (state as { fxtrViewer: Partial<Snapshot> }).fxtrViewer
  if (snapshot?.version !== SNAPSHOT_VERSION || snapshot.job !== job || typeof snapshot.view !== 'object') return null
  return snapshot as Snapshot
}

/** What to show of a job as its view opens. `check`: the panels came from a
 *  link, and are checked against what they name. `pending`: the workflow to show, at a level below
 *  the root, whose pathname is found once the levels on the way load (`view` shows the root till
 *  then). */
export interface PageView {
  view: ViewState
  check: boolean
  pending: UrlWorkflow | null
  problems: readonly string[]
}

/** How the center shows its level, from its address's view. */
export function centerView(view: string | null): { pageView: string | null; flowchartMode: boolean } {
  if (view === null || view === 'trace') return { pageView: null, flowchartMode: false }
  if (view === 'flowchart') return { pageView: null, flowchartMode: true }
  return { pageView: view, flowchartMode: false }
}

/** What to show of job `job`: the history entry's snapshot (`entryState`) when it has one for it,
 *  else what the URL's `hash` says; null when the URL names another job (a job switched to starts
 *  afresh). Panels without a layout are placed in columns `columnHeight` tall. Each call makes new
 *  panels for a link's: `App`'s `Jobs` calls it once per entry come to. */
export function pageViewFrom(job: string, hash: string, entryState: unknown, columnHeight: number): PageView | null {
  const snapshot = readSnapshot(entryState, job)
  if (snapshot) return { view: snapshot.view, check: false, pending: null, problems: [] }
  const { state, problems } = parseUrlState(hash)
  if (state.job !== null && state.job !== job) return null
  const atRoot = state.workflow === null || (state.workflow.address.location?.root === 'step' && state.workflow.address.location.steps.length === 0)
  return {
    view: {
      columns: columnsFromUrl(state.panels, state.layout, columnHeight),
      slice: state.slice,
      trail: [],
      depth: 0,
      ...(atRoot ? centerView(state.workflow?.address.view ?? null) : centerView(null)),
      card: state.card,
    },
    check: true,
    pending: atRoot ? null : state.workflow,
    problems,
  }
}
