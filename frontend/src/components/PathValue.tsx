import { createContext, useContext, useEffect, useMemo } from 'react'
import { atom } from 'jotai'
import { isDataMap, isEntityID, useEntity, type DataValue, type EntityID } from 'fxtr-view'
import { DataTree, type CollapsedPaths } from 'fxtr-view-cbor-tree'
import { EntityPanel } from '../views/EntityPanel'

/**
 * `value` followed along `rest` (`done` is the path to `value`, for messages): through an entity
 * reference by loading the entity, then into its fields and list items. What it reaches is shown
 * through the entity's panel when it is an entity, and as a data tree otherwise. A panel showing a
 * value at a path renders it, and sets its link base to that value (`lib/linkBase.ts`), so the
 * links shown here sit at paths inside it. `stateKey` keeps a data tree's collapsed parts apart per
 * what it shows.
 *
 * A path the data cannot have (a field a map lacks, or any step into a value that is neither a map
 * nor a list) is reported through `PathMissingContext`: the panel's address names nothing, which
 * its resolution cannot tell when the path goes on through an entity reference. A list too short
 * or a null on the way is this data's, not the address's, and is only said.
 */
export function PathValue({ value, rest, done, stateKey }: {
  value: DataValue
  rest: readonly string[]
  done: readonly string[]
  stateKey: string
}) {
  if (rest.length === 0)
    return (
      <div className="view-panel">
        {isEntityID(value) ? <EntityPanel cid={value.cid} /> : <ValueTree value={value} stateKey={stateKey} linkPaths />}
      </div>
    )
  if (isEntityID(value)) return <EntityHop id={value} rest={rest} done={done} stateKey={stateKey} />
  const [segment, ...more] = rest as [string, ...string[]]
  const child = Array.isArray(value) ? value[Number(segment)] : isDataMap(value) ? value[segment] : undefined
  if (child === undefined)
    return <Missing text={`no ${segment} in ${done.join('.') || 'the value'}`} namesNothing={value !== null && !Array.isArray(value)} />
  return <PathValue value={child} rest={more} done={[...done, segment]} stateKey={stateKey} />
}

/** Told when the value shown has no place at the path its panel's address names (`PathValue`);
 *  null where nothing is told, or the value shown is not yet the one the address names. */
export const PathMissingContext = createContext<(() => void) | null>(null)

function Missing({ text, namesNothing }: { text: string; namesNothing: boolean }) {
  const report = useContext(PathMissingContext)
  useEffect(() => {
    if (namesNothing) report?.()
  }, [namesNothing, report])
  return <span className="detail-none">{text}</span>
}

function EntityHop({ id, rest, done, stateKey }: { id: EntityID; rest: readonly string[]; done: readonly string[]; stateKey: string }) {
  const result = useEntity(id)
  if (result.status === 'loading') return <span className="detail-none">loading {done.join('.') || 'the value'}…</span>
  if (result.status === 'error') return <span role="alert">{String(result.error)}</span>
  return <PathValue value={result.value as DataValue} rest={rest} done={done} stateKey={stateKey} />
}

/** A value that is not an entity, as a data tree. */
export function ValueTree({ value, stateKey, linkPaths = false }: { value: DataValue; stateKey: string; linkPaths?: boolean }) {
  const collapsed = useMemo(() => atom<CollapsedPaths>({}), [stateKey])
  return <DataTree value={value} collapsed={collapsed} linkPaths={linkPaths} />
}
