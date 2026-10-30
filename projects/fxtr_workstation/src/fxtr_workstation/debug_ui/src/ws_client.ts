// Minimal /client wire client. Mirrors the protocol defined in
// fxtr_workstation/client_protocol.py — kept hand-written to avoid
// pulling in a code-generation step. The debug UI always subscribes
// with `debug=true`, so debug-only fields are typed as required here.
//
// Wire shape recap (see client_protocol.py):
//   - subscribe → resource_snapshot (initial) + resource_changes (deltas)
//   - subscribe_session → session_snapshot (initial) + session_event (push)
//   - subscribe_session_debug → session_debug_snapshot (full snapshot
//     re-pushed on every refcount / slot_counter change)

export type ResourceKind =
  | "rpc_connections"
  | "services"
  | "entities"
  | "sessions";

// --- Client → server requests --------------------------------------

export type SubscribeRequest = {
  request_id: number;
  op: "subscribe";
  resource: ResourceKind;
  debug: boolean;
};

export type UnsubscribeRequest = {
  request_id: number;
  op: "unsubscribe";
  subscription_id: number;
};

export type SubscribeSessionRequest = {
  request_id: number;
  op: "subscribe_session";
  session_id: string;
};

export type SubscribeSessionDebugRequest = {
  request_id: number;
  op: "subscribe_session_debug";
  session_id: string;
};

export type ClientRequest =
  | SubscribeRequest
  | UnsubscribeRequest
  | SubscribeSessionRequest
  | SubscribeSessionDebugRequest;

// --- Server → client frames ----------------------------------------

export type AckResponse = { request_id: number; type: "ack" };
export type ErrorResponse = {
  request_id: number;
  type: "error";
  message: string;
};

export type ResourceSnapshotEvent = {
  subscription_id: number;
  type: "resource_snapshot";
  data: Record<string, unknown>[];
};

export type ResourceChangesEvent = {
  subscription_id: number;
  type: "resource_changes";
  added: Record<string, unknown>[];
  updated: Record<string, unknown>[];
  removed: string[];
};

// Session-event tagged union (matches client_protocol.py::SessionEvent)
export type StateUpdateEvent = {
  kind: "state_update";
  seq: number;
  ack_seq: number;
  state: Record<string, unknown>;
  closed: boolean;
  tracked_entities: string[];
  tracked_services: string[];
  slot_counter: number;
};

export type LoadResultEvent = {
  kind: "load_result";
  seq: number;
  slot: number;
  payload: unknown;
};

export type LoadResultSilentEvent = {
  kind: "load_result_silent";
  seq: number;
  slot: number;
};

export type LoadErrorEvent = {
  kind: "load_error";
  seq: number;
  slot: number;
  message: string;
};

export type InvokeResultEvent = {
  kind: "invoke_result";
  seq: number;
  slot: number;
  payload: unknown;
};

export type InvokeErrorEvent = {
  kind: "invoke_error";
  seq: number;
  slot: number;
  message: string;
};

export type CallbackEvent = {
  kind: "callback";
  seq: number;
  slot: number;
  payload: unknown;
};

export type EntityMetadataResultEvent = {
  kind: "entity_metadata_result";
  seq: number;
  slot: number;
  metadata: Record<string, unknown>;
};

export type EntityMetadataErrorEvent = {
  kind: "entity_metadata_error";
  seq: number;
  slot: number;
  message: string;
};

export type ServiceMetadataResultEvent = {
  kind: "service_metadata_result";
  seq: number;
  slot: number;
  metadata: Record<string, unknown>;
};

export type ServiceMetadataErrorEvent = {
  kind: "service_metadata_error";
  seq: number;
  slot: number;
  message: string;
};

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
  | CallbackEvent;

export type SessionEvent = StateUpdateEvent | NonStateSessionEvent;

export type SessionSnapshotEvent = {
  subscription_id: number;
  type: "session_snapshot";
  session_id: string;
  last_state: StateUpdateEvent;
  // Events held past the retention horizon by an explicit
  // ``preserve_events`` mutation op. Each has
  // ``seq <= last_state.ack_seq``.
  preserved_events: NonStateSessionEvent[];
  // Unacked-suffix events. Each has ``seq > last_state.ack_seq``.
  subsequent_events: NonStateSessionEvent[];
};

export type SessionEventFrame = {
  subscription_id: number;
  type: "session_event";
  event: SessionEvent;
};

export type OpenInvokeInfo = {
  service_short_id: string;
  method_name: string;
};

