import type { LinkPlace } from 'fxtr-view'
import type { LinkTarget } from '../panes/drag'
import { VIEWS, type Address, type Location, type Params, type Rel } from '../panes/address'
import type { KeyValue, StepDoc } from '../types'

/**
 * What this app sets as the base of a `LinkBase` (fxtr-view's link places), and the links the
 * viewer's own controls open:
 *
 * - a node's output (`nodeBase`), whose rows' values the links below sit in, in the trace's cards: a
 *   link with a place on it opens the value at the link's path in that node's output, at the row's
 *   keys;
 * - an open panel (`panelBase`), whose shown value the links below sit in: a link with a place on
 *   it opens the value at its path in what the panel shows, relative to the panel (`.output.…` or
 *   `.…`), so the panel it opens follows that one as it changes.
 *
 * Any other link opens its entity alone (`cid:…`), fixed.
 */
export interface NodeLinkBase {
  kind: 'fxtr-node'
  /** The node's location: its step path. */
  location: Location & { root: 'step' }
}

export interface PanelLinkBase {
  kind: 'fxtr-panel'
  panelId: string
  /** What the panel shows, with no params. */
  address: Address
}

export function nodeBase(location: Location & { root: 'step' }): NodeLinkBase {
  return { kind: 'fxtr-node', location }
}

export function panelBase(panelId: string, address: Address): PanelLinkBase {
  return { kind: 'fxtr-panel', panelId, address }
}

function baseKind(base: unknown): unknown {
  return typeof base === 'object' && base !== null ? (base as { kind?: unknown }).kind : undefined
}

const NO_PARAMS: Params = {}

/** The location of a node of the level at `levelPath`, with `path` into its invocations' records. */
export function nodeLocation(levelPath: readonly string[], name: string, path: readonly string[] = []): Location & { root: 'step' } {
  return { root: 'step', steps: [...levelPath, name], path }
}

/** A link to an absolute location through its default view. */
function absolute(location: Location): Rel {
  return { view: null, location: { kind: 'absolute', location } }
}

/**
 * The value at `path` in what `location` shows, relative to it, so the panel it opens follows that
 * one wherever it goes: below a value (an entity, or a step's field path), the path below whatever
 * the panel shows; in a node shown whole (its rows, or a renderer), the path in the node's output.
 */
function valueRel(location: Location, path: readonly string[]): Rel {
  const below = location.root === 'step' && location.path.length === 0 ? ['output', ...path] : path
  return { view: null, location: below.length === 0 ? { kind: 'same' } : { kind: 'below', path: below } }
}

/** What a link to `cid` opens, given its place. */
export function linkTarget(cid: string, place: LinkPlace | null | undefined): LinkTarget {
  if (place && baseKind(place.base) === 'fxtr-panel') {
    const { location } = (place.base as PanelLinkBase).address
    if (location !== null) return { rel: valueRel(location, place.path), params: place.keys }
  }
  if (place && baseKind(place.base) === 'fxtr-node') {
    const base = place.base as NodeLinkBase
    return { rel: absolute({ ...base.location, path: ['output', ...place.path] }), params: place.keys }
  }
  return { rel: absolute({ root: 'entity', cid, path: [] }), params: NO_PARAMS }
}

/** The link to a node's panel at a row's or an invocation's keys: what clicking a row opens. */
export function nodeTarget(levelPath: readonly string[], step: StepDoc, keys: Readonly<Record<string, KeyValue>> = NO_PARAMS): LinkTarget {
  return { rel: absolute(nodeLocation(levelPath, step.name)), params: keys }
}

/** The link to a node's panel through `view`, one of its function's renderers, at the trace's
 *  bindings: what a card's views button opens. */
export function viewTarget(levelPath: readonly string[], step: StepDoc, view: string): LinkTarget {
  return { rel: { view, location: { kind: 'absolute', location: nodeLocation(levelPath, step.name) } }, params: NO_PARAMS }
}

/** The link to one invocation of a run node: the node's panel through `invocation` (its status,
 *  output, inputs and provenance), at `keys`: the invocation's, or one of its rows' (which the
 *  panel's output then narrows to). */
export function invocationTarget(levelPath: readonly string[], step: StepDoc, keys: Readonly<Record<string, KeyValue>>): LinkTarget {
  if (!step.browse || step.method === null) throw new Error(`${step.name} was not run, so it has no invocations`)
  return { rel: { view: VIEWS.invocation, location: { kind: 'absolute', location: nodeLocation(levelPath, step.name) } }, params: keys }
}

/** The link to what the level at `levelPath` returned. */
export function returnedTarget(levelPath: readonly string[]): LinkTarget {
  return { rel: absolute({ root: 'step', steps: levelPath, path: ['output'] }), params: NO_PARAMS }
}

/** The link to where the function of the node at `steps` (the root workflow's, for none) is
 *  implemented. */
export function codeTarget(steps: readonly string[]): LinkTarget {
  return { rel: { view: VIEWS.code, location: { kind: 'absolute', location: { root: 'step', steps, path: [] } } }, params: NO_PARAMS }
}

/** The link to a view of no location: a page, or (`VIEWS.experimentReadme`) the experiment. */
export function pageTarget(view: string): LinkTarget {
  return { rel: { view, location: { kind: 'absolute', location: null } }, params: NO_PARAMS }
}

/** The link to the level at `levelPath`'s own workflow through `view`, one of its renderers. */
export function workflowViewTarget(levelPath: readonly string[], view: string): LinkTarget {
  return { rel: { view, location: { kind: 'absolute', location: { root: 'step', steps: levelPath, path: [] } } }, params: NO_PARAMS }
}
