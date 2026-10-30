/**
 * Wire protocol type unions for the workstation `/client` endpoint.
 * Mirrors `fxtr_workstation/client_protocol.py` — kept in sync by hand
 * rather than codegen.
 *
 * Discriminators: client requests use `op`; server frames use `type`;
 * mutation ops carry `kind`; session events also carry `kind`.
 *
 * The connector handles JSON parse / serialize at the WebSocket
 * boundary; everything in this file is just types.
 *
 * Fields that carry the wire-escape representation of structured
 * state — `StateUpdateEvent.state`, the various `*Event.payload`
 * fields, `InvokeOp.args`, `LoadEntityResponse.payload`,
 * `JsonPatchWireOp.value`, and the `EntityMetadataResultEvent` /
 * `ServiceMetadataResultEvent` `metadata` field — are typed as
 * `WireValue` (or `WireValue`-shaped containers) directly. The
 * server's session reducer guarantees this shape by construction,
 * so consumers can feed these straight into `unescapeWire` without
 * a cast. Other `metadata` fields (e.g. `CreateSessionRequest.metadata`
 * — session creation metadata, not an op result) are *not*
 * wire-encoded and keep the broader `Record<string, unknown>` typing.
 */

import type { JsonValue, WireValue } from "@fxtr-ui/core";

/** All resources a client can subscribe to. */
export type ResourceKind = "rpc_connections" | "services" | "entities" | "sessions";

// --- Client → server requests ----------------------------------------

export interface SubscribeRequest {
	readonly request_id: number;
	readonly op: "subscribe";
	readonly resource: ResourceKind;
	readonly debug?: boolean;
}

export interface UnsubscribeRequest {
	readonly request_id: number;
	readonly op: "unsubscribe";
	readonly subscription_id: number;
}

export interface CreateSessionRequest {
	readonly request_id: number;
	readonly op: "create_session";
	readonly metadata: JsonValue;
}

export interface SubscribeSessionRequest {
	readonly request_id: number;
	readonly op: "subscribe_session";
	readonly session_id: string;
}

export interface SubscribeSessionDebugRequest {
	readonly request_id: number;
	readonly op: "subscribe_session_debug";
	readonly session_id: string;
}

export interface LoadEntityRequest {
	readonly request_id: number;
	readonly op: "load_entity";
	readonly short_id: string;
}

export interface MutateSessionRequest {
	readonly request_id: number;
	readonly op: "mutate_session";
	readonly subscription_id: number;
	readonly ack_seq: number;
	readonly update_id: number;
	readonly patch: readonly JsonPatchWireOp[];
	readonly ops: readonly MutationOp[];
}

export type ClientRequest =
	| SubscribeRequest
	| UnsubscribeRequest
	| CreateSessionRequest
	| SubscribeSessionRequest
	| SubscribeSessionDebugRequest
	| LoadEntityRequest
	| MutateSessionRequest;

// --- JSON Patch (RFC 6902) wire shape --------------------------------
//
// Server expects this exact dict shape inside
// `MutateSessionRequest.patch`. Structurally identical to
// `JsonPatchOp` in `./state.ts` (the diff helper's output type) —
// we keep them as two separate aliases rather than cross-importing
// because `protocol.ts` and `state.ts` are intentionally
// independent (the diff helper doesn't know about workstation
// RPCs). Could consolidate later if a single canonical home
// emerges.

export type JsonPatchWireOp =
	| { readonly op: "add"; readonly path: string; readonly value: WireValue }
	| { readonly op: "remove"; readonly path: string }
	| { readonly op: "replace"; readonly path: string; readonly value: WireValue };

// --- Mutation ops ----------------------------------------------------

export interface CloseOp {
	readonly kind: "close";
}

export interface TrackEntityOp {
	readonly kind: "track_entity";
	readonly short_id: string;
}

export interface UntrackEntityOp {
	readonly kind: "untrack_entity";
	readonly short_id: string;
}

export interface TrackServiceOp {
	readonly kind: "track_service";
	readonly short_id: string;
}

export interface UntrackServiceOp {
	readonly kind: "untrack_service";
	readonly short_id: string;
}

