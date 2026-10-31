/**
 * The detail view's columns as one piece of state, with the vocabulary the app around it needs:
 * following a link (from a panel or a root), hovering one (which lights where it would open),
 * dropping, moving, pinning, closing panels, their history, and what each panel resolves to.
 *
 * **Following a link** from a view (a panel, or a root) shows what it names, relative to the view's
 * address, at the params it adds to the view's (`own`):
 *
 * - a **plain click** browses. When the browsing panel right after the view in the sequence, or a
 *   pinned panel, already shows that, it is revealed. Else the browsing panel right after the view
 *   navigates there (remembering what it showed, for back). Else a browsing panel opens right after
 *   the view (before a pinned panel there), where the placement rules say.
 * - an **Alt-click** opens a pinned panel beside the view, in the sequence after the view's
 *   followers. A **drop** opens a panel where it was dropped: a browsing one right after the view
 *   when the view has no browsing panel after it yet, else a pinned one as an Alt-click does.
 *
 * A link followed from a root (the trace, the rail) is stored absolute: its location as it resolves
 * from the centre's, and as its own params the level's keys (`Centre.keys`) and what it adds to the
 * slice. So moving between levels changes what no panel shows, and the panels still follow the
 * slice.
 *
 * **Navigating** a panel (a click in the view before it, its view switcher, its own params, back and
 * forward) keeps the browsing panels following it when it stays on the same kind of thing
 * (`sameKind`: the same location, whatever its view and params), and they follow it there. When it
 * moves to another kind of thing they close, even where they would still resolve, and are kept with
 * what it showed, so going back to that brings them back (`columns.ts`'s `Visit`).
 *
 * A browsing panel showing a view declared within another (`parentViewOf`) names nothing unless the
 * panel before it shows that one, so it closes when that one switches away; only that view links to
 * it (`views/viewLink.ts`).
 *
 * A pick inside a browsing panel that leaves out a binding it inherits pins it (`setBindings`).
 * After every change, the first browsing panel whose address names nothing any more (the panel
 * before it changed to something it cannot follow) closes, with its followers (`prune`); a pinned
 * one stays, saying why it shows nothing. A path through an entity reference can only be told from
 * the data: the panel's body reports one that names nothing (`namesNothing`), closing it the same
 * way.
 */

import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Store, useStableCallback } from '../lib/store'
import { AddressError, addressTitle, applyRelLocation, formatRel, relLocationFor, sameLocation, type Address, type Location, type Params, type Rel } from './address'
import {
  RAIL,
  TRACE,
  allPanels,
  closePanels,
  findPanel,
  followersOf,
  historyStep,
  insertPanel,
  isRoot,
  movePanel,
  navigate,
  newPanelId,
  resizeColumn,
  sequence,
  pinPanel,
  stepHistory,
  toggleCollapsed,
  type Columns,
  type PanelInstance,
  type Placement,
  type Rebase,
  type Stored,
} from './columns'
import type { LinkTarget } from './drag'
import { autoPlacement, type Decision, type Geometry } from './placement'
import { isPage, parentViewOf, resolve, type Catalog, type Resolution } from './resolve'


/** Dim values the panels following a view show, on dims it does not bind. */
export type Selected = Readonly<Record<string, readonly Params[string][]>>

/** What a panel (or a root) resolves to now. */
export interface PanelState {
  /** Its address as resolved (its default view written in), without params; null when its `rel`
   *  does not apply to the address of the panel before it. */
  address: Address | null
  resolution: Resolution
  /** Where each binding comes from: `own`, the id of the panel before it that binds it, or `SLICE`. */
  sources: Readonly<Record<string, string>>
}

export interface LinkState {
  /** The browsing panel right after the link's view shows what it names. */
  selected: boolean
  /** A panel shows what it names: that one, or a pinned one. */
  open: boolean
}

