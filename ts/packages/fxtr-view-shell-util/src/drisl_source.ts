// An entity loader over a backend that serves entities' raw DRISL (DAG-CBOR) bytes by cid, many
// per request: fetch in batches, decode, and convert each tree to fxtr-view's EntityData, every CID
// leaf (DAG-CBOR tag 42) becoming an EntityID. The `$type` tag is in the bytes, so nothing else is
// asked.

import * as dagCbor from "@ipld/dag-cbor";
import { CID } from "multiformats/cid";
import { EntityDataError, EntityID, isEntityData, type DataValue, type EntityData, type EntityID as EntityIDType } from "fxtr-view";
import type { EntityLoader } from "./cached_loader_hook";

/** A decoded DRISL value as a DataValue: CIDs become EntityIDs, everything else is kept. */
export function toDataValue(decoded: unknown): DataValue {
  const cid = CID.asCID(decoded);
  if (cid) return new EntityID(cid.toString());
  if (decoded === null || decoded === undefined) return null;
  switch (typeof decoded) {
    case "boolean":
    case "number":
    case "bigint":
    case "string":
      return decoded;
  }
  if (decoded instanceof Uint8Array) return decoded;
  if (Array.isArray(decoded)) return decoded.map(toDataValue);
  if (typeof decoded === "object") {
    return Object.fromEntries(Object.entries(decoded as Record<string, unknown>).map(([k, v]) => [k, toDataValue(v)]));
  }
  throw new EntityDataError(`unexpected decoded value of type ${typeof decoded}`);
}

/** Decoded DRISL bytes as entity data. Throws `EntityDataError` unless the bytes hold a tagged map. */
export function decodeEntityData(bytes: Uint8Array, cid: string): EntityData {
  const data = toDataValue(dagCbor.decode(bytes));
  if (!isEntityData(data)) throw new EntityDataError(`${cid} did not decode to tagged entity data`);
  return data;
}

export interface BatchOptions {
  /** The most cids one request asks for; the backend's own limit must be at least this. */
  readonly batchSize?: number;
  /** The most requests awaiting a response at once. */
  readonly maxInFlight?: number;
}

interface Waiter {
  readonly cid: string;
  readonly resolve: (data: EntityData) => void;
  readonly reject: (error: unknown) => void;
}

/**
 * A loader over a backend serving many entities per request: a POST to `entitiesUrl` with JSON
 * `{"cids": [...]}`, answered by a DAG-CBOR list in the same order, each item the entity's DRISL
 * bytes or null when it is not stored (debug_web's `POST /api/entities`).
 *
 * Loads are queued and sent in batches, the first once the current task ends (so a render that
 * mounts many loaders asks for them together), with at most `maxInFlight` requests outstanding.
 * The queue keeps filling while those are out, so under load each request carries a full batch,
 * and however many entities a page asks for, the browser holds only a handful of requests.
 */
export function createBatchedDrislEntityLoader(
  entitiesUrl: string,
  { batchSize = 100, maxInFlight = 4 }: BatchOptions = {},
): EntityLoader {
  if (!(Number.isInteger(batchSize) && batchSize >= 1))
    throw new RangeError(`batchSize must be a positive integer, got ${batchSize}`);
  if (!(Number.isInteger(maxInFlight) && maxInFlight >= 1))
    throw new RangeError(`maxInFlight must be a positive integer, got ${maxInFlight}`);
  const queue: Waiter[] = [];
  let inFlight = 0;
  let scheduled = false;
  const pump = () => {
    scheduled = false;
    while (inFlight < maxInFlight && queue.length > 0) {
      inFlight += 1;
      void loadBatch(entitiesUrl, queue.splice(0, batchSize)).finally(() => {
        inFlight -= 1;
        pump();
      });
    }
  };
  return (id: EntityIDType) =>
    new Promise<EntityData>((resolve, reject) => {
      queue.push({ cid: id.cid, resolve, reject });
      if (!scheduled && inFlight < maxInFlight) {
        scheduled = true;
        setTimeout(pump, 0);
      }
    });
}

/** One request for a batch, settling every waiter in it; never rejects. A failed request fails
 *  each of its entities; otherwise each settles on its own bytes. */
async function loadBatch(entitiesUrl: string, batch: readonly Waiter[]): Promise<void> {
  let items: unknown[];
  try {
    const res = await fetch(entitiesUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ cids: batch.map((waiter) => waiter.cid) }),
    });
    if (!res.ok) throw new Error(`entities request: ${res.status} ${res.statusText}: ${await res.text()}`);
    const decoded: unknown = dagCbor.decode(new Uint8Array(await res.arrayBuffer()));
    if (!Array.isArray(decoded) || decoded.length !== batch.length)
      throw new EntityDataError(`entities response is not a list of ${batch.length} items`);
    items = decoded;
  } catch (error) {
    for (const waiter of batch) waiter.reject(error);
    return;
  }
  batch.forEach((waiter, i) => {
    const item = items[i];
    try {
      if (item === null) throw new Error(`entity ${waiter.cid}: not stored`);
      if (!(item instanceof Uint8Array)) throw new EntityDataError(`entity ${waiter.cid}: the response item is not bytes`);
      waiter.resolve(decodeEntityData(item, waiter.cid));
    } catch (error) {
      waiter.reject(error);
    }
  });
}
