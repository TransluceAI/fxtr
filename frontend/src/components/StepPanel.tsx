import { useContext, useMemo, useState, type ReactNode } from 'react'
import { atom, type PrimitiveAtom } from 'jotai'
import { EntityPanel as EntityPanelSlot, InvocationPanel, LinkBase, type AnyRenderer, type JsonValue, type ViewParams } from 'fxtr-view'
import { RenderEntity, RenderInvocation, useRenderers } from 'fxtr-view-shell-util'
import { ENTITY_DATA } from 'fxtr-view-cbor-tree'
import { fetchAllSpawns, fetchRunPage, useEntityMetadata, useQuery } from '../api/browse'
import { invocationDims, rowSliceFor, invocationSliceFor, spawnOf } from '../lib/invocations'
import { OUTPUT_VIEW, useLevelState, type PanelView } from '../lib/levelState'
import { isEntityOutput, outputIds, outputSourceFor, outputValues, useOutputRows, useSelectedRun } from '../lib/output'
import { defaultPanelView, panelViewKey, panelViewOptions } from '../lib/panelView'
import { aggregate } from '../lib/progress'
import { invocationOf } from '../model/level'
import type { NodeView, ValueView } from '../panes/resolve'
import type { LinkTarget } from '../panes/drag'
import { nodeBase, nodeLocation } from '../lib/linkBase'
import { DefaultInvocationPanel, type InvocationValues } from '../views/DefaultInvocationPanel'
import { formatType, returnType, typeLabel } from '../model/schema'
import { VIEWS, formatValue, type Rel } from '../panes/address'
import { PathMissingContext, PathValue, ValueTree } from './PathValue'
import type { KeyValue, OutputSource, StepDoc, TableRow } from '../types'
import { InvocationInfo } from './InvocationInfo'
import { PagedTable } from './PagedTable'
import { ViewSelect, type ViewOption } from './ViewSelect'
import { PendingInvocation } from './PendingInvocation'
import { StateGlyph } from './StateGlyph'
import { RetryLine } from './Status'
import { isDetailView, isProjectRendererId } from '../views/registry'
import styles from './StepPanel.module.css'

/** How many unfinished invocations the list above an output names before counting the rest. */
const PENDING_SHOWN = 12

/** The level's slice restricted to the node's invocation dims: the invocations its panel shows. */
function useInvocationSlice(step: StepDoc): Record<string, KeyValue> {
  const { slice } = useLevelState()
  return useMemo(() => invocationSliceFor(step, slice), [step, slice])
}

/** The level's slice restricted to every dim of the node's output: the rows its panel shows. */
export function useRowSlice(step: StepDoc): Record<string, KeyValue> {
  const { slice } = useLevelState()
  return useMemo(() => rowSliceFor(step, slice), [step, slice])
}

/** The entity type of the output (read off its first row's type metadata, not its body), or null
 *  for an output that is not entities or has no rows yet. */
function useOutputTypeName(step: StepDoc, slice: Readonly<Record<string, KeyValue>>): string | null {
  const entities = isEntityOutput(step)
  const first = useOutputRows(step, slice, 1, entities)
  const cid = entities && first.rows?.[0] ? outputIds(step, first.rows)[0]!.cid : null
  return useEntityMetadata(cid).value?.typeTag ?? null
}

/** The `EntityPanel` renderers of the output's entity type (none for an output that is not
 *  entities, or before the type is known), the data tree left out: it is the panel's invocation
 *  output. */
function useOutputRenderers(typeName: string | null): readonly AnyRenderer[] {
  const renderers = useRenderers(EntityPanelSlot, typeName)
  return useMemo(() => renderers.filter((r) => r.id !== ENTITY_DATA), [renderers])
}

/** The renderers a node's panel offers: of its output's type, and of its function, details of the
 *  step left out (`isDetailView`). */