export interface LoadEntityOp {
	readonly kind: "load_entity";
	readonly slot: number;
	readonly short_id: string;
}

export interface LoadEntitySilentOp {
	readonly kind: "load_entity_silent";
	readonly slot: number;
	readonly short_id: string;
}

export interface InvokeOp {
	readonly kind: "invoke";
	readonly slot: number;
	readonly service_short_id: string;
	readonly method_name: string;
	readonly args: readonly WireValue[];
	/**
	 * Keyword arguments. Empty dict when the caller passed no kwargs;
	 * matches the Python `InvokeOp.kwargs` default (`{}`). The
	 * workstation invoke worker calls
	 * `method(*resolved_args, **resolved_kwargs)`.
	 */
	readonly kwargs: { readonly [key: string]: WireValue };
}

/**
 * Invoke a method on one of the session's own callbacks (addressed
 * by callback slot — including a slot created by a `CreateCallbackOp`
 * earlier in the same mutation). Mirrors the Python
 * `CallbackInvokeOp`; everything except the target field matches
 * `InvokeOp`.
 */
export interface CallbackInvokeOp {
	readonly kind: "callback_invoke";
	readonly slot: number;
	readonly callback_slot: number;
	readonly method_name: string;
	readonly args: readonly WireValue[];
	readonly kwargs: { readonly [key: string]: WireValue };
}

export interface GetEntityMetadataOp {
	readonly kind: "get_entity_metadata";
	readonly slot: number;
	readonly short_id: string;
}

export interface GetServiceMetadataOp {
	readonly kind: "get_service_metadata";
	readonly slot: number;
	readonly short_id: string;
}

export interface CreateCallbackOp {
	readonly kind: "create_callback";
	readonly slot: number;
}

export interface CloseCallbackOp {
	readonly kind: "close_callback";
	readonly slot: number;
}

export interface PreserveEventsOp {
	readonly kind: "preserve_events";
	readonly seqs: readonly number[];
}

export interface UnpreserveEventsOp {
	readonly kind: "unpreserve_events";
	readonly seqs: readonly number[];
}

export type MutationOp =
	| CloseOp
	| TrackEntityOp
	| UntrackEntityOp
	| TrackServiceOp
	| UntrackServiceOp
	| LoadEntityOp
	| LoadEntitySilentOp
	| InvokeOp
	| CallbackInvokeOp
	| GetEntityMetadataOp
	| GetServiceMetadataOp
	| CreateCallbackOp
	| CloseCallbackOp
	| PreserveEventsOp
	| UnpreserveEventsOp;

// --- Server → client frames ------------------------------------------

export interface AckResponse {
	readonly request_id: number;
	readonly type: "ack";
}

export interface ErrorResponse {
	readonly request_id: number;
	readonly type: "error";
	readonly message: string;
}

export interface ResourceSnapshotEvent {
	readonly subscription_id: number;
	readonly type: "resource_snapshot";
	readonly data: readonly Readonly<Record<string, unknown>>[];
}

export interface ResourceChangesEvent {
	readonly subscription_id: number;
	readonly type: "resource_changes";
	readonly added: readonly Readonly<Record<string, unknown>>[];
	readonly updated: readonly Readonly<Record<string, unknown>>[];
	readonly removed: readonly string[];
}

export interface SessionCreatedResponse {
	readonly request_id: number;
	readonly type: "session_created";
	readonly session_id: string;
}

export type MutationRejectReason =
	| "stale_ack"
	| "conflict"
	| "validation_failed"
	| "callback_setup_failed"
	| "session_busy"
	| "session_gone";

export interface MutationCommittedResponse {
	readonly request_id: number;
	readonly type: "mutation_committed";
	readonly subscription_id: number;
	readonly seq: number;
	readonly update_id: number;
}

export interface MutationRejectedResponse {
	readonly request_id: number;
	readonly type: "mutation_rejected";
	readonly reason: MutationRejectReason;
	readonly message: string;
}

// Session events: tagged on `kind`, carried inside SessionEventFrame
// (live push) and SessionSnapshotEvent (snapshot replay).

