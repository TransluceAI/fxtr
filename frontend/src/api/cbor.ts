/**
 * Decoding the raw DRISL (DAG-CBOR) bytes the backend serves for array cells (base64 in GraphQL).
 * Entity ids come back as `CID` objects (DAG-CBOR tag 42) and `bytes` as `Uint8Array`, so the UI
 * can tell them from strings. Whole entities are loaded by the view system's shell (`views/`).
 */

import * as dagCbor from '@ipld/dag-cbor'
import { CID } from 'multiformats/cid'
import type { Cell } from '../types'

export function decodeCbor(bytes: Uint8Array): Cell {
  return dagCbor.decode(bytes) as Cell
}

export function decodeBase64(b64: string): Uint8Array {
  const binary = atob(b64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes
}

export function decodeBase64Cbor(b64: string): Cell {
  return decodeCbor(decodeBase64(b64))
}

/** The value as a CID if it is one (a DAG-CBOR link), else null. */
export function asCid(value: unknown): CID | null {
  return CID.asCID(value)
}

export function isBytes(value: unknown): value is Uint8Array {
  return value instanceof Uint8Array
}

/** A struct or a list, as opposed to a leaf, an id, or bytes. */
export function isStruct(value: Cell): value is { [key: string]: Cell } {
  return (
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    !isBytes(value) &&
    asCid(value) === null
  )
}

export function isList(value: Cell): value is Cell[] {
  return Array.isArray(value)
}

export function toHex(bytes: Uint8Array, limit = 16): string {
  const shown = Array.from(bytes.slice(0, limit))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
  return bytes.length > limit ? `${shown}…` : shown
}
