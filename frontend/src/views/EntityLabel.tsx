import { shortCid } from 'fxtr-view'
import { shortName } from 'fxtr-view-cbor-tree'
import { useEntityMetadata } from '../api/browse'

/** Chips need a type label, not the entity payload. Metadata requests are batched. The cid is
 * the same short run `CidText` emphasizes in the full cid. */
export function EntityLabel({ cid }: { cid: string }) {
  const { value } = useEntityMetadata(cid)
  const type = value?.typeTag
  const label = type ? shortName(type) : ''
  return <span title={type ?? cid}>{label ? `${label} · ` : ''}{shortCid(cid)}</span>
}