export type SessionDebugSnapshotEvent = {
  subscription_id: number;
  type: "session_debug_snapshot";
  session_id: string;
  entity_refcounts: Record<string, number>;
  service_refcounts: Record<string, number>;
  slot_counter: number;
  open_callbacks: Record<string, string>;
  open_invokes: Record<string, OpenInvokeInfo>;
  open_loads: Record<string, string>;
  // 8a: seqs the session has explicitly preserved past the ack
  // horizon. The events themselves are delivered in the next
  // ``session_snapshot.preserved_events`` bucket; this field
  // surfaces just the seqs so the event log can render preserved
  // rows distinctly.
  preserved_events: number[];
};

export type ServerFrame =
  | AckResponse
  | ErrorResponse
  | ResourceSnapshotEvent
  | ResourceChangesEvent
  | SessionSnapshotEvent
  | SessionEventFrame
  | SessionDebugSnapshotEvent;

// --- Resource record shapes ----------------------------------------

export type RpcConnectionRecord = {
  connection_id: string;
  name: string | null;
  peer_addr: string | null;
  connected_at: string;
  service_registration_short_ids: string[];
  entity_registration_short_ids: string[];
};

export type ServiceRecord = {
  short_id: string;
  service_type_id: string;
  external_names: string[];
  // Visible third state: the bridge's grant was revoked / its RPC
  // connection dropped. Optional for back-compat with older servers.
  broken?: boolean;
  registering_connection_ids: string[];
  system_source_kinds: string[];
  pinned_by_sessions: string[];
};

export type EntityRecord = {
  short_id: string;
  fxtr_id: string;
  external_names: string[];
  registering_connection_ids: string[];
  pinned_by_sessions: string[];
};

export type SessionRecord = {
  session_id: string;
  metadata: Record<string, unknown>;
  created_at: string;
};

// Each resource's primary-key field, used to merge deltas against the
// local snapshot copy.
const RESOURCE_KEY_FIELD: Record<ResourceKind, string> = {
  rpc_connections: "connection_id",
  services: "short_id",
  entities: "short_id",
  sessions: "session_id",
};

// --- Subscription handlers -----------------------------------------

export type SnapshotHandler = (data: Record<string, unknown>[]) => void;

export type SessionStreamSnapshot = {
  last_state: StateUpdateEvent;
  // Single ordered event log: preserved events + subsequent events
  // from the snapshot, plus any live events received after that. A
  // state_update arriving live replaces ``last_state`` and we drop
  // any events with ``seq <= last_state.ack_seq`` that aren't
  // explicitly preserved (matching the workstation's retention
  // model). The renderer derives the preserved set from the debug
  // snapshot's ``preserved_events`` list.
  events: NonStateSessionEvent[];
};

export type SessionStreamHandler = (snapshot: SessionStreamSnapshot) => void;
export type SessionDebugHandler = (
  snapshot: SessionDebugSnapshotEvent,
) => void;

type ResourceSubState = {
  kind: "resource";
  resource: ResourceKind;
  records: Map<string, Record<string, unknown>>;
  handler: SnapshotHandler;
};

type SessionSubState = {
  kind: "session";
  session_id: string;
  snapshot: SessionStreamSnapshot | null;
  handler: SessionStreamHandler;
};

type SessionDebugSubState = {
  kind: "session_debug";
  session_id: string;
  handler: SessionDebugHandler;
};

type SubState = ResourceSubState | SessionSubState | SessionDebugSubState;

// --- The client ----------------------------------------------------

export class WorkstationDebugClient {
  private ws: WebSocket;
  private nextId = 1;
  private subs = new Map<number, SubState>();

  constructor(url: string) {
    this.ws = new WebSocket(url);
    this.ws.addEventListener("message", (e) => {
      const frame = JSON.parse(e.data) as ServerFrame;
      this.handleFrame(frame);
    });
  }

  async ready(): Promise<void> {
    if (this.ws.readyState === WebSocket.OPEN) return;
    await new Promise<void>((resolve, reject) => {
      this.ws.addEventListener("open", () => resolve(), { once: true });
      this.ws.addEventListener("error", (e) => reject(e), { once: true });
    });
  }

  /** Subscribe to a resource. ``onSnapshot`` fires once with the
   *  initial snapshot, then again with the merged snapshot after each
   *  ``resource_changes`` delta. */
  subscribe(
    resource: ResourceKind,
    onSnapshot: SnapshotHandler,
    options?: { debug?: boolean },
  ): () => void {
    const id = this.nextId++;
    this.subs.set(id, {
      kind: "resource",
      resource,
      records: new Map(),
      handler: onSnapshot,
    });
    this.send({
      request_id: id,
      op: "subscribe",
      resource,
      debug: options?.debug ?? false,
    });
    return () => this.unsubscribe(id);
  }