export interface ColumnsState {
  columns: Columns
  /** Follow a link from view `originId` (a panel, or a root): see the module's comment. `pin` opens
   *  a pinned panel, as an Alt-click does. */
  follow: (target: LinkTarget, originId: string, options?: { placement?: Placement; pin?: boolean }) => void
  /** Light where a link would open (null: nothing hovered). */
  hover: (target: LinkTarget | null, originId: string) => void
  /** Move a panel on screen; its place in the sequence stays. */
  move: (panelId: string, placement: Placement) => void
  /** Close a panel, and the panels following it. */
  close: (panelId: string) => void
  /** Pin a browsing panel where it is; the panels after it go on following it. */
  pin: (panelId: string) => void
  resizeColumn: (columnId: string, width: number) => void
  toggleCollapsed: (panelId: string) => void
  /** Step a panel through what it showed. */
  history: (panelId: string, direction: -1 | 1) => void
  /** Show another address in a panel (its view switched): a browsing one still relative to the
   *  panel before it where it can be (`relFor`). The panel's params stay. */
  setAddress: (panelId: string, address: Address) => void
  /** Set a panel's own params. */
  setOwn: (panelId: string, own: Params) => void
  /** Bind exactly `bindings` in a panel (a pick inside it). A browsing panel binds itself what
   *  differs from what it has; one leaving out a binding it inherits is pinned at `bindings`. */
  setBindings: (panelId: string, bindings: Params) => void
  /** The data a panel shows has no place at the path its address names, `text` as it resolved:
   *  close it, as `prune` would, if it browses and still resolves to `text`. */
  namesNothing: (panelId: string, text: string) => void
  reset: () => void
  /** Where the hovered or dragged link would land, read by the columns' overlay. */
  highlight: Store<Decision | null>
  /** The panel last opened or used by a link, which the columns scroll into view. */
  reveal: Store<{ panelId: string; seq: number } | null>
  /** Set by the columns' view while mounted: the layout's geometry as measured now. */
  geometry: React.MutableRefObject<(() => Geometry) | null>
  /** What a panel or root resolves to now. */
  stateOf: (id: string) => PanelState
  /** A panel's name in text (`addressTitle`; what it stores, when its address names
   *  nothing), or a root's (`trace`, `rail`). */
  nameOf: (id: string) => string
  /** Whether what a link from `originId` names is shown, and where. */
  linkState: (target: LinkTarget, originId: string) => LinkState
  /** What the panels following a view show on dims the view does not bind. */
  selectedBelowOf: (viewId: string) => Selected
  /** Whether a panel shows what the level at this step path returned. */
  showsReturnOf: (levelPath: readonly string[]) => boolean
  /** Whether a panel shows the node at this step path (in any view), which lights its card's panel
   *  button. */
  showsStep: (stepPath: readonly string[]) => boolean
  /** The renderers of its function a panel shows the node at this step path through, views declared
   *  within others left out: which light its card's views button. The same array until the panels
   *  change. */
  viewsShownOf: (stepPath: readonly string[]) => readonly string[]
  showsExperimentReadme: boolean
  anyOpen: boolean
}

/** Before the columns have been laid out: a column as tall as the window, every panel empty. */
function fallbackGeometry(): Geometry {
  return { columnHeight: window.innerHeight, panelHeight: () => 0 }
}

/** The view a panel resolves against: the panel before it in the sequence (or the roots), with
 *  that one's state; null for a pinned panel. */
type Before = { id: string; state: PanelState } | null

/** What the centre shows, which links from the trace start from: its address (the level's workflow
 *  through the trace, the flowchart, or a view of the workflow), and the keys of the workflows on
 *  the way to the level, which a link from a root binds itself. */
export interface Centre {
  address: Address
  keys: Params
}

/** What every panel resolves to, in sequence, with the roots binding the slice. */
export function resolveColumns(columns: Columns, slice: Params, catalog: Catalog, centre: Centre): Map<string, PanelState> {
  const states = new Map<string, PanelState>()
  const root = { id: TRACE, state: rootState(TRACE, slice, centre) }
  for (const panel of sequence(columns)) states.set(panel.id, resolveStored(panel, beforeOf(panel, states, root), catalog))
  return states
}

/** What `panel` resolves against: the panel before it, or `root` after the roots; null when pinned. */
function beforeOf(panel: PanelInstance, states: ReadonlyMap<string, PanelState>, root: NonNullable<Before>): Before {
  return panel.pinned ? null : panel.prevId === null ? root : { id: panel.prevId, state: states.get(panel.prevId)! }
}

/**
 * Whether a panel showing `from` that comes to show `to` stays on the same kind of thing, so the
 * browsing panels following it follow it there: the same location (whatever the view and the
 * params), or for two views of no location (pages, the experiment), the same view. An address naming
 * nothing is never the same kind as another.
 */
export function sameKind(from: Address | null, to: Address | null): boolean {
  if (from === null || to === null) return false
  if (from.location === null && to.location === null) return from.view === to.view
  return sameLocation(from.location, to.location)
}