function usePanelViews(step: StepDoc) {
  const outputRenderers = useOutputRenderers(useOutputTypeName(step, useRowSlice(step)))
  const allInvocationRenderers = useRenderers(InvocationPanel, step.method)
  const invocationRenderers = useMemo(() => allInvocationRenderers.filter((r) => !isDetailView(r)), [allInvocationRenderers])
  return { outputRenderers, invocationRenderers }
}

type PanelViews = ReturnType<typeof usePanelViews>

/** The view a node's panel shows for the view its address names: the default (`defaultPanelView`)
 *  for none, and `invocation` as the output, which `StepPanel` shows its one invocation in place
 *  of. */
function shownView(view: NodeView, views: PanelViews): PanelView {
  switch (view.kind) {
    case 'default':
      return defaultPanelView(views.outputRenderers, views.invocationRenderers)
    case 'invocation':
    case 'table':
      return OUTPUT_VIEW
    case 'renderer':
      return { kind: 'invocation', renderer: view.id }
    case 'outputRenderer':
      return { kind: 'outputRenderer', renderer: view.id }
  }
}

/** The view a panel showing a node's whole output shows for the view its address names. */
function shownValueView(view: ValueView): PanelView {
  return view.kind === 'renderer' ? { kind: 'outputRenderer', renderer: view.id } : OUTPUT_VIEW
}

/** The view id an address names for a panel view (`table` for the output). */
function viewIdOf(view: PanelView): string {
  return view.kind === 'output' ? VIEWS.table : view.renderer
}

/** Whether a panel shows a node or its output through a renderer of the project's own
 *  (`isProjectRendererId`): a renderer of the step, or of its output's type. */
export function isCustomView(view: NodeView | ValueView): boolean {
  return (view.kind === 'renderer' || view.kind === 'outputRenderer') && isProjectRendererId(view.id)
}

/** In the head of a pane showing a node: the dropdown choosing among the entity renderers of its
 *  output's type (which changes the panel's view, `onView`), when there is a choice (the output
 *  too, only while it is the view shown), except on a view of the project's own (`isCustomView`)
 *  or of its function (`InvocationPanel` renderers are switched from the step card's views
 *  button); and the info button when the slice leaves one invocation. A panel showing one
 *  invocation (`invocation`, opened from a row) has neither: its body shows what they would. */
export function StepControls({ step, view, onView }: { step: StepDoc; view: NodeView; onView: (view: string) => void }) {
  if (view.kind === 'invocation') return null
  return (
    <span className={styles.controls}>
      {!isCustomView(view) && <StepViewSelect step={step} view={view} onView={onView} />}
      <OneInvocationInfo step={step} />
    </span>
  )
}

function OneInvocationInfo({ step }: { step: StepDoc }) {
  const run = useSelectedRun(step, useInvocationSlice(step))
  return run.total === 1 && run.firstSpawn ? <InvocationInfo spawn={run.firstSpawn} /> : null
}

function StepViewSelect({ step, view, onView }: { step: StepDoc; view: NodeView; onView: (view: string) => void }) {
  const views = usePanelViews(step)
  const shown = shownView(view, views)
  if (shown.kind === 'invocation') return null
  const value = panelViewKey(shown)
  // The output is the fallback a step without a renderer of the project's shows, and `table`
  // names it, but the menu does not offer it: an invocation's own panel shows its output. It is
  // listed only while shown, which the dropdown needs to read.
  const options = panelViewOptions(views.outputRenderers, [])
    .filter((option) => option.view.kind !== 'output' || panelViewKey(option.view) === value)
  const listed: ViewOption[] = options.map((option) => ({ key: panelViewKey(option.view), label: option.label, fromProject: option.fromProject, renderer: option.renderer }))
  // An output renderer the address names is listed before a row has loaded and told the output's
  // type, whose renderers the options list.
  if (shown.kind === 'outputRenderer' && !listed.some((option) => option.key === value))
    listed.push({ key: value, label: `output ${shown.renderer}`, fromProject: false })
  if (listed.length < 2) return null
  return (
    <ViewSelect
      options={listed}
      value={value}
      onChange={(key) => {
        const option = options.find((candidate) => panelViewKey(candidate.view) === key)
        if (!option) throw new Error(`no panel view ${key}`)
        onView(viewIdOf(option.view))
      }}
    />
  )
}

