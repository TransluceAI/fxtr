/**
 * What an address shows, worked out against the whole job and the registry (`Catalog`): the
 * address with its default view written in, the params it uses, and the target a panel renders, or
 * why there is none.
 *
 * **Locations.** A step path is walked from the job's root level: each workflow step on the way is
 * entered through the run its mapped (and replica) keys pick out, which the params must bind, and
 * that run's level is read from the catalog, loaded as needed. The last step is a node of the level
 * reached, shown through a view; `/` alone is the root workflow's own invocation. A field path goes
 * into each invocation's record: `.output…` the value at that path in each row of the node's output,
 * `.inputs.<name>…` the same in the input's source node (when the argument is one node's output).
 * An entity's cid, and a path into its data, is shown alone.
 *
 * **Views.** A node shows through `table` (its output rows), `invocation` (its one invocation), an
 * `InvocationPanel` renderer of its function, or an `EntityPanel` renderer of its output's one
 * entity; with none named, the first renderer of the
 * project's own, written into the resolved address, else the viewer's choice once its output loads.
 * A value or an entity shows through `tree` or an `EntityPanel` renderer. `code` of a node or the
 * root shows where its function is implemented, as the code version its level ran records it. A
 * view of no location is a page (`PagePanel`) or the experiment.
 *
 * The params an address uses: the keys of the workflows on its way, its node's row dims, and the
 * params its view declares. A view whose declared params are not all bound, a workflow key left
 * unbound, or a level still loading is a **problem** of the params (or of time), which a panel waits
 * out or a link can fix; an address naming nothing is a **structural** problem, and a browsing panel
 * at one closes (`useColumns`).
 */

import { EntityPanel, InvocationPanel, PagePanel, type AnyRenderer } from 'fxtr-view'
import { renderersFor, type DispatchRegistry } from 'fxtr-view-shell-util'
import type { KeyValue, Level, SchemaJson, StepDoc } from '../types'
import { schemaAt } from '../model/schema'
import { isProjectRenderer } from '../views/registry'
import { VIEWS, formatAddress, type Address, type Location, type Params } from './address'

/** A level as the catalog has it so far. */
export type LevelLookup =
  | { status: 'ready'; level: Level }
  | { status: 'pending' }
  | { status: 'error'; error: unknown }

export interface Catalog {
  /** The pathname of the job's root workflow invocation. */
  root: string
  registry: DispatchRegistry
  /** The level of the workflow invocation at `pathname`, as loaded so far; asking for one starts
   *  its load. */
  level: (pathname: string) => LevelLookup
}

/** How a node's panel shows it: by default, its output rows, its one invocation, an
 *  `InvocationPanel` renderer of its function, or its output's one entity through an `EntityPanel`
 *  renderer. */
export type NodeView =
  | { kind: 'default' }
  | { kind: 'table' }
  | { kind: 'invocation' }
  | { kind: 'renderer'; id: string }
  | { kind: 'outputRenderer'; id: string }

/** How a value's panel shows it. */
export type ValueView =
  | { kind: 'default' }
  | { kind: 'tree' }
  | { kind: 'renderer'; id: string }

export type Target =
  /** A node's invocations. `levelPath`: the step path of the level it is a node of. `inputs`: the
   *  source node of each of its arguments that is one node's output (`inputSource`). */
  | { kind: 'node'; step: StepDoc; levelPath: readonly string[]; view: NodeView; inputs: Readonly<Record<string, StepDoc>> }
  /** The value at `path` in each row of a node's output. `schema`: of that value; null where the
   *  path goes on through an entity reference (only the data says what is there). */
  | { kind: 'value'; step: StepDoc; levelPath: readonly string[]; path: readonly string[]; schema: SchemaJson | null; view: ValueView }
  /** The root workflow's own invocation, through a renderer of its own (null: the default panel). */
  | { kind: 'run'; workflowName: string; renderer: string | null }
  | { kind: 'entity'; cid: string; path: readonly string[]; view: string | null }
  | { kind: 'page'; name: string }
  | { kind: 'experiment_readme' }
  /** Where the function `name` is implemented, as `codeVersion` (a CID) records it; null: as the
   *  backend's registry has it. */
  | { kind: 'code'; name: string; codeVersion: string | null }