/** What `stored` resolves to after `before`. */
function resolveStored(stored: Stored, before: Before, catalog: Catalog): PanelState {
  const env = before === null ? stored.own : { ...before.state.resolution.env, ...stored.own }
  const inherited = before === null ? {}
    : Object.fromEntries(Object.entries(before.state.sources).map(([name, source]) => [name, source === 'own' ? before.id : source]))
  const sources = { ...inherited, ...Object.fromEntries(Object.keys(stored.own).map((name) => [name, 'own'])) }
  let location: Location | null
  try {
    location = applyRelLocation(stored.rel.location, before?.state.address?.location ?? null)
  } catch (error) {
    if (!(error instanceof AddressError)) throw error
    return unresolved(stored, env, sources, error.message)
  }
  const resolution = resolve({ view: stored.rel.view, location }, env, catalog)
  const parent = before === null ? null : parentViewOf(catalog.registry, resolution.address.view)
  if (parent !== null && before!.state.address?.view !== parent)
    return unresolved(stored, env, sources, `this is a view of ${parent}, and the panel before it does not show that`)
  // What the panel after it is relative to is what it resolved to: its default view written in.
  return { address: resolution.address, resolution, sources }
}

function unresolved(stored: Stored, env: Params, sources: Record<string, string>, problem: string): PanelState {
  return {
    address: null,
    resolution: {
      address: { view: null, location: null }, params: {}, text: formatRel(stored.rel), env, viewParams: {}, takes: [], target: null,
      problem, structural: true, loading: false,
    },
    sources,
  }
}

/** What a panel resolving to `state` stores once pinned: its address whole, and every binding it
 *  has. One whose address names nothing keeps the `rel` it stored. */
function pinnedOf(stored: Stored, state: PanelState): Stored {
  const rel: Rel = state.address === null ? stored.rel : { view: state.address.view, location: { kind: 'absolute', location: state.address.location } }
  return { rel, own: state.resolution.env }
}

/** The params of `params` that `env` does not already bind to the same value. */
function added(params: Params, env: Params): Params {
  return Object.fromEntries(Object.entries(params).filter(([name, value]) => env[name] !== value))
}

/** `address` relative to `parent` (the address of the panel before it): its view, and its location
 *  as near as it can be put (`relLocationFor`). */
export function relFor(address: Address, parent: Address | null): Rel {
  return { view: address.view, location: relLocationFor(address.location, parent?.location ?? null) }
}