/**
 * A panel showing a node: its output for the invocations the panel's slice picks out (every row
 * of a node that was not run), narrowed to the rows at its slice of dims produced inside an
 * invocation, through the view chosen for it (`StepBody`). The output is a paged table when the
 * slice picks out several rows, and the one row's value as a data tree when it picks out one (an
 * entity decoded, through the `EntityPanel` slot). An output renderer is handed the one entity, or
 * with several rows the one picked in the table above it (`RowSplit`). With a `path`, only the value at that
 * path in the row's value is shown (`PathOutput`). Clicking a row follows a link to the node at the
 * row's keys; links in the output carry their place in it (`lib/linkBase.ts`). `invocation` shows
 * the one invocation the slice picks out (`OneInvocation`).
 */
export function StepPanel({ jobId, step, inputs = NO_INPUTS, path, view = { kind: 'default' }, valueView = { kind: 'default' }, params }: {
  jobId: string
  step: StepDoc
  /** The source node of each argument that is one node's output (the resolver's `inputs`). */
  inputs?: Readonly<Record<string, StepDoc>>
  /** The value at this path in a row's value (`[]`: the whole value) in place of the node's
   *  views. */
  path?: readonly string[]
  /** The view its address names, for the node. */
  view?: NodeView
  /** The view its address names, for the value at `path`: a renderer only for the whole value. */
  valueView?: ValueView
  /** What the renderer shown declares, bound. */
  params?: ViewParams
}) {
  const rowSlice = useRowSlice(step)
  const views = usePanelViews(step)
  const shown = shownView(view, views)
  // A row opens the node at the row's keys, from this panel, as a table of that one row.
  const rowTarget = (row: TableRow): LinkTarget => ({ rel: ROW_REL, params: rowKeys(step, row) })
  if (path !== undefined && valueView.kind === 'renderer') {
    if (path.length > 0) throw new Error('a renderer shows only a node\'s whole output')
    const outputShown = shownValueView(valueView)
    return (
      <StepBody jobId={jobId} step={step} shown={outputShown}
        output={(source, storage) => <Output step={step} source={source} slice={rowSlice} view={outputShown} storage={storage} />} />
    )
  }
  if (path !== undefined)
    return (
      <StepBody jobId={jobId} step={step} shown={OUTPUT_VIEW}
        output={(source) => <PathOutput step={step} source={source} slice={rowSlice} path={path} />} />
    )
  if (view.kind === 'invocation') return <OneInvocation step={step} inputs={inputs} />
  return (
    <StepBody jobId={jobId} step={step} shown={shown}
      {...(params === undefined ? {} : { params })}
      output={(source, storage) => (
        <Output step={step} source={source} slice={rowSlice} view={shown} storage={storage} rowLink={rowTarget} />
      )} />
  )
}

const NO_INPUTS: Readonly<Record<string, StepDoc>> = {}
const ROW_REL: Rel = { view: VIEWS.table, location: { kind: 'same' } }

/** `invocation`: the one invocation the panel's params pick out (`DefaultInvocationPanel`), its
 *  output and inputs shown as the panels of `.output` and `.inputs.<name>` would show them. */
