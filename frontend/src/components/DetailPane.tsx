import { useCallback, useMemo, useRef } from 'react'
import { atom } from 'jotai'
import { CidText, EntityID, InvocationPanel, LinkBase, PagePanel, useEntity, type DataValue, type JsonValue, type Spawn } from 'fxtr-view'
import { RenderInvocation, RenderSlot } from 'fxtr-view-shell-util'
import { DefaultInvocationPanel } from '../views/DefaultInvocationPanel'
import { CodeBody } from './CodePane'
import { ExperimentReadmeBody } from './ExperimentReadme'
import { PanelHead } from './PanelHead'
import { OpenEntityContext, type EntityOpener } from '../lib/openEntity'
import { LevelStateContext, useLevelState, type LevelState } from '../lib/levelState'
import type { Params } from '../panes/address'
import type { PanelInstance } from '../panes/columns'
import type { ColumnsState } from '../panes/useColumns'
import type { Target } from '../panes/resolve'
import { StepPanel } from './StepPanel'
import { PathMissingContext, PathValue } from './PathValue'
import { panelBase } from '../lib/linkBase'
import { EntityPanel } from '../views/EntityPanel'
import { useKeepScroll } from '../lib/keepScroll'
import styles from './DetailPane.module.css'

interface Props {
  jobId: string
  panel: PanelInstance
  columns: ColumnsState
  /** The job's root workflow invocation, which pages and its own views are handed. */
  run: Spawn
}

/**
 * One panel of the detail view: its head (`PanelHead`: the address it shows, typeset, its params,
 * its controls, and the address card on hover), and the body of what its address shows.
 *
 * Links inside follow from this panel (`OpenEntityContext`), and its contents read its bindings as
 * their slice, and the level of the node it shows as theirs; a pick made inside (a slice control)
 * sets them (`ColumnsState.setBindings`).
 */
export function DetailPane({ jobId, panel, columns, run }: Props) {
  const level = useLevelState()
  const state = columns.stateOf(panel.id)
  const { resolution, address } = state
  const opener = useMemo<EntityOpener>(
    () => ({
      originId: panel.id,
      viewId: address?.view ?? null,
      relative: true,
      follow: (target, options) => columns.follow(target, panel.id, options),
      hover: (target) => columns.hover(target, panel.id),
      linkState: (target) => columns.linkState(target, panel.id),
    }),
    [panel.id, address?.view, columns.follow, columns.hover, columns.linkState],
  )
  const env = useStable(resolution.env)
  const selected = useStable(columns.selectedBelowOf(panel.id))
  const { setBindings } = columns
  const target = resolution.target
  const levelPath = target?.kind === 'node' || target?.kind === 'value' ? target.levelPath : level.levelPath
  const panelState = useMemo<LevelState>(() => ({
    ...level,
    slice: env,
    selected,
    levelPath,
    setSlice: (next) => setBindings(panel.id, typeof next === 'function' ? next(env) : next),
  }), [level, env, selected, levelPath, panel.id, setBindings])
  // A click in a parent that navigates this panel, or changes its params, keeps where it was scrolled.
  const rootRef = useRef<HTMLDivElement>(null)
  useKeepScroll(rootRef, resolution.text)
  const { namesNothing } = columns
  const missing = useCallback(() => namesNothing(panel.id, resolution.text), [namesNothing, panel.id, resolution.text])
  const setStepView = (view: string) => {
    if (address?.location?.root !== 'step') throw new Error('only a step panel chooses among a step\'s views')
    columns.setAddress(panel.id, { ...address, view })
  }
  const setEntityView = (view: string | null) => {
    if (!address) throw new Error('a panel naming nothing has no view to choose')
    columns.setAddress(panel.id, { ...address, view })
  }

  return (
    <LevelStateContext.Provider value={panelState}>
      <OpenEntityContext.Provider value={opener}>
        <div className="detail" ref={rootRef}>
          <PanelHead panel={panel} columns={columns} onView={setStepView} />
          {/* Hidden rather than unmounted while collapsed, so the body keeps its state. */}
          <div style={{ display: panel.collapsed ? 'none' : 'contents' }}>
            {resolution.problem !== null || target === null ? (
              <div className="detail-scroll">
                <div className={styles.problem} role="note">
                  {resolution.loading ? <span className="detail-none">{resolution.problem}</span> : <>
                  <b>{resolution.structural ? 'This address names nothing here.' : 'Nothing to show yet.'}</b> {resolution.problem}
                </>}
                </div>
              </div>
            ) : (
              // Keyed by what it shows, so a panel navigated to something else starts afresh.
              // Links inside sit in what this panel shows, so the panels they open follow it.
              <LinkBase base={address ? panelBase(panel.id, address) : null}>
                <PathMissingContext.Provider value={missing}>
                  <TargetBody key={targetKey(target)} jobId={jobId} target={target} params={resolution.viewParams}
                    run={run} onEntityView={setEntityView} />
                </PathMissingContext.Provider>
              </LinkBase>
            )}
          </div>
        </div>
      </OpenEntityContext.Provider>
    </LevelStateContext.Provider>
  )
}