export interface Resolution {
  /** The address, its default view written in when the registry decides it. */
  address: Address
  /** The params of `env` the address uses (`takes`): with the address, what the panel shows, as
   *  one string (`text`). */
  params: Params
  text: string
  /** Every binding the panel has: inherited, with its own over them. */
  env: Params
  /** The params its view reads, from `env`. */
  viewParams: Params
  /** Every param its address could use, bound or not. */
  takes: readonly string[]
  target: Target | null
  /** Why it shows nothing; null when it shows its target. */
  problem: string | null
  /** The problem is with the address itself (it names nothing), not with the params. */
  structural: boolean
  /** The problem is a level still loading. */
  loading: boolean
}

class Unresolved extends Error {
  /** The params the address uses as far as it was resolved: the keys of the workflows walked. */
  takes: readonly string[] = []

  constructor(message: string, readonly structural: boolean, readonly loading = false) {
    super(message)
  }
}

function pick(env: Params, names: readonly string[]): Params {
  return Object.fromEntries(names.filter((name) => Object.hasOwn(env, name)).map((name) => [name, env[name]!]))
}

function rowDims(step: StepDoc): string[] {
  return step.type.dims.map(([dim]) => dim)
}

/** The renderer with `id` among the registry's, in any slot. */
function rendererWithId(registry: DispatchRegistry, id: string): AnyRenderer | undefined {
  return registry.renderers.find((renderer) => renderer.id === id)
}

/** The id of the view the view `id` is declared within; null for one within nothing. */
export function parentViewOf(registry: DispatchRegistry, id: string | null): string | null {
  return id === null ? null : rendererWithId(registry, id)?.within ?? null
}

/** Whether `id` names a page. */
export function isPage(registry: DispatchRegistry, id: string | null): boolean {
  return id !== null && renderersFor(registry, PagePanel, id).some((renderer) => renderer.id === id)
}

/**
 * The pathname of a workflow node's run at `key` (its mapped then replica dims, by name), as the
 * runner names it: the parent's pathname, `/`, the node's name, then `[dim:value,…]` with values
 * JSON-encoded (`append_mapped_key` in fxtr's `experiment/workflows.py`). A number is written as
 * JavaScript writes it, which matches Python's for integers and for floats with a fraction; a whole
 * float key (`1.0`) would be named differently, and its run reads as missing.
 */
export function childPathname(parent: string, name: string, key: readonly (readonly [string, KeyValue])[]): string {
  const base = `${parent}/${name}`
  return key.length === 0 ? base : `${base}[${key.map(([dim, value]) => `${dim}:${JSON.stringify(value)}`).join(',')}]`
}

/** The level reached along `steps` (workflow steps from the root), and the keys of the workflows
 *  on the way. */
function walk(steps: readonly string[], env: Params, catalog: Catalog): { level: Level; keys: string[] } {
  let pathname = catalog.root
  let level = lookup(catalog, pathname, 'the run')
  const keys: string[] = []
  for (const [index, name] of steps.entries()) {
    const node = level.steps.find((step) => step.name === name)
    const where = index === 0 ? 'the run' : steps.slice(0, index).join('/')
    if (!node) throw new Unresolved(`${where} has no step ${name}`, true)
    if (node.kind !== 'workflow') throw new Unresolved(`${name} is a ${node.kind}, not a workflow with steps inside`, true)
    const dims = [...node.mapOver, ...node.replicaDims]
    keys.push(...dims)
    try {
      const unbound = dims.filter((dim) => !Object.hasOwn(env, dim))
      if (unbound.length > 0) throw new Unresolved(`pick ${unbound.join(', ')} to look inside ${name}`, false)
      pathname = childPathname(pathname, name, dims.map((dim) => [dim, env[dim]!] as const))
      level = lookup(catalog, pathname, `the run of ${name} at ${dims.map((dim) => `${dim}=${JSON.stringify(env[dim])}`).join(', ')}`)
    } catch (error) {
      if (error instanceof Unresolved) error.takes = keys
      throw error
    }
  }
  return { level, keys }
}

function lookup(catalog: Catalog, pathname: string, what: string): Level {
  const found = catalog.level(pathname)
  if (found.status === 'pending') throw new Unresolved(`loading ${what}…`, false, true)
  if (found.status === 'error') throw new Unresolved(`${what} could not be read: ${String(found.error)}`, false)
  return found.level
}