function OneInvocation({ step, inputs }: { step: StepDoc; inputs: Readonly<Record<string, StepDoc>> }) {
  const slice = useInvocationSlice(step)
  const rowSlice = useRowSlice(step)
  const run = useSelectedRun(step, slice)
  if (run.error) return <Retry error={run.error} onRetry={run.reload} />
  if (run.total === null) return <None text="loading invocations…" />
  if (run.total !== 1 || !run.firstSpawn)
    return <None text={`${run.total} invocations are in this panel's params: bind ${step.mapOver.filter((dim) => !Object.hasOwn(slice, dim)).join(', ')} to pick one`} />
  const key = run.firstSpawn.key
  const returns = returnType(step.type, invocationDims(step))
  const values: InvocationValues = {
    outputType: { label: typeLabel(returns), full: formatType(returns) },
    outputSchema: step.type.schema,
    output: <InvocationOutput step={step} slice={slice} rowSlice={rowSlice} />,
    inputType: (name) => {
      const source = inputs[name]
      return source ? typeLabel(returnType(source.type, mappedDims(step, name))) : null
    },
    input: (name) => {
      const source = inputs[name]
      return source ? <InputValue source={source} slice={pickKeys(key, mappedDims(step, name))} /> : null
    },
  }
  return (
    <div className="detail-scroll">
      <DefaultInvocationPanel spawn={run.firstSpawn} values={values} />
    </div>
  )
}

/** The dims an invocation sees one value of in its argument `name`: the mapped dims it has. */
function mappedDims(step: StepDoc, name: string): string[] {
  const arg = step.args.find((candidate) => candidate.name === name)
  if (!arg) throw new Error(`${step.name} has no input ${name}`)
  return step.mapOver.filter((dim) => arg.dims.includes(dim))
}

function pickKeys(keys: Readonly<Record<string, KeyValue>>, dims: readonly string[]): Record<string, KeyValue> {
  return Object.fromEntries(dims.map((dim) => {
    if (!Object.hasOwn(keys, dim)) throw new Error(`the invocation's key has no ${dim}`)
    return [dim, keys[dim]!]
  }))
}

/**
 * An invocation's output in its panel, as `.output` at the panel's params shows it (`PathOutput`): its
 * rows at the panel's row slice. When that binds dims produced inside the invocation, a line says
 * which of its rows those are, with **Show all**, which leaves those dims out of the panel's params
 * (pinning the panel when it inherits them). A row of the table opens `.output` at its keys.
 */
function InvocationOutput({ step, slice, rowSlice }: {
  step: StepDoc
  slice: Readonly<Record<string, KeyValue>>
  rowSlice: Readonly<Record<string, KeyValue>>
}) {
  const source = outputSourceFor(step, rowSlice)
  if (!source) return <span className="detail-none">no output recorded</span>
  const produced = Object.keys(rowSlice).filter((dim) => !Object.hasOwn(slice, dim))
  const rowTarget = (row: TableRow): LinkTarget => ({ rel: OUT_REL, params: rowKeys(step, row) })
  return (
    <>
      {produced.length > 0 && <Narrowed step={step} slice={slice} rowSlice={rowSlice} produced={produced} />}
      <PathOutput step={step} source={source} slice={rowSlice} path={NO_PATH} inline rowLink={rowTarget} />
    </>
  )
}

const OUT_REL: Rel = { view: null, location: { kind: 'below', path: ['output'] } }

/** Which of the invocation's rows the panel's bindings of `produced` dims leave, and Show all. */
function Narrowed({ step, slice, rowSlice, produced }: {
  step: StepDoc
  slice: Readonly<Record<string, KeyValue>>
  rowSlice: Readonly<Record<string, KeyValue>>
  produced: readonly string[]
}) {
  const { setSlice } = useLevelState()
  const shown = useOutputRows(step, rowSlice, 1).total
  const all = useOutputRows(step, slice, 1).total
  const counts = shown === null || all === null ? '' : `${shown} of ${all} ${all === 1 ? 'row' : 'rows'} · `
  return (
    <div className={styles.narrowed}>
      <span>{counts}{produced.map((dim) => `${dim}=${formatValue(rowSlice[dim]!)}`).join(', ')}</span>
      <button className={styles.showAll} onClick={() =>
        setSlice((current) => Object.fromEntries(Object.entries(current).filter(([dim]) => !produced.includes(dim))))}>
        Show all
      </button>
    </div>
  )
}