/** A value kept at one identity while it stays equal (as JSON), for a value derived afresh each
 *  render that hooks below depend on. */
function useStable<T>(value: T): T {
  const kept = useRef({ json: JSON.stringify(value), value })
  const json = JSON.stringify(value)
  if (json !== kept.current.json) kept.current = { json, value }
  return kept.current.value
}

/** What a body is keyed by: what it shows, apart from its params (a param change keeps it, and its
 *  queries refresh). */
function targetKey(target: Target): string {
  switch (target.kind) {
    case 'node':
      return `node:${[...target.levelPath, target.step.name].join('/')}:${JSON.stringify(target.view)}`
    case 'value':
      return `value:${[...target.levelPath, target.step.name].join('/')}.${target.path.join('.')}:${JSON.stringify(target.view)}`
    case 'run':
      return `run:${target.renderer}`
    case 'entity':
      // The entity's view is left out: switching it keeps the panel's state, as its dropdown did.
      return `entity:${target.cid}.${target.path.join('.')}`
    case 'page':
      return `page:${target.name}`
    case 'experiment_readme':
      return 'experiment_readme'
    case 'code':
      return `code:${target.name}:${target.codeVersion}`
  }
}

function TargetBody({ jobId, target, params, run, onEntityView }: {
  jobId: string
  target: Target
  params: Params
  run: Spawn
  onEntityView: (view: string | null) => void
}) {
  const storage = useMemo(() => atom<JsonValue>(null), [])
  switch (target.kind) {
    case 'experiment_readme':
      return (
        <div className="detail-scroll">
          <ExperimentReadmeBody />
        </div>
      )
    case 'code':
      return (
        <div className="detail-scroll">
          <CodeBody name={target.name} codeVersion={target.codeVersion} />
        </div>
      )
    case 'entity':
      if (target.path.length > 0) return <EntityPath cid={target.cid} path={target.path} />
      return (
        <div className="detail-scroll">
          <div className={styles.cid}>
            <CidText cid={target.cid} />
          </div>
          <EntityPanel cid={target.cid} view={target.view} onView={onEntityView} />
        </div>
      )
    case 'node':
      return <StepPanel jobId={jobId} step={target.step} inputs={target.inputs} view={target.view} params={params} />
    case 'value':
      return <StepPanel jobId={jobId} step={target.step} path={target.path} valueView={target.view} />
    case 'run':
      return (
        <div className="detail-scroll">
          {target.renderer === null ? <DefaultInvocationPanel spawn={run} /> : (
            <RenderInvocation slot={InvocationPanel} name={target.workflowName} renderer={target.renderer}
              invocations={[run]} storage={storage} params={params} />
          )}
        </div>
      )
    case 'page':
      return (
        <div className="detail-scroll">
          <RenderSlot slot={PagePanel} subjectType={target.name} renderer={target.name} run={run} params={params} storage={storage} />
        </div>
      )
  }
}

/** The value at a path in an entity's data: the entity loaded, then followed along the path. */
function EntityPath({ cid, path }: { cid: string; path: readonly string[] }) {
  const result = useEntity(useMemo(() => new EntityID(cid), [cid]))
  if (result.status === 'loading') return <div className="detail-scroll"><span className="detail-none">loading…</span></div>
  if (result.status === 'error') return <div className="detail-scroll"><span role="alert">{String(result.error)}</span></div>
  return (
    <div className="detail-scroll">
      <PathValue value={result.value as DataValue} rest={path} done={[]} stateKey={`${cid}/${path.join('/')}`} />
    </div>
  )
}