export interface StateUpdateEvent {
	readonly kind: "state_update";
	readonly seq: number;
	readonly ack_seq: number;
	readonly state: { readonly [key: string]: WireValue };
	readonly closed: boolean;
	readonly tracked_entities: readonly string[];
	readonly tracked_services: readonly string[];
	readonly slot_counter: number;
}

export interface LoadResultEvent {
	readonly kind: "load_result";
	readonly seq: number;
	readonly slot: number;
	readonly payload: WireValue;
}

export interface LoadResultSilentEvent {
	readonly kind: "load_result_silent";
	readonly seq: number;
	readonly slot: number;
}

export interface LoadErrorEvent {
	readonly kind: "load_error";
	readonly seq: number;
	readonly slot: number;
	readonly message: string;
}

export interface InvokeResultEvent {
	readonly kind: "invoke_result";
	readonly seq: number;
	readonly slot: number;
	readonly payload: WireValue;
}

export interface InvokeErrorEvent {
	readonly kind: "invoke_error";
	readonly seq: number;
	readonly slot: number;
	readonly message: string;
}

export interface EntityMetadataResultEvent {
	readonly kind: "entity_metadata_result";
	readonly seq: number;
	readonly slot: number;
	/**
	 * Wire-escape-encoded metadata dict. Entity-ref fields appear in
	 * tagged form (`["entity", short_id]`), `*_id_string` fields are
	 * plain strings. Decode through `unescapeWire` to produce an
	 * `EntityMetadata` value with `EntityRefStub` instances at the
	 * ref leaves.
	 */
	readonly metadata: { readonly [key: string]: WireValue };
}

export interface EntityMetadataErrorEvent {
	readonly kind: "entity_metadata_error";
	readonly seq: number;
	readonly slot: number;
	readonly message: string;
}

export interface ServiceMetadataResultEvent {
	readonly kind: "service_metadata_result";
	readonly seq: number;
	readonly slot: number;
	/**
	 * Wire-escape-encoded metadata dict. Today carries
	 * `type_id_string` (plain string); future ref-valued fields would
	 * appear in tagged form. Decode through `unescapeWire` to produce
	 * a `ServiceMetadata` value.
	 */
	readonly metadata: { readonly [key: string]: WireValue };
}

export interface ServiceMetadataErrorEvent {
	readonly kind: "service_metadata_error";
	readonly seq: number;
	readonly slot: number;
	readonly message: string;
}

export interface CallbackEvent {
	readonly kind: "callback";
	readonly seq: number;
	readonly slot: number;
	readonly payload: WireValue;
}

/**
 * Connection-state transition for a callback inbox's `subscribe_to`
 * subscription. `connected=true` when one is established, `false` (with
 * the keep-alive lease's break `reason`) when the source breaks
 * ungracefully. A recoverable state transition the session reducer
 * folds into a per-slot `idle`/`connected`/`broken` connection state
 * (latest-wins) — see `WorkstationCallbackInboxStub.connectionState`.
 */
export interface CallbackSubscriptionEvent {
	readonly kind: "callback_subscription";
	readonly seq: number;
	readonly slot: number;
	readonly connected: boolean;
	readonly reason?: string | null;
}

export type NonStateSessionEvent =
	| LoadResultEvent
	| LoadResultSilentEvent
	| LoadErrorEvent
	| InvokeResultEvent
	| InvokeErrorEvent
	| EntityMetadataResultEvent
	| EntityMetadataErrorEvent
	| ServiceMetadataResultEvent
	| ServiceMetadataErrorEvent
	| CallbackEvent
	| CallbackSubscriptionEvent;

export type SessionEvent = StateUpdateEvent | NonStateSessionEvent;

export interface SessionSnapshotEvent {
	readonly subscription_id: number;
	readonly type: "session_snapshot";
	readonly session_id: string;
	readonly last_state: StateUpdateEvent;
	/** Events held past the retention horizon. Each seq ≤ last_state.ack_seq. */
	readonly preserved_events: readonly NonStateSessionEvent[];
	/** Unacked-suffix events. Each seq > last_state.ack_seq. */
	readonly subsequent_events: readonly NonStateSessionEvent[];
}