export function useColumns(slice: Params, catalog: Catalog, centre: Centre): ColumnsState {
  const [columns, setColumns] = useState<Columns>([])
  const [highlight] = useState(() => new Store<Decision | null>(null))
  const [reveal] = useState(() => new Store<{ panelId: string; seq: number } | null>(null))
  const geometry = useRef<(() => Geometry) | null>(null)
  const measure = () => (geometry.current ?? fallbackGeometry)()

  type FollowOptions = { placement?: Placement; pin?: boolean }

  const stateIn = (states: ReadonlyMap<string, PanelState>, id: string): PanelState => {
    if (isRoot(id)) return rootState(id, slice, centre)
    const state = states.get(id)
    if (!state) throw new Error(`no panel ${id}`)
    return state
  }

  /** What a link from `originId` resolves to, and what it stores there: relative to a panel, the
   *  params it adds to the panel's; from a root, absolute, binding the level's keys too. */
  const targetOf = (states: ReadonlyMap<string, PanelState>, target: LinkTarget, originId: string) => {
    const origin = stateIn(states, originId)
    const from = { id: originId, state: origin }
    if (!isRoot(originId)) {
      const stored: Stored = { rel: target.rel, own: added(target.params, origin.resolution.env) }
      return { stored, state: resolveStored(stored, from, catalog) }
    }
    const own = added({ ...centre.keys, ...target.params }, origin.resolution.env)
    let rel = target.rel
    try {
      rel = { view: rel.view, location: { kind: 'absolute', location: applyRelLocation(rel.location, origin.address?.location ?? null) } }
    } catch (error) {
      // Stored as it is, it resolves to the same problem, which the panel shows.
      if (!(error instanceof AddressError)) throw error
    }
    const stored: Stored = { rel, own }
    return { stored, state: resolveStored(stored, from, catalog) }
  }

  /** A browsing panel's address and bindings as it resolves in `states`, put relative to the panel
   *  `prevId` (null: the roots), as near as they can be put: what it stores to follow that one. */
  const rebase = (states: ReadonlyMap<string, PanelState>): Rebase => (panel, prevId) => {
    const before = stateIn(states, prevId ?? TRACE)
    const state = states.get(panel.id)!
    return {
      rel: state.address === null ? panel.rel : prevId === null ? absoluteRel(state.address) : relFor(state.address, before.address),
      own: added(state.resolution.env, before.resolution.env),
    }
  }

  /** Close the first browsing panel whose address names nothing, and its followers. */
  const prune = (current: Columns): Columns => {
    const states = resolveColumns(current, slice, catalog, centre)
    const broken = sequence(current).find((panel) => !panel.pinned && states.get(panel.id)!.resolution.structural)
    return broken ? closePanels(current, new Set([broken.id])) : current
  }

  /** Where a plain click on `target` from `originId` goes: a panel already showing it, or else the
   *  browsing panel right after the view to navigate, if there is one. */
  const plainClick = (current: Columns, states: ReadonlyMap<string, PanelState>, state: PanelState, originId: string) => {
    const ordered = sequence(current)
    const next = followersOf(ordered, originId)[0]
    const text = state.resolution.text
    const showing = state.address === null ? undefined
      : [...(next ? [next] : []), ...ordered.filter((panel) => panel.pinned)].find((panel) => states.get(panel.id)!.resolution.text === text)
    return { ordered, showing, next }
  }

  /** Following `target` from `originId` in the layout `current`: the layout after, and the panel
   *  shown, navigated, or opened. */
  const applyFollow = (current: Columns, measured: Geometry, target: LinkTarget, originId: string,
    { placement, pin = false }: FollowOptions): { columns: Columns; panelId: string } => {
    const states = resolveColumns(current, slice, catalog, centre)
    const { stored, state } = targetOf(states, target, originId)
    const viewId = isRoot(originId) ? null : originId
    const followers = followersOf(sequence(current), originId)
    // A drop browses when the view has no browsing panel after it yet.
    if (pin || (placement !== undefined && followers.length > 0)) {
      const prevId = followers.at(-1)?.id ?? viewId
      const panel: PanelInstance = { id: newPanelId(), prevId, pinned: true, ...pinnedOf(stored, state), back: [], forward: [] }
      const at = placement ?? autoPlacement(current, measured, viewId, state.address)
      return { columns: prune(insertPanel(current, at, panel)), panelId: panel.id }
    }
    const { showing, next } = plainClick(current, states, state, originId)
    if (placement === undefined) {
      if (showing) return { columns: current, panelId: showing.id }
      if (next) {
        const leaving = !sameKind(states.get(next.id)!.address, state.address)
        return { columns: prune(navigate(current, next.id, stored, leaving)), panelId: next.id }
      }
    }
    const panel: PanelInstance = { id: newPanelId(), prevId: viewId, pinned: false, ...stored, back: [], forward: [] }
    const at = placement ?? autoPlacement(current, measured, viewId, state.address)
    return { columns: prune(insertPanel(current, at, panel)), panelId: panel.id }
  }

  /** The panel the last follow opened or used, published to `reveal` once the layout holding it has
   *  rendered: the follow is worked out inside a state update, which must not set stores. */
  const pendingReveal = useRef<{ panelId: string; seq: number } | null>(null)
  useLayoutEffect(() => {
    if (pendingReveal.current === null) return
    reveal.set(pendingReveal.current)
    pendingReveal.current = null
  }, [columns, reveal])

  const nextSeq = () => (reveal.get()?.seq ?? 0) + 1

  const follow = useStableCallback((target: LinkTarget, originId: string, options: FollowOptions = {}) => {
    highlight.set(null)
    const seq = nextSeq()
    const measured = measure()
    setColumns((current) => {
      const origin = isRoot(originId) || findPanel(current, originId) ? originId : TRACE
      const next = applyFollow(current, measured, target, origin, options)
      pendingReveal.current = { panelId: next.panelId, seq }
      return next.columns
    })
  })

  /** Where a plain click on `target` from `originId` would land. */
  const decide = (current: Columns, measured: Geometry, target: LinkTarget, originId: string): Decision => {
    const states = resolveColumns(current, slice, catalog, centre)
    const { state } = targetOf(states, target, originId)
    const { showing, next } = plainClick(current, states, state, originId)
    const existing = showing ?? next
    if (existing) return { kind: 'existing', panelId: existing.id }
    return autoPlacement(current, measured, isRoot(originId) ? null : originId, state.address)
  }

  const hover = useStableCallback((target: LinkTarget | null, originId: string) => {
    highlight.set(target ? decide(columns, measure(), target, originId) : null)
  })

  const update = (change: (current: Columns, states: Map<string, PanelState>) => Columns) =>
    setColumns((current) => prune(change(current, resolveColumns(current, slice, catalog, centre))))

  const move = useStableCallback((panelId: string, placement: Placement) => {
    update((current, states) => movePanel(current, panelId, placement, rebase(states)))
    highlight.set(null)
  })
  const close = useStableCallback((panelId: string) => update((current) => closePanels(current, new Set([panelId]))))
  const pin = useStableCallback((panelId: string) =>
    update((current, states) => {
      const panel = findPanel(current, panelId)!
      return panel.pinned ? current : pinPanel(current, panelId, pinnedOf(panel, states.get(panelId)!))
    }))
  /** Whether `panel` moves to another kind of thing showing `to` (`sameKind`). */
  const leaves = (panel: PanelInstance, to: Stored, states: ReadonlyMap<string, PanelState>): boolean => {
    const before = beforeOf(panel, states, { id: TRACE, state: rootState(TRACE, slice, centre) })
    return !sameKind(states.get(panel.id)!.address, resolveStored(to, before, catalog).address)
  }
  const history = useStableCallback((panelId: string, direction: -1 | 1) =>
    update((current, states) => {
      const panel = findPanel(current, panelId)!
      const to = historyStep(panel, direction)
      return to ? stepHistory(current, panelId, direction, leaves(panel, to, states)) : current
    }))
  const setAddress = useStableCallback((panelId: string, address: Address) =>
    update((current, states) => {
      const panel = findPanel(current, panelId)!
      const rel = panel.pinned || panel.prevId === null ? absoluteRel(address)
        : relFor(address, stateIn(states, panel.prevId).address)
      const to: Stored = { rel, own: panel.own }
      return navigate(current, panelId, to, leaves(panel, to, states))
    }))
  const setOwn = useStableCallback((panelId: string, own: Params) =>
    update((current) => navigate(current, panelId, { rel: findPanel(current, panelId)!.rel, own })))
  const setBindings = useStableCallback((panelId: string, bindings: Params) =>
    update((current, states) => {
      const panel = findPanel(current, panelId)!
      if (panel.pinned) return navigate(current, panelId, { rel: panel.rel, own: bindings })
      const state = states.get(panelId)!
      // A binding it inherits can only be left out by pinning it at `bindings`.
      const env = state.resolution.env
      if (Object.keys(env).some((name) => !Object.hasOwn(bindings, name) && !Object.hasOwn(panel.own, name)))
        return pinPanel(current, panelId, { ...pinnedOf(panel, state), own: bindings })
      const own: Record<string, Params[string]> = { ...panel.own }
      for (const name of new Set([...Object.keys(env), ...Object.keys(bindings)])) {
        if (bindings[name] === env[name]) continue
        if (Object.hasOwn(bindings, name)) own[name] = bindings[name]!
        else delete own[name]
      }
      return navigate(current, panelId, { rel: panel.rel, own })
    }))
  const namesNothing = useStableCallback((panelId: string, text: string) =>
    setColumns((current) => {
      const panel = findPanel(current, panelId)
      if (!panel || panel.pinned) return current
      const states = resolveColumns(current, slice, catalog, centre)
      if (states.get(panelId)!.resolution.text !== text) return current
      return prune(closePanels(current, new Set([panelId])))
    }))
  const resize = useStableCallback((columnId: string, width: number) => setColumns((current) => resizeColumn(current, columnId, width)))
  const toggle = useStableCallback((panelId: string) => setColumns((current) => toggleCollapsed(current, panelId)))
  const reset = useStableCallback(() => setColumns([]))

  // What is derived from the panels alone is kept while only the columns' widths change, so
  // resizing a column does not re-render every link in the trace and the panels.
  const panelsOnly = useRef(columns)
  if (!samePanels(panelsOnly.current, columns)) panelsOnly.current = columns
  const structure = panelsOnly.current

  const derived = useMemo(() => {
    const states = resolveColumns(structure, slice, catalog, centre)
    const panels = allPanels(structure)
    const ordered = sequence(structure)
    const pinned = panels.filter((panel) => panel.pinned)
    const stateOf = (id: string): PanelState => stateIn(states, id)
    const linkState = (target: LinkTarget, originId: string): LinkState => {
      if (!isRoot(originId) && !states.has(originId)) return CLOSED
      const { state } = targetOf(states, target, originId)
      if (state.address === null) return CLOSED
      const shows = (panel: PanelInstance) => states.get(panel.id)!.resolution.text === state.resolution.text
      const next = followersOf(ordered, originId)[0]
      const selected = next !== undefined && shows(next)
      return { selected, open: selected || pinned.some(shows) }
    }
    const selectedBelowOf = (viewId: string): Selected => {
      const env = stateOf(viewId).resolution.env
      const selected: Record<string, Params[string][]> = {}
      for (const panel of followersOf(ordered, viewId)) {
        for (const [dim, value] of Object.entries(panel.own)) {
          if (Object.hasOwn(env, dim)) continue
          const values = (selected[dim] ??= [])
          if (!values.includes(value)) values.push(value)
        }
      }
      return selected
    }
    const stepKey = (stepPath: readonly string[]) => stepPath.join('/')
    const shownSteps = new Set(panels.flatMap((panel) => {
      const target = states.get(panel.id)!.resolution.target
      return target?.kind === 'node' ? [stepKey([...target.levelPath, target.step.name])] : []
    }))
    const viewsShown = new Map<string, string[]>()
    for (const panel of panels) {
      const target = states.get(panel.id)!.resolution.target
      if (target?.kind !== 'node' || target.view.kind !== 'renderer' || parentViewOf(catalog.registry, target.view.id) !== null) continue
      const key = stepKey([...target.levelPath, target.step.name])
      const shown = viewsShown.get(key) ?? []
      if (!shown.includes(target.view.id)) shown.push(target.view.id)
      viewsShown.set(key, shown)
    }
    const showsReturnOf = (levelPath: readonly string[]) => panels.some((panel) => {
      const location = states.get(panel.id)!.address?.location
      return location?.root === 'step' && stepKey(location.steps) === stepKey(levelPath) && location.path[0] === 'output'
    })
    const nameOf = (id: string): string => {
      if (id === TRACE) return 'trace'
      if (id === RAIL) return 'rail'
      const address = stateOf(id).address
      if (address) return addressTitle(address, isPage(catalog.registry, address.view)).join(' › ')
      const panel = panels.find((candidate) => candidate.id === id)
      if (!panel) throw new Error(`no panel ${id}`)
      return formatRel(panel.rel, panel.own)
    }
    return {
      stateOf,
      nameOf,
      linkState,
      selectedBelowOf,
      showsReturnOf,
      showsStep: (stepPath: readonly string[]) => shownSteps.has(stepKey(stepPath)),
      viewsShownOf: (stepPath: readonly string[]) => viewsShown.get(stepKey(stepPath)) ?? NO_VIEWS,
      showsExperimentReadme: panels.some((panel) => states.get(panel.id)!.resolution.target?.kind === 'experiment_readme'),
      anyOpen: panels.length > 0,
    }
  }, [structure, slice, catalog, centre])

  return {
    columns, follow, hover, move, close, pin, resizeColumn: resize, toggleCollapsed: toggle, history,
    setAddress, setOwn, setBindings, namesNothing, reset, highlight, reveal, geometry, ...derived,
  }
}

const NO_VIEWS: readonly string[] = []
const CLOSED: LinkState = { selected: false, open: false }

/** The source of a binding the level's slice makes. */
export const SLICE = 'slice'

/** A root's state: the trace at the centre's address, the rail at none, both binding the slice. */
function rootState(id: string, slice: Params, centre: Centre): PanelState {
  const address = id === TRACE ? centre.address : null
  return {
    address,
    resolution: {
      address: address ?? { view: null, location: null }, params: {}, text: '', env: slice, viewParams: {}, takes: [], target: null,
      problem: null, structural: false, loading: false,
    },
    sources: Object.fromEntries(Object.keys(slice).map((name) => [name, SLICE])),
  }
}

function absoluteRel(address: Address): Rel {
  return { view: address.view, location: { kind: 'absolute', location: address.location } }
}

/** Whether two layouts hold the same panels, in the same places, whatever their columns' widths. */
function samePanels(a: Columns, b: Columns): boolean {
  return a.length === b.length && a.every((column, index) => {
    const other = b[index]!.panels
    return column.panels.length === other.length && column.panels.every((panel, at) => panel === other[at])
  })
}
