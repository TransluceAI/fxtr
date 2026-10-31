import { Suspense, useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { unwrap, useInvocation, type EntityID, type InvocationRecord, type Spawn } from 'fxtr-view'
import { fetchOutputPage, useQuery } from '../api/browse'
import { retryAndReset } from '../api/live'
import { ErrorBoundary } from '../components/ErrorBoundary'
import { InvocationProvenance, InvocationStatusLine } from '../components/InvocationInfo'
import { Loading, Problem } from '../components/Status'
import { isLive, runStateOf } from '../lib/progress'
import { structFields, typeLabel } from '../model/schema'
import { cx } from '../lib/cx'
import { useLevelOpener } from '../lib/openLevel'
import type { SchemaJson } from '../types'
import { EntityPanel } from './EntityPanel'
import styles from './DefaultInvocationPanel.module.css'

/** How a node's panel shows an invocation's values: the way the panels of its record's parts
 *  (`.output`, `.inputs.<name>`) at the same params show them. */
export interface InvocationValues {
  /** The output's type at a glance, and in full. */
  outputType: { label: string; full: string }
  /** The schema of one of the output's values, sketched while the invocation has none. */
  outputSchema: SchemaJson
  /** The output, once the invocation completed. */
  output: ReactNode
  /** An input's type at a glance, when its source is known. */
  inputType: (name: string) => string | null
  /** An input's value; null to show the input's entity. */
  input: (name: string) => ReactNode | null
}

/**
 * The built-in view of an invocation opened in a pane, when no renderer is chosen, top to bottom:
 *
 * - its status line (`InvocationStatusLine`), and its provenance under it as fine print
 *   (`InvocationProvenance`);
 * - its inputs by argument name, each a row (its type and row count) that expands to the value;
 * - for a workflow, the runs its body made with how many of their spawns have produced something;
 * - its output, last since it is the longest: the value (through `values`, or its result's
 *   entity), a sketch of its shape from the output's schema while it is queued or running, or its
 *   error when it failed.
 *
 * The pane's head names the invocation and its keys, so the body does not.
 */
export function DefaultInvocationPanel({ spawn, values }: { spawn: Spawn; values?: InvocationValues }) {
  return (
    <ErrorBoundary fallback={(error, reset) => <Problem error={error} onRetry={() => retryAndReset(reset)} />}>
      <Suspense fallback={<Loading what="loading invocation…" />}>
        <Body spawn={spawn} values={values} />
      </Suspense>
    </ErrorBoundary>
  )
}

function Body({ spawn, values }: { spawn: Spawn; values: InvocationValues | undefined }) {
  const record = unwrap(useInvocation(spawn.ref))
  const inputs = Object.entries(record.inputs)
  return (
    <div className={styles.panel}>
      <div className={styles.about}>
        <InvocationStatusLine spawn={spawn} />
        <InvocationProvenance spawn={spawn} />
        {spawn.ref.kind === 'workflow' && <OpenInTrace pathname={spawn.ref.pathname} />}
      </div>
      {inputs.length > 0 && (
        <section className={styles.section}>
          <h3 className={styles.heading}>Inputs</h3>
          <div className={styles.inputs}>
            {inputs.map(([name, id]) => <InputRow key={name} name={name} id={id} values={values} />)}
          </div>
        </section>
      )}
      {record.children.length > 0 && (
        <section className={styles.section}>
          <h3 className={styles.heading}>Runs</h3>
          <dl className="kv kv-args">
            {record.children.map((run) => (
              <div key={run.nodeName} className="kv-pair">
                <dt>{run.nodeName}</dt>
                <dd>
                  <span className="code-method">{run.name}</span>
                  <span className={styles.dim}>
                    {' · '}
                    {run.spawns.filter((s) => s.output !== null).length} / {run.spawns.length} spawns done
                  </span>
                </dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      <section className={styles.section}>
        <h3 className={styles.heading}>
          Output
          {values && <span className={styles.type} title={values.outputType.full}>{values.outputType.label}</span>}
        </h3>
        <OutputBody record={record} values={values} />
      </section>
    </div>
  )
}

/** A workflow invocation's button showing its level in the trace; none while the trace shows it. */
function OpenInTrace({ pathname }: { pathname: string }) {
  const opener = useLevelOpener()
  if (!opener || opener.shown === pathname) return null
  return (
    <button className={styles.openTrace} onClick={() => opener.open(pathname)} title="show this invocation's steps in the trace">
      Open in trace
    </button>
  )
}

function OutputBody({ record, values }: { record: InvocationRecord; values: InvocationValues | undefined }) {
  const state = runStateOf(record.state)
  if (state === 'failed')
    return <div className={styles.error}>{record.error ? `${record.error.typeName}: ${record.error.message}` : 'failed'}</div>
  if (isLive(state)) return <Sketch schema={values?.outputSchema ?? null} />
  if (record.output === null) return <span className="detail-none">no output recorded</span>
  return <>{values ? values.output : <EntityPanel cid={record.output.cid} />}</>
}

/** The shape of a value not there yet: a placeholder per field of a struct, or one for anything
 *  else. */
function Sketch({ schema }: { schema: SchemaJson | null }) {
  const fields = schema === null ? null : structFields(schema)
  if (fields === null) return <div className={styles.sketch}><span className={styles.bar} /></div>
  return (
    <dl className={cx(styles.sketch, 'kv kv-args')}>
      {Object.keys(fields).map((name) => (
        <div key={name} className="kv-pair">
          <dt>{name}</dt>
          <dd><span className={styles.bar} /></dd>
        </div>
      ))}
    </dl>
  )
}

/** How many rows an array has, from its first page: `1 row`, `5 rows`, or `2+ rows` when the
 *  server does not count them. */
function rowCount({ total, rows, hasMore }: { total: number | null; rows: readonly unknown[]; hasMore: boolean }): string {
  if (total === null && hasMore) return `${rows.length + 1}+ rows`
  const count = total ?? rows.length
  return `${count} ${count === 1 ? 'row' : 'rows'}`
}

/** One input: its name, type and how many rows the invocation was given on a line (the input's
 *  cid on hover), expanding to its value. Its type is `values`', else its array's own. */
function InputRow({ name, id, values }: { name: string; id: EntityID; values: InvocationValues | undefined }) {
  const [open, setOpen] = useState(false)
  // The input is an array entity: one row of it says its type and how many rows it has.
  const query = useQuery(JSON.stringify(['input-head', id.cid]),
    (signal) => fetchOutputPage({ cid: id.cid }, { offset: 0, first: 1 }, signal))
  const page = query.value
  const type = values?.inputType(name) ?? (page?.type ? typeLabel(page.type) : null)
  return (
    <div className={styles.input}>
      <button className={styles.inputHead} aria-expanded={open} onClick={() => setOpen(!open)}>
        <ChevronRight size={12} className={cx(styles.chevron, open && styles.isOpen)} aria-hidden />
        <span className={styles.inputName}>{name}</span>
        {type && <span className={styles.type}>{type}</span>}
        <span className={styles.summary} title={id.cid}>
          {query.error ? 'rows unavailable' : page ? rowCount(page) : '…'}
        </span>
      </button>
      {open && <div className={styles.inputValue}>{values?.input(name) ?? <EntityPanel cid={id.cid} />}</div>}
    </div>
  )
}