/** An input as the invocation saw it: its source node's rows at the invocation's keys on the dims
 *  it maps over (`PathOutput`). Its links, and a row of its table, open the source's `.output`. */
function InputValue({ source, slice }: { source: StepDoc; slice: Readonly<Record<string, KeyValue>> }) {
  const { levelPath } = useLevelState()
  const output = outputSourceFor(source, slice)
  if (!output) return <span className="detail-none">no output recorded</span>
  const rowTarget = (row: TableRow): LinkTarget => ({
    rel: { view: null, location: { kind: 'absolute', location: nodeLocation(levelPath, source.name, ['output']) } },
    params: rowKeys(source, row),
  })
  return (
    <LinkBase base={nodeBase(nodeLocation(levelPath, source.name))}>
      <PathOutput step={source} source={output} slice={slice} path={NO_PATH} inline rowLink={rowTarget} />
    </LinkBase>
  )
}

const NO_PATH: readonly string[] = []

/** A row's keys by dim name. */
function rowKeys(step: StepDoc, row: TableRow): Record<string, KeyValue> {
  return Object.fromEntries(step.type.dims.map(([dim], index) => [dim, row.key[index]!]))
}

/**
 * The value at `path` in the node's output: in the one row the slice picks out, followed through
 * fields, list indices, and entity references (`PathValue`); for several rows, the table (only the
 * field's column, for a path of one field), whose rows open `rowLink`. A value that is an
 * entity is shown through the entity's panel, any other as a data tree. `inline`: within a body
 * that scrolls itself, rather than as one.
 */
function PathOutput({ step, source, slice, path, inline = false, rowLink }: {
  step: StepDoc
  source: OutputSource
  slice: Readonly<Record<string, KeyValue>>
  path: readonly string[]
  inline?: boolean
  rowLink?: (row: TableRow) => LinkTarget
}) {
  const { levelPath } = useLevelState()
  const missing = useContext(PathMissingContext)
  const head = useOutputRows(step, slice, 2)
  const rows = head.rows
  const scroll = (content: ReactNode) => (inline ? content : <div className="detail-scroll">{content}</div>)
  const none = (text: string) => scroll(<span className="detail-none">{text}</span>)
  if (head.error) return scroll(<RetryLine error={head.error} onRetry={head.reload} />)
  if (!rows) return none('loading output…')
  if (rows.length === 0) return none('no output recorded')
  if (rows.length > 1 || head.hasMore) {
    if (path.length > 1)
      return none(`${head.total ?? 'several'} rows are selected: slice to one to show ${path.join('.')}`)
    // One field's column: its cells' links sit in the node's rows (where the field is a level up
    // from the panel's value), so they are placed on the node instead.
    if (path.length === 1)
      return (
        <LinkBase base={nodeBase(nodeLocation(levelPath, step.name))}>
          <PagedTable table={step} source={source} version={step.version} hiddenDims={Object.keys(slice)} hideHead linkCells
            onlyValueColumn={path[0]!} />
        </LinkBase>
      )
    return <PagedTable table={step} source={source} version={step.version} hiddenDims={Object.keys(slice)} hideHead linkCells
      rowLink={rowLink} />
  }
  // The panel's link base is the value at the path (`DetailPane`), so the links in it sit inside
  // that; the row's keys are added here. A row retained from other params while this one loads
  // says nothing about the path.
  return scroll(
    <LinkBase at={{ keys: rowKeys(step, rows[0]!) }}>
      <PathMissingContext.Provider value={head.loading ? null : missing}>
        <PathValue value={outputValues(step, rows)[0]!} rest={path} done={NO_PATH} stateKey={step.id} />
      </PathMissingContext.Provider>
    </LinkBase>,
  )
}

