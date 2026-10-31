import { useMemo, useState } from 'react'
import { atom } from 'jotai'
import {
  EntityID,
  EntityPanel as EntityPanelSlot,
  LinkBase,
  typeNameOf,
  useEntity,
  type JsonValue,
} from 'fxtr-view'
import { PlaceholderLoading, RenderEntity, chosenRenderer, useRenderers } from 'fxtr-view-shell-util'
import { RendererSelect } from '../components/RendererSelect'
import { ENTITY_DATA } from 'fxtr-view-cbor-tree'
import { VIEWS } from '../panes/address'
import styles from './EntityPanel.module.css'
import { useEntityMetadata } from '../api/browse'
import { PagedTable } from '../components/PagedTable'
import { LEAF_PROGRESS } from '../model/level'
import type { TableDescriptor } from '../types'
import { LoadingContent } from '../lib/loading'

const fallbacks = { loading: <LoadingContent><PlaceholderLoading /></LoadingContent> }

/** An entity opened in full: `RenderEntity` in the `EntityPanel` slot, so the registry decides how
 *  its type renders. The panel's storage atom is created per mount and not persisted. Its corner
 *  carries a dropdown of the type's views (its registered panels, then the data tree; shown even
 *  when the tree is the only one, so the choice is always visible), each renderer's row naming its
 *  component behind an info icon. In a panel of its own, the choice is the panel's address's view
 *  (`view`, set through `onView`), so back and forward return to it; read against the views the entity's type offers, a
 *  view it does not offer shows as the default. (A node's panel dispatches its one output entity to
 *  the same slot, with its own view dropdown.) */
export function EntityPanel({ cid, view, onView }: {
  cid: string
  /** The view chosen for it, and its setter; left out (an entity shown inside a value), the choice
   *  is the mount's own. */
  view?: string | null
  onView?: (view: string | null) => void
}) {
  const meta = useEntityMetadata(cid)
  const [local, setLocal] = useState<string | null>(null)
  const chosen = onView === undefined ? local : view ?? null
  const renderer = chosen === VIEWS.tree ? ENTITY_DATA : chosen
  const setRenderer = (id: string | null) => (onView ?? setLocal)(id === ENTITY_DATA ? VIEWS.tree : id)
  if (meta.error) return <div role="alert">{String(meta.error)} <button onClick={meta.reload}>retry</button></div>
  if (!meta.value) return <span>{meta.loading ? 'loading entity…' : 'entity not found'}</span>
  if (meta.value.typeTag === 'org.transluce.fxtr.core.Array')
    return <PagedArrayPanel cid={cid} renderer={renderer} setRenderer={setRenderer} />
  return <FullEntityPanel cid={cid} renderer={renderer} setRenderer={setRenderer} />
}

/** The renderer chosen for an `EntityPanel` (null: the default), and its setter. */
interface Choice {
  renderer: string | null
  setRenderer: (renderer: string | null) => void
}

function PagedArrayPanel({ cid, renderer: remembered, setRenderer }: { cid: string } & Choice) {
  const choices = useRenderers(EntityPanelSlot, 'org.transluce.fxtr.core.Array')
  const renderer = chosenRenderer(choices, remembered)
  const id = useMemo(() => new EntityID(cid), [cid])
  const storage = useMemo(() => atom<JsonValue>(null), [cid])
  const table: TableDescriptor = { id: cid, name: 'Array', type: { dims: [], schema: 'id' },
    dims: [], mapOver: [], replicaDims: [], columns: [],
    progress: LEAF_PROGRESS }
  // An array's rows are not a path into whatever holds it, so its links open their entities alone.
  return <LinkBase base={null}><div className="view-panel">
    <RendererSelect choices={choices} hostDefaultLabel="paged array" value={renderer} onChange={setRenderer} />
    {renderer ? <RenderEntity slot={EntityPanelSlot} id={id} storage={storage} renderer={renderer} fallbacks={fallbacks} />
      : <PagedTable key={cid} table={table} source={{cid}} />}
  </div></LinkBase>
}

function FullEntityPanel({ cid, renderer, setRenderer }: { cid: string } & Choice) {
  const id = useMemo(() => new EntityID(cid), [cid])
  const storage = useMemo(() => atom<JsonValue>(null), [])
  const choices = useRenderers(EntityPanelSlot, useTypeName(id))
  const chosen = chosenRenderer(choices, renderer)
  return (
    <div className="view-panel">
      <RenderEntity slot={EntityPanelSlot} id={id} storage={storage} fallbacks={fallbacks} {...(chosen === null ? {} : { renderer: chosen })} />
      <span className={styles.corner}>
        {choices.length > 0 && <RendererSelect choices={choices} value={chosen} onChange={setRenderer} />}
      </span>
    </div>
  )
}

/** The entity's type name once loaded; null before, and for data that will not render (the panel
 *  itself shows that error). */
function useTypeName(id: EntityID): string | null {
  const result = useEntity(id)
  if (result.status !== 'loaded') return null
  try {
    return typeNameOf(result.value)
  } catch {
    return null
  }
}