export interface SessionEventFrame {
	readonly subscription_id: number;
	readonly type: "session_event";
	readonly event: SessionEvent;
}

export interface OpenInvokeInfo {
	readonly service_short_id: string;
	readonly method_name: string;
}

export interface SessionDebugSnapshotEvent {
	readonly subscription_id: number;
	readonly type: "session_debug_snapshot";
	readonly session_id: string;
	readonly entity_refcounts: Readonly<Record<string, number>>;
	readonly service_refcounts: Readonly<Record<string, number>>;
	readonly slot_counter: number;
	readonly open_callbacks: Readonly<Record<string, string>>;
	readonly open_invokes: Readonly<Record<string, OpenInvokeInfo>>;
	readonly open_loads: Readonly<Record<string, string>>;
	readonly preserved_events: readonly number[];
}

export interface LoadEntityResponse {
	readonly request_id: number;
	readonly type: "load_entity_response";
	readonly payload: WireValue;
}

export interface LoadEntityErrorResponse {
	readonly request_id: number;
	readonly type: "load_entity_error";
	readonly message: string;
}

/**
 * Out-of-band notice that a service a session holds has broken. Routed
 * by `subscription_id` like the other session-subscription frames, but
 * NOT a session event — it has no seq, is never on the event log, and
 * is re-derived from current state on every (re)subscribe. The
 * connector coalesces these into a connector-level broken-services set
 * that all interned `WorkstationServiceRefStub`s read via `state`.
 */
export interface ServiceBrokenNotification {
	readonly subscription_id: number;
	readonly type: "service_broken";
	readonly service_short_id: string;
}

export type ServerFrame =
	| AckResponse
	| ErrorResponse
	| ResourceSnapshotEvent
	| ResourceChangesEvent
	| SessionCreatedResponse
	| SessionSnapshotEvent
	| MutationCommittedResponse
	| MutationRejectedResponse
	| SessionEventFrame
	| SessionDebugSnapshotEvent
	| LoadEntityResponse
	| LoadEntityErrorResponse
	| ServiceBrokenNotification;

// --- Resource record shapes ------------------------------------------
//
// Records inside ResourceSnapshotEvent.data / ResourceChangesEvent.{added,updated}.
// Debug-only fields are typed optional — they only appear when the
// subscription was opened with debug=true.

export interface RpcConnectionRecord {
	readonly connection_id: string;
	readonly name: string | null;
	readonly peer_addr: string | null;
	readonly connected_at: string;
	/** Debug-only. */
	readonly service_registration_short_ids?: readonly string[];
	/** Debug-only. */
	readonly entity_registration_short_ids?: readonly string[];
}

export interface ServiceRecord {
	readonly short_id: string;
	readonly service_type_id: string;
	readonly external_names: readonly string[];
	/**
	 * Visible third state: `true` once the grant backing the
	 * workstation's bridge to this service was revoked / its RPC
	 * connection dropped. List/debug tables render this (e.g.
	 * strikethrough). Always present on server records; optional here
	 * for back-compat with older snapshots.
	 */
	readonly broken?: boolean;
	/** Debug-only. */
	readonly registering_connection_ids?: readonly string[];
	/** Debug-only. */
	readonly system_source_kinds?: readonly string[];
	/** Debug-only. */
	readonly pinned_by_sessions?: readonly string[];
}

export interface EntityRecord {
	readonly short_id: string;
	readonly fxtr_id: string;
	readonly external_names: readonly string[];
	/** Debug-only. */
	readonly registering_connection_ids?: readonly string[];
	/** Debug-only. */
	readonly pinned_by_sessions?: readonly string[];
}

export interface SessionRecord {
	readonly session_id: string;
	readonly metadata: Readonly<Record<string, unknown>>;
	readonly created_at: string;
}

/** Primary-key field per resource, used by snapshot-and-delta merging. */
export const RESOURCE_KEY_FIELD: Readonly<Record<ResourceKind, string>> = {
	rpc_connections: "connection_id",
	services: "short_id",
	entities: "short_id",
	sessions: "session_id",
};