export function resolve(address: Address, env: Params, catalog: Catalog): Resolution {
  let resolved = address
  let target: Target | null = null
  let takes: readonly string[] = []
  let declared: readonly string[] = []
  let problem: string | null = null
  let structural = false
  let loading = false
  try {
    const { location, view } = address
    if (view === VIEWS.trace || view === VIEWS.flowchart) throw new Unresolved(`the ${view} shows only in the centre`, true)
    if (location === null) {
      if (view === VIEWS.experimentReadme) target = { kind: 'experiment_readme' }
      else if (isPage(catalog.registry, view)) {
        declared = rendererWithId(catalog.registry, view!)!.params ?? []
        takes = declared
        target = { kind: 'page', name: view! }
      } else throw new Unresolved(view === null ? 'an address needs a view or a location' : `no page ${view}`, true)
    } else if (location.root === 'entity') {
      const shown = valueViewId(view, catalog.registry)
      if (shown !== null && shown !== VIEWS.tree && location.path.length > 0) throw new Unresolved(RENDERER_AT_PATH, false)
      target = { kind: 'entity', cid: location.cid, path: location.path, view: shown }
    } else if (view === VIEWS.code) {
      const code = resolveCode(location, env, catalog)
      takes = code.takes
      target = code.target
    } else {
      const step = resolveStep(location, view, env, catalog)
      resolved = step.address
      takes = step.takes
      declared = step.declared
      target = step.target
    }
    const missing = declared.filter((name) => !Object.hasOwn(env, name))
    if (missing.length > 0) throw new Unresolved(`this view needs ${missing.join(', ')} bound`, false)
  } catch (error) {
    if (!(error instanceof Unresolved)) throw error
    problem = error.message
    structural = error.structural
    loading = error.loading
    if (takes.length === 0) takes = error.takes
    target = null
  }
  const params = pick(env, takes)
  return {
    address: resolved, params, text: formatAddress(resolved, params), env, viewParams: pick(env, declared), takes, target,
    problem, structural, loading,
  }
}

const RENDERER_AT_PATH = 'a renderer shows a whole output or entity, not a field inside one: open this without a view'

/** Whether `id` names an `EntityPanel` renderer (of whatever type: only the data says which). */
function isEntityRenderer(registry: DispatchRegistry, id: string): boolean {
  return registry.renderers.some((renderer) => renderer.slot.symbol === EntityPanel.symbol && renderer.id === id)
}

/** A value's or an entity's view: `tree`, an `EntityPanel` renderer, or the default. */
function valueViewId(view: string | null, registry: DispatchRegistry): string | null {
  if (view === null || view === VIEWS.tree) return view
  if (!isEntityRenderer(registry, view)) throw new Unresolved(`no view ${view} of a value`, true)
  return view
}

function valueView(view: string | null, registry: DispatchRegistry): ValueView {
  const id = valueViewId(view, registry)
  return id === null ? { kind: 'default' } : id === VIEWS.tree ? { kind: 'tree' } : { kind: 'renderer', id }
}

/** The node whose output a node's argument `name` is, when it is one node's output as it is; null
 *  for an expression over several. */
export function inputSource(node: StepDoc, name: string, nodes: readonly StepDoc[]): StepDoc | null {
  const arg = node.args.find((candidate) => candidate.name === name)
  if (!arg) throw new Error(`${node.name} has no input ${name}`)
  const [only, ...others] = arg.expr
  if (typeof only !== 'object' || others.length > 0) return null
  return nodes.find((candidate) => candidate.id === only.ref) ?? null
}

/** The value at `path` in a node's rows; a path its schema does not have names nothing. */
function valueTarget(step: StepDoc, levelPath: readonly string[], path: readonly string[], view: ValueView): Target {
  if (view.kind === 'renderer' && path.length > 0) throw new Unresolved(RENDERER_AT_PATH, false)
  const schema = schemaAt(step.type.schema, path)
  if (schema === undefined) throw new Unresolved(`${step.name}'s output has no ${path.join('.')}`, true)
  return { kind: 'value', step, levelPath, path, schema, view }
}

/** The first `InvocationPanel` renderer of the project's own for `name`, not declared within
 *  another: what a node of that function shows with no view named. */
function defaultRenderer(registry: DispatchRegistry, name: string | null): AnyRenderer | undefined {
  if (name === null) return undefined
  return renderersFor(registry, InvocationPanel, name).find((renderer) => isProjectRenderer(renderer) && renderer.within === undefined)
}

