import { ARRAY_TYPE, EntityInlineLink, shortCid, type EntityID } from 'fxtr-view'
import { shortName } from 'fxtr-view-cbor-tree'
import { RenderEntity, useRenderers } from 'fxtr-view-shell-util'
import { useEntityMetadata } from '../api/browse'

/** A chip's label: its type's own `EntityInlineLink` renderer when it has one, and otherwise its
 * type and short cid, the same short run `CidText` emphasizes in the full cid. The plain label needs
 * no entity payload, and metadata requests are batched, so it also shows while a renderer loads. An
 * array is always plain: its renderer would load every row for each chip in a table. */
export function EntityLabel({ id }: { id: EntityID }) {
  const { value } = useEntityMetadata(id.cid)
  const type = value?.typeTag
  const label = type ? shortName(type) : ''
  const own = useRenderers(EntityInlineLink, type && type !== ARRAY_TYPE ? type : null).find(
    (renderer) => renderer.subjectType === type,
  )
  const plain = <span title={type ?? id.cid}>{label ? `${label} · ` : ''}{shortCid(id.cid)}</span>
  if (own === undefined) return plain
  return <RenderEntity slot={EntityInlineLink} id={id} renderer={own.id} fallbacks={{ loading: plain, error: () => plain }} />
}