  /** Subscribe to a session's main event stream. ``onSnapshot`` fires
   *  once with the initial snapshot; subsequent ``session_event``
   *  pushes append to ``events`` (and update ``last_state`` for
   *  ``state_update`` events) and re-fire ``onSnapshot``. */
  subscribeSession(
    sessionId: string,
    onSnapshot: SessionStreamHandler,
  ): () => void {
    const id = this.nextId++;
    this.subs.set(id, {
      kind: "session",
      session_id: sessionId,
      snapshot: null,
      handler: onSnapshot,
    });
    this.send({
      request_id: id,
      op: "subscribe_session",
      session_id: sessionId,
    });
    return () => this.unsubscribe(id);
  }

  /** Subscribe to a session's debug stream (refcounts +
   *  slot_counter). Each ``session_debug_snapshot`` frame is
   *  delivered verbatim. */
  subscribeSessionDebug(
    sessionId: string,
    onSnapshot: SessionDebugHandler,
  ): () => void {
    const id = this.nextId++;
    this.subs.set(id, {
      kind: "session_debug",
      session_id: sessionId,
      handler: onSnapshot,
    });
    this.send({
      request_id: id,
      op: "subscribe_session_debug",
      session_id: sessionId,
    });
    return () => this.unsubscribe(id);
  }

  close(): void {
    this.ws.close();
  }

  private unsubscribe(id: number): void {
    if (!this.subs.has(id)) return;
    this.subs.delete(id);
    this.send({
      request_id: this.nextId++,
      op: "unsubscribe",
      subscription_id: id,
    });
  }

  private send(req: ClientRequest): void {
    this.ws.send(JSON.stringify(req));
  }

  private handleFrame(frame: ServerFrame): void {
    switch (frame.type) {
      case "resource_snapshot": {
        const s = this.subs.get(frame.subscription_id);
        if (!s || s.kind !== "resource") return;
        s.records.clear();
        const keyField = RESOURCE_KEY_FIELD[s.resource];
        for (const r of frame.data) {
          s.records.set(String(r[keyField]), r);
        }
        s.handler([...s.records.values()]);
        return;
      }
      case "resource_changes": {
        const s = this.subs.get(frame.subscription_id);
        if (!s || s.kind !== "resource") return;
        const keyField = RESOURCE_KEY_FIELD[s.resource];
        for (const r of frame.added) s.records.set(String(r[keyField]), r);
        for (const r of frame.updated) s.records.set(String(r[keyField]), r);
        for (const k of frame.removed) s.records.delete(k);
        s.handler([...s.records.values()]);
        return;
      }
      case "session_snapshot": {
        const s = this.subs.get(frame.subscription_id);
        if (!s || s.kind !== "session") return;
        // Flatten preserved + subsequent into the renderer's single
        // ordered event log. Both buckets are non-state by wire
        // contract, sorted by seq within each bucket; preserved
        // seqs are all <= last_state.ack_seq and subsequent are
        // all > last_state.ack_seq, so concatenating preserves
        // overall seq order.
        s.snapshot = {
          last_state: frame.last_state,
          events: [...frame.preserved_events, ...frame.subsequent_events],
        };
        s.handler(s.snapshot);
        return;
      }
      case "session_event": {
        const s = this.subs.get(frame.subscription_id);
        if (!s || s.kind !== "session" || !s.snapshot) return;
        // state_update events update last_state authoritatively;
        // they aren't appended to ``events`` (the local stream
        // log holds non-state events only). Non-state events
        // append to ``events``.
        const next: SessionStreamSnapshot =
          frame.event.kind === "state_update"
            ? {
                last_state: frame.event,
                events: s.snapshot.events,
              }
            : {
                last_state: s.snapshot.last_state,
                events: [...s.snapshot.events, frame.event],
              };
        s.snapshot = next;
        s.handler(next);
        return;
      }
      case "session_debug_snapshot": {
        const s = this.subs.get(frame.subscription_id);
        if (!s || s.kind !== "session_debug") return;
        s.handler(frame);
        return;
      }
      case "ack":
      case "error":
        // The debug UI never sends mutations, so acks/errors are
        // unexpected. Log them in case one slips through.
        // eslint-disable-next-line no-console
        console.log("[debug-ui] unexpected response", frame);
        return;
    }
  }
}