/**
 * What a node's panel shows around the output: the invocations the level's slice picks out,
 * loading or none; an invocation renderer handed every spawn; a slice of one unfinished invocation
 * as its state, error, and log lines; and otherwise `output`, with the invocations that have not
 * finished listed above it.
 */
function StepBody({ jobId, step, shown, output, params }: {
  jobId: string
  step: StepDoc
  shown: PanelView
  /** For an invocation renderer: the params it declares, bound. */
  params?: ViewParams
  output: (source: OutputSource, storage: PrimitiveAtom<JsonValue>) => ReactNode
}) {
  const slice = useInvocationSlice(step)
  const run = useSelectedRun(step, slice)
  const storage = useMemo(() => atom<JsonValue>(null), [step.id, panelViewKey(shown)])
  const source = outputSourceFor(step, useRowSlice(step))

  if (run.error) return <Retry error={run.error} onRetry={run.reload} />
  if (step.browse) {
    if (run.total === null) return <None text="loading invocations…" />
    if (run.total === 0)
      return <None text={step.progress.counts?.total === 0 ? 'not run yet' : 'no invocation is in the slice'} />
  }
  if (shown.kind === 'invocation') {
    if (step.method === null || !step.browse) throw new Error(`${step.name} has no function to show invocations of`)
    return <InvocationRendererView step={step} slice={slice} name={step.method} renderer={shown.renderer} storage={storage}
      {...(params === undefined ? {} : { params })} />
  }
  if (run.total === 1 && run.first && run.first.state !== 'done') {
    return (
      <div className="detail-scroll">
        <PendingInvocation jobId={jobId} invocation={run.first} />
      </div>
    )
  }
  if (!source) return <None text="no output recorded" />
  return (
    <>
      {step.browse && <PendingList step={step} slice={slice} />}
      {output(source, storage)}
    </>
  )
}

/** The output in a pane: a paged table of the rows the slice picks out when there are several (a
 *  key column of a dim sliced on holds one value, so it is left out), with an output renderer the
 *  table over the picked row's entity (`RowSplit`), and the one row's value when there is one. */
function Output({ step, source, slice, view, storage, rowLink }: {
  step: StepDoc
  source: OutputSource
  slice: Readonly<Record<string, KeyValue>>
  view: PanelView
  storage: PrimitiveAtom<JsonValue>
  rowLink?: (row: TableRow) => LinkTarget
}) {
  // Two rows are enough to tell one from several.
  const head = useOutputRows(step, slice, 2)
  const rows = head.rows
  if (head.error) return <Retry error={head.error} onRetry={head.reload} />
  if (!rows) return <None text="loading output…" />
  if (rows.length === 0) return <None text="no output recorded" />
  if (rows.length > 1 || head.hasMore) {
    if (view.kind === 'outputRenderer')
      return <RowSplit step={step} source={source} slice={slice} renderer={view.renderer} first={rows[0]!} />
    return <PagedTable table={step} source={source} version={step.version} hiddenDims={Object.keys(slice)} rowLink={rowLink} hideHead linkCells />
  }
  return (
    <div className="detail-scroll">
      <LinkBase at={{ keys: rowKeys(step, rows[0]!) }}>
        <div className="view-panel">
          {isEntityOutput(step) ? (
            <RenderEntity slot={EntityPanelSlot} id={outputIds(step, rows)[0]!}
              renderer={view.kind === 'outputRenderer' ? view.renderer : ENTITY_DATA} storage={storage} />
          ) : (
            <ValueTree value={outputValues(step, rows)[0]!} stateKey={step.id} linkPaths />
          )}
        </div>
      </LinkBase>
    </div>
  )
}

/** An output renderer over several rows: the table above, and below it, through the renderer, the
 *  entity of the row last clicked in it (the output's first row until one is, and again once the
 *  slice changes). A row's renderer state lasts while it is the one shown. */