/** The code of the function behind the node at `location`, or the root workflow's at `/`. */
function resolveCode(location: Location & { root: 'step' }, env: Params, catalog: Catalog): { takes: string[]; target: Target } {
  if (location.path.length > 0) throw new Unresolved('code shows a step or workflow, not a field of its record', true)
  const { steps } = location
  const { level, keys } = walk(steps.slice(0, -1), env, catalog)
  if (steps.length === 0) return { takes: keys, target: { kind: 'code', name: level.workflowName, codeVersion: level.codeVersion } }
  const name = steps.at(-1)!
  const node = level.steps.find((step) => step.name === name)
  if (!node) throw new Unresolved(`${steps.length === 1 ? 'the run' : steps.slice(0, -1).join('/')} has no step ${name}`, true)
  if (node.method === null) throw new Unresolved(`${name} is not a step or workflow, so it has no code`, true)
  return { takes: keys, target: { kind: 'code', name: node.method, codeVersion: node.codeVersion } }
}

function resolveStep(location: Location & { root: 'step' }, view: string | null, env: Params, catalog: Catalog) {
  const { steps, path } = location
  const levelPath = steps.slice(0, -1)
  const { level, keys } = walk(levelPath, env, catalog)
  const registry = catalog.registry
  const address = (resolvedView: string | null): Address => ({ view: resolvedView, location })

  if (steps.length === 0) {
    // The root workflow's own invocation, or what it returned.
    if (path.length === 0) {
      const renderers = renderersFor(registry, InvocationPanel, level.workflowName)
      const shown = view ?? defaultRenderer(registry, level.workflowName)?.id ?? null
      if (shown !== null && shown !== VIEWS.invocation && !renderers.some((renderer) => renderer.id === shown))
        throw new Unresolved(`no view ${shown} of ${level.workflowName}`, true)
      const renderer = shown === null || shown === VIEWS.invocation ? null : renderers.find((candidate) => candidate.id === shown)!
      return {
        address: address(shown), takes: [...keys, ...(renderer?.params ?? [])], declared: renderer?.params ?? [],
        target: { kind: 'run', workflowName: level.workflowName, renderer: renderer?.id ?? null } satisfies Target,
      }
    }
    if (path[0] !== 'output') throw new Unresolved(`the run's record has no ${path[0]} shown here: name its output`, true)
    const returned = level.result?.stepId ? level.steps.find((step) => step.id === level.result!.stepId) : level.result?.table
    if (!returned) throw new Unresolved('the run has not returned', false)
    return { address: address(view), takes: [...keys, ...rowDims(returned)], declared: [], target: valueTarget(returned, levelPath, path.slice(1), valueView(view, registry)) }
  }

  const name = steps.at(-1)!
  const node = level.steps.find((step) => step.name === name)
  if (!node) throw new Unresolved(`${levelPath.length === 0 ? 'the run' : levelPath.join('/')} has no step ${name}`, true)
  const takes = [...keys, ...rowDims(node)]

  if (path.length > 0) {
    if (path[0] === 'output') return { address: address(view), takes, declared: [], target: valueTarget(node, levelPath, path.slice(1), valueView(view, registry)) }
    if (path[0] === 'inputs') {
      const input = path[1]
      if (input === undefined) throw new Unresolved('name an input after `.inputs`', true)
      if (!node.args.some((arg) => arg.name === input)) throw new Unresolved(`${name} has no input ${input}`, true)
      const source = inputSource(node, input, level.steps)
      if (!source) throw new Unresolved(`${name}'s input ${input} is an expression, which is not shown yet`, false)
      return {
        address: address(view), takes: [...keys, ...rowDims(source)], declared: [],
        target: valueTarget(source, levelPath, path.slice(2), valueView(view, registry)),
      }
    }
    throw new Unresolved(`an invocation's record shows its output and inputs, not ${path[0]}`, true)
  }

  let nodeView: NodeView
  let declared: readonly string[] = []
  const shown = view ?? defaultRenderer(registry, node.method)?.id ?? null
  if (shown === null) nodeView = { kind: 'default' }
  else if (shown === VIEWS.table || shown === VIEWS.invocation) nodeView = { kind: shown }
  else {
    const renderer = node.method === null ? undefined
      : renderersFor(registry, InvocationPanel, node.method).find((candidate) => candidate.id === shown)
    if (renderer) {
      declared = renderer.params ?? []
      nodeView = { kind: 'renderer', id: shown }
    } else if (isEntityRenderer(registry, shown)) nodeView = { kind: 'outputRenderer', id: shown }
    else throw new Unresolved(`no view ${shown} of ${name}`, true)
  }
  const inputs = Object.fromEntries(node.args.flatMap((arg) => {
    const source = inputSource(node, arg.name, level.steps)
    return source ? [[arg.name, source]] : []
  }))
  return {
    address: address(shown), takes: [...takes, ...declared], declared,
    target: { kind: 'node', step: node, levelPath, view: nodeView, inputs } satisfies Target,
  }
}
