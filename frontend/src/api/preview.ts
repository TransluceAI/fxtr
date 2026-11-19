/**
 * A step attempt's live preview: the value its body last published through `ctx.preview`, which
 * the backend keeps per attempt (debug_web's `GET /api/previews/{attemptId}`). The step pin's poll
 * carries the preview's `seq`, so the bytes are fetched again only when it grows.
 */

import { useEffect, useRef, useState } from 'react'
import * as dagCbor from '@ipld/dag-cbor'
import { isDataMap, isEntityData, markPartial, type DataValue, type EntityData } from 'fxtr-view'
import { toDataValue } from 'fxtr-view-shell-util'
import { viewerConfig } from '../config'
import type { GqlPreview } from './graphql'

/** The `$type` of a partial value's wire form (fxtr's `fxtr.entity_defns.partial.PARTIAL_TYPE_ID`). */
export const PARTIAL_TYPE_ID = 'org.transluce.fxtr.Partial'

/** A decoded preview: the value, as the entity data it is becoming. */
export interface Preview {
  /** The previewed value with every partial value's wrapper taken off, at any depth: each part
   *  reads as the record it will be, its fields not yet known null. */
  value: EntityData
  /** Whether the preview is a partial value (still being produced), not a finished one. */
  partial: boolean
  seq: number
}

/** A value with every partial value's wrapper (`{$type: PARTIAL_TYPE_ID, of}`) replaced by what it
 *  wraps, at any depth, each map that came out of a wrapper marked (`markPartial`), so a renderer
 *  can tell the parts still being produced from the finished ones (`isPartial`). */
function withoutPartials(value: DataValue): DataValue {
  if (Array.isArray(value)) return value.map(withoutPartials)
  if (!isDataMap(value)) return value
  if (value.$type === PARTIAL_TYPE_ID && isDataMap(value.of)) {
    const unwrapped = withoutPartials(value.of)
    return isDataMap(unwrapped) ? markPartial(unwrapped) : unwrapped
  }
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, withoutPartials(item)]))
}

/** A decoded preview's value and whether it is partial: the value with its partial wrappers taken
 *  off (see `Preview`). Throws unless it is tagged data. */
export function unwrapPreview(decoded: DataValue, seq: number): Preview {
  const partial = isDataMap(decoded) && decoded.$type === PARTIAL_TYPE_ID
  const value = withoutPartials(decoded)
  if (!isEntityData(value)) throw new Error('the preview is not tagged data')
  return { value, partial, seq }
}

/** Fetch and decode a preview; null when the attempt has none (any more). */
export async function fetchPreview(preview: GqlPreview, signal?: AbortSignal): Promise<Preview | null> {
  const { previewsBase, headers } = viewerConfig()
  if (previewsBase === null) return null
  const url = `${previewsBase}/${encodeURIComponent(preview.attemptId)}?seq=${preview.seq}`
  const response = await fetch(url, { signal, headers })
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`preview request failed: ${response.status}`)
  const seq = Number(response.headers.get('X-Fxtr-Preview-Seq') ?? preview.seq)
  return unwrapPreview(toDataValue(dagCbor.decode(new Uint8Array(await response.arrayBuffer()))), seq)
}

/** Which attempt's preview, up to which `seq`. */
interface PreviewMark {
  attemptId: string
  seq: number
}

/**
 * The preview a step pin names, fetched whenever its attempt changes or its `seq` grows. The last
 * preview stays shown while a newer one loads, so a streaming preview does not flicker; a failed
 * fetch keeps it too, and the next `seq` tries again.
 *
 * At most one fetch is in flight per attempt, and a newer `seq` never cancels it: a large preview
 * can take longer to fetch and decode than the pin's poll, and cancelling it for each newer `seq`
 * would never let one land. When it lands, the latest `seq` is fetched if it has moved meanwhile,
 * skipping the ones in between. Only a change of attempt (or unmounting) cancels a fetch.
 */
export function useStepPreview(preview: GqlPreview | null): Preview | null {
  const [shown, setShown] = useState<{ attemptId: string; preview: Preview } | null>(null)
  const [loading, setLoading] = useState<PreviewMark | null>(null)
  const [fetched, setFetched] = useState<PreviewMark | null>(null)
  const inFlight = useRef<AbortController | null>(null)
  const attemptId = preview?.attemptId ?? null
  const seq = preview?.seq ?? null
  useEffect(() => () => inFlight.current?.abort(), [attemptId])
  useEffect(() => {
    if (attemptId === null || seq === null) return
    if (loading?.attemptId === attemptId) return
    if (fetched?.attemptId === attemptId && fetched.seq >= seq) return
    const controller = new AbortController()
    inFlight.current = controller
    setLoading({ attemptId, seq })
    let landed = seq
    fetchPreview({ attemptId, seq, at: '' }, controller.signal)
      .then(
        (next) => {
          if (next === null || controller.signal.aborted) return
          landed = Math.max(seq, next.seq)
          setShown({ attemptId, preview: next })
        },
        () => {},
      )
      .finally(() => {
        if (controller.signal.aborted) return
        setFetched({ attemptId, seq: landed })
        setLoading(null)
      })
  }, [attemptId, seq, loading, fetched])
  if (attemptId === null || shown === null || shown.attemptId !== attemptId) return null
  return shown.preview
}
