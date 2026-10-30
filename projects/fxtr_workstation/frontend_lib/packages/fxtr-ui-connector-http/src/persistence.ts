/**
 * Persisted shapes for the HTTP connector's storage layer (design/04
 * §Storage, design/05 §Persistence). The connector owns the `Storage`
 * handle and the key schema; the session knows how to serialize /
 * rehydrate itself into / out of a `SerializedSession` blob.
 *
 * Key schema (namespaced so two backends on one origin don't collide):
 *
 *   fxtrhttp:<ns>:table            # shortId table (see shortid_table.ts)
 *   fxtrhttp:<ns>:sessions         # SessionRegistryEntry[]
 *   fxtrhttp:<ns>:session:<uuid>   # one SerializedSession
 *
 * Persisted state uses the **workstation wire form** (shortId-mode
 * escape: `["entity", shortId]`, `["service", shortId]`, `["op_result",
 * slot]`, `["inbox", slot, offset]`, …); the persisted shortId table
 * supplies shortId → stable-identifier resolution on rehydrate.
 *
 * What is *not* persisted, and why (design/05):
 *   - Entity load (and metadata) payloads — content-addressed and
 *     cheap to refetch, so their slots rehydrate **pending** and
 *     refire. Only invoke fills persist.
 *   - In-flight invokes — rehydrate as an errored ("interrupted") fill.
 *   - Live ws subscriptions / transient stubs — rehydrate broken
 *     (the connection generation they rode is gone).
 */

import type { JsonValue, WireValue } from "@fxtr-ui/core";

/** Bump when the blob shape changes incompatibly; older blobs are dropped. */
export const SESSION_BLOB_VERSION = 1;

/** A persisted op-result fill (invoke slots only). Values are shortId-escaped. */
export type SerializedFill =
	| { readonly kind: "filled"; readonly seq: number; readonly value: WireValue }
	| { readonly kind: "errored"; readonly seq: number; readonly message: string };

/** A persisted callback inbox: its uuid, event log, and connection state. */
export interface SerializedInbox {
	readonly uuid: string;
	readonly firstIndex: number;
	readonly events: ReadonlyArray<{ readonly seq: number; readonly payload: WireValue }>;
	/** Last `connectionState` (`idle` is represented by omission). */
	readonly sub?: { readonly state: "connected" | "broken"; readonly reason: string | null };
}

/** One session's full persisted state. */
export interface SerializedSession {
	readonly version: number;
	readonly metadata: JsonValue;
	readonly createdAt: number;
	readonly slotCounter: number;
	readonly seqCounter: number;
	/** Committed user state, shortId-mode escaped (`null` if none). */
	readonly userState: WireValue;
	/** Dedup forward maps (kind → shortId → slot) for cross-tick load/metadata dedup. */
	readonly dedup: {
		readonly load: Record<string, number>;
		readonly get_entity_metadata: Record<string, number>;
		readonly get_service_metadata: Record<string, number>;
	};
	/** Persisted fills, keyed by slot (string keys — JSON object). Invoke slots only. */
	readonly slotFills: Record<string, SerializedFill>;
	/** Persisted inboxes, keyed by slot. */
	readonly inboxes: Record<string, SerializedInbox>;
}

/** One entry in the `:sessions` registry blob. */
export interface SessionRegistryEntry {
	readonly sessionId: string;
	readonly metadata: JsonValue;
	readonly createdAt: number;
}