function RowSplit({ step, source, slice, renderer, first }: {
  step: StepDoc
  source: OutputSource
  slice: Readonly<Record<string, KeyValue>>
  renderer: string
  first: TableRow
}) {
  const sliceKey = JSON.stringify(slice)
  const [selected, setSelected] = useState<{ sliceKey: string; row: TableRow } | null>(null)
  const row = selected?.sliceKey === sliceKey ? selected.row : first
  const storage = useMemo(() => atom<JsonValue>(null), [row.id, renderer])
  return (
    <>
      <PagedTable table={step} source={source} version={step.version} hiddenDims={Object.keys(slice)} hideHead
        selectedRowId={row.id} onSelectRow={(next) => setSelected({ sliceKey, row: next })} />
      <div className="detail-scroll">
        <LinkBase at={{ keys: rowKeys(step, row) }}>
          <div className="view-panel">
            <RenderEntity slot={EntityPanelSlot} id={outputIds(step, [row])[0]!} renderer={renderer} storage={storage} />
          </div>
        </LinkBase>
      </div>
    </>
  )
}

/** An `InvocationPanel` renderer handed every invocation the slice picks out: loaded in full
 *  (server pages of 200) since the renderer's contract is the complete list. */
function InvocationRendererView({ step, slice, name, renderer, storage, params }: {
  step: StepDoc
  slice: Readonly<Record<string, KeyValue>>
  name: string
  renderer: string
  storage: PrimitiveAtom<JsonValue>
  params?: ViewParams
}) {
  const source = step.browse!
  const query = useQuery(JSON.stringify(['all-spawns', source, slice]),
    (signal) => fetchAllSpawns(source, { ...slice }, signal), { version: step.version })
  const spawns = useMemo(() => (query.value ?? []).flatMap((gql) => {
    const spawn = spawnOf(source.jobId, invocationOf(gql, invocationDims(step)))
    return spawn ? [spawn] : []
  }), [query.value, source.jobId, step])
  if (query.error) return <Retry error={query.error} onRetry={query.reload} />
  if (!query.value) return <None text="loading all selected invocations for this view…" />
  return (
    <div className="detail-scroll">
      <RenderInvocation slot={InvocationPanel} name={name} renderer={renderer} invocations={spawns} storage={storage}
        {...(params === undefined ? {} : { params })} />
    </div>
  )
}

function None({ text }: { text: string }) {
  return (
    <div className="detail-scroll">
      <span className="detail-none">{text}</span>
    </div>
  )
}

function Retry({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <div className="detail-scroll">
      <RetryLine error={error} onRetry={onRetry} />
    </div>
  )
}

/** The invocations among the selected that have not finished (their rows are not in the output
 *  yet): the first few, each a button picking its key so its state and log lines are one click
 *  away, and how many more. */
function PendingList({ step, slice }: { step: StepDoc; slice: Readonly<Record<string, KeyValue>> }) {
  const { slice: levelSlice, setSlice } = useLevelState()
  const source = step.browse!
  const query = useQuery(JSON.stringify(['unfinished', source, slice]),
    (signal) => fetchRunPage(source, { ...slice }, null, PENDING_SHOWN, { unfinished: true }, signal),
    { version: step.version, retainKey: JSON.stringify(['unfinished', source]) })
  const page = query.value
  if (!page || page.total === 0) return null
  const pending = page.items.map((spawn) => invocationOf(spawn, invocationDims(step)))
  return (
    <div className={styles.pending}>
      <span className={styles.pendingLabel}>{page.total} not finished</span>
      {pending.map((invocation) => {
        const key = Object.values(invocation.key).join('\u0000')
        const label = <>
          <StateGlyph glyph={aggregate([invocation.state])} r={3.5} />
          {Object.values(invocation.key).join(' · ')}
        </>
        return (
          <button key={key} className={styles.pendingKey} onClick={() => setSlice({ ...levelSlice, ...invocation.key })}>
            {label}
          </button>
        )
      })}
      {page.total > pending.length && <span className={styles.pendingLabel}>+{page.total - pending.length} more</span>}
    </div>
  )
}
