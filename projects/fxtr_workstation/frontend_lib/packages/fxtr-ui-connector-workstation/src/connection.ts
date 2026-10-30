/**
 * Wire-facing layer for the workstation `/client` endpoint:
 * WebSocket transport, request/response correlation, subscription
 * frame routing, automatic reconnect. Knows nothing about resources,
 * sessions, or state — those live in the higher-level
 * `WorkstationConnector` which consumes this connection.
 *
 * Two primary APIs:
 *
 *   `sendRequest(body)` is RPC-style: send a request (without
 *   `request_id` — the connection allocates it), await the
 *   correlated response. Response type is determined by the request
 *   shape via the `ResponseFor` mapped type.
 *
 *   `registerSubscription(spec, handler)` is streaming-style:
 *   returns a `SubscriptionHandle` synchronously while the underlying
 *   subscribe request is still in flight. The handler receives the
 *   server's snapshot + every subsequent frame via `onFrame`, and any
 *   termination (failed-subscribe, socket reset, consumer-close, or
 *   connection-close) via `onClose(reason)`. There is no separate
 *   Promise-rejection path for subscribe failures; everything flows
 *   through `onClose`.
 *
 * Socket reconnection is automatic (linear backoff, clamped).
 * Subscription replay is *not* — consumers re-register on
 * `onClose("reset")` if they want the subscription back.
 *
 * Defensive posture: unexpected conditions (unknown request_id /
 * subscription_id on inbound frames, stale socket events, etc.) log
 * a warning rather than silently dropping. Subscription handler
 * exceptions are caught and logged so one bad handler can't break
 * the read loop or stop other handlers from receiving their close
 * callbacks.
 */

import { type Atom, type PrimitiveAtom, atom, getDefaultStore } from "jotai";
import type {
	AckResponse,
	ClientRequest,
	LoadEntityErrorResponse,
	LoadEntityResponse,
	MutationCommittedResponse,
	MutationRejectedResponse,
	ResourceChangesEvent,
	ResourceKind,
	ResourceSnapshotEvent,
	ServerFrame,
	ServiceBrokenNotification,
	SessionCreatedResponse,
	SessionDebugSnapshotEvent,
	SessionEventFrame,
	SessionSnapshotEvent,
	UnsubscribeRequest,
} from "./protocol";

// --- Errors ----------------------------------------------------------

/** Transport-level protocol failure (server sent ErrorResponse, malformed frame, etc.). */
export class ProtocolError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "ProtocolError";
	}
}

/** Raised when a pending request is cut off by an unexpected socket drop. */
export class ConnectionLostError extends Error {
	constructor(message = "connection lost") {
		super(message);
		this.name = "ConnectionLostError";
	}
}

/** Raised when a pending request is cut off by an explicit `connection.close()`. */
export class ConnectionClosedError extends Error {
	constructor(message = "connection closed") {
		super(message);
		this.name = "ConnectionClosedError";
	}
}

// --- Types -----------------------------------------------------------

/** The four observable connection states surfaced via `connectionStatus`. */
export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "closed";

/** What kind of subscription to open. Determines the wire request shape. */
export type SubscriptionSpec =
	| { readonly kind: "resource"; readonly resource: ResourceKind; readonly debug?: boolean }
	| { readonly kind: "session"; readonly session_id: string }
	| { readonly kind: "session_debug"; readonly session_id: string };

/**
 * Reason a subscription ended.
 *
 * `reset` is the only one that's transient — the consumer can call
 * `registerSubscription` again with the same spec to recreate.
 * `subscribe_failed`, `unsubscribed`, and `connector_closed` are all
 * terminal: the subscription is gone for good (though the consumer
 * may choose to register a *new* subscription with the same spec
 * later if it wants).
 */
export type SubscriptionCloseReason =
	| "subscribe_failed"
	| "reset"
	| "unsubscribed"
	| "connector_closed";

/** Server frames that are routed by `subscription_id` rather than `request_id`. */
export type SubscriptionFrame =
	| ResourceSnapshotEvent
	| ResourceChangesEvent
	| SessionSnapshotEvent
	| SessionEventFrame
	| SessionDebugSnapshotEvent
	| ServiceBrokenNotification;

/**
 * Consumer-supplied callbacks for a subscription. `onFrame` fires for
 * every frame routed to this subscription's `subscription_id`,
 * starting with the snapshot. `onClose` fires exactly once when the
 * subscription ends, with a reason and (for `subscribe_failed`) the
 * underlying error.
 *
 * Exceptions thrown from these callbacks are caught and logged by
 * the connection; they do not propagate or interrupt other handlers.
 */
export interface SubscriptionHandler {
	onFrame(frame: SubscriptionFrame): void;
	onClose(reason: SubscriptionCloseReason, error?: Error): void;
}

/** Returned from `registerSubscription`. `close()` is idempotent. */
export interface SubscriptionHandle {
	/**
	 * Stable identifier for this subscription, equal to the
	 * `request_id` allocated for the underlying subscribe request.
	 * Consumers needing to send subscription-scoped requests (e.g.
	 * `MutateSessionRequest.subscription_id`) read it from here.
	 *
	 * After `onClose("reset")` and a re-register, the new handle has
	 * a new `subscriptionId` — the old one's identifier is gone with
	 * the old socket.
	 */
	readonly subscriptionId: number;
	close(): void;
}

/**
 * Maps a client-request `op` discriminator to the typed response the
 * server will send back. Used by `sendRequest`'s return type.
 *
 * `MutationRejectedResponse` and `LoadEntityErrorResponse` are *not*
 * transport errors — they're typed per-request failures. They appear
 * in the resolved response union, not as Promise rejections.
 */
export type ResponseFor<Op extends ClientRequest["op"]> = Op extends "subscribe"
	? AckResponse
	: Op extends "unsubscribe"
		? AckResponse
		: Op extends "create_session"
			? SessionCreatedResponse
			: Op extends "subscribe_session"
				? AckResponse
				: Op extends "subscribe_session_debug"
					? AckResponse
					: Op extends "load_entity"
						? LoadEntityResponse | LoadEntityErrorResponse
						: Op extends "mutate_session"
							? MutationCommittedResponse | MutationRejectedResponse
							: never;

/** A client request without the `request_id` field — the connection allocates it. */
export type ClientRequestBody = DistributiveOmit<ClientRequest, "request_id">;

/** Distributive `Omit`: applies the omit to each arm of a union. */
type DistributiveOmit<T, K extends string> = T extends T ? Omit<T, K> : never;

type JotaiStore = ReturnType<typeof getDefaultStore>;

interface WorkstationServerConnectionOptions {
	readonly url: string;
	readonly store?: JotaiStore;
	/**
	 * WebSocket constructor to use. Defaults to the global
	 * `WebSocket`. Tests can inject a mock constructor that follows
	 * the WebSocket spec (numeric `readyState`, the four lifecycle
	 * events) to exercise edge cases without a real server.
	 */
	readonly webSocketCtor?: new (
		url: string,
	) => WebSocket;
}

// --- Internal bookkeeping --------------------------------------------

interface PendingRequest {
	resolve(value: unknown): void;
	reject(error: Error): void;
}

interface ActiveSubscription {
	readonly spec: SubscriptionSpec;
	readonly handler: SubscriptionHandler;
}

/** Reconnect schedule: capped at ~3 seconds. */
const RECONNECT_DELAYS_MS = [100, 250, 500, 1000, 2000, 3000];

function reconnectDelay(attempt: number): number {
	const i = Math.min(attempt, RECONNECT_DELAYS_MS.length - 1);
	return RECONNECT_DELAYS_MS[i] ?? 3000;
}

/**
 * No-op pending request entry. Used for outbound `unsubscribe`
 * requests so the server's ack is "expected" and lands in
 * `resolvePending` with a matching entry (no warning), even though
 * we don't actually care about the response.
 */
const NOOP_PENDING: PendingRequest = {
	resolve: () => {},
	reject: () => {},
};

// --- The connection class --------------------------------------------

/**
 * Owns the WebSocket and a small amount of dispatch state. Reads
 * happen in event handlers; writes go through `sendOrQueue`. All
 * mutation happens on the JS main thread, single-threaded — no locks
 * needed.
 */
export class WorkstationServerConnection {
	/** Read-only atom view of the connection's transport state. */
	readonly connectionStatus: Atom<ConnectionStatus>;

	/** The WebSocket URL this connection was constructed with. */
	readonly url: string;
	private readonly store: JotaiStore;
	private readonly statusAtom: PrimitiveAtom<ConnectionStatus>;
	private readonly webSocketCtor: new (
		url: string,
	) => WebSocket;

	private socket: WebSocket | null = null;
	private explicitlyClosed = false;
	private reconnectAttempt = 0;
	private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

	private nextRequestId = 1;
	private readonly pendingRequests = new Map<number, PendingRequest>();
	private readonly subscriptions = new Map<number, ActiveSubscription>();
	private readonly outboundQueue: string[] = [];

	private closePromise: Promise<void> | null = null;

	constructor(opts: WorkstationServerConnectionOptions) {
		this.url = opts.url;
		this.store = opts.store ?? getDefaultStore();
		this.webSocketCtor = opts.webSocketCtor ?? WebSocket;
		this.statusAtom = atom<ConnectionStatus>("connecting");
		this.connectionStatus = this.statusAtom;
		this.openSocket();
	}

	/**
	 * Send a request, await the correlated response. Response type
	 * is determined by `body["op"]` via `ResponseFor`. The connection
	 * allocates `request_id` and includes it in the wire request.
	 *
	 * Resolves with the typed response. Rejects with `ProtocolError`
	 * on transport-level error (server's `ErrorResponse`),
	 * `ConnectionLostError` on socket drop mid-request, or
	 * `ConnectionClosedError` if `close()` is called while the request
	 * is pending.
	 *
	 * Per-request typed failures (`MutationRejectedResponse`,
	 * `LoadEntityErrorResponse`) are resolved with the failure
	 * variant, not rejected.
	 */
	sendRequest<B extends ClientRequestBody>(body: B): Promise<ResponseFor<B["op"]>> {
		if (this.explicitlyClosed) {
			return Promise.reject(new ConnectionClosedError("connection is closed"));
		}
		const request_id = this.allocateRequestId();
		const fullRequest = { ...body, request_id } as ClientRequest;
		return new Promise<ResponseFor<B["op"]>>((resolve, reject) => {
			this.pendingRequests.set(request_id, {
				resolve: resolve as (value: unknown) => void,
				reject,
			});
			this.sendOrQueue(fullRequest);
		});
	}

	/**
	 * Register a subscription. Returns a handle synchronously; the
	 * underlying subscribe request is sent in the background. The
	 * handler receives all frames routed to this subscription via
	 * `onFrame`, and exactly one terminal `onClose` callback when
	 * the subscription ends (subscribe rejection, socket reset,
	 * consumer-initiated close, or connection close).
	 *
	 * Subscriptions are managed entirely via the connection's
	 * `subscriptions` map — they do *not* register a pending request
	 * for the subscribe ack. Instead, inbound `ack` and `error`
	 * frames consult the `subscriptions` map first (by `request_id`)
	 * before falling through to `pendingRequests`. This keeps the
	 * subscription's lifecycle (ack → frames → close) self-contained.
	 */
	registerSubscription(spec: SubscriptionSpec, handler: SubscriptionHandler): SubscriptionHandle {
		if (this.explicitlyClosed) {
			// Defer the onClose so callers can wire up cleanup before
			// it fires. queueMicrotask matches typical event-emitter
			// semantics.
			queueMicrotask(() => this.callOnClose(handler, "connector_closed"));
			return { subscriptionId: -1, close: () => {} };
		}

		const subscription_id = this.allocateRequestId();
		const wireRequest = buildSubscribeRequest(spec, subscription_id);

		this.subscriptions.set(subscription_id, { spec, handler });
		this.sendOrQueue(wireRequest);

		return {
			subscriptionId: subscription_id,
			close: () => {
				const active = this.subscriptions.get(subscription_id);
				if (!active) return; // idempotent — already closed
				this.subscriptions.delete(subscription_id);

				// Best-effort unsubscribe. Only meaningful when the
				// current socket has seen (or will see) our subscribe —
				// i.e. CONNECTING (queued sub will be sent on open) or
				// OPEN (sub already sent). After a reset / reconnect
				// the subscription_id is gone with the old socket; the
				// new server doesn't know about it.
				const socket = this.socket;
				if (
					socket &&
					(socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)
				) {
					const unsubRequestId = this.allocateRequestId();
					this.pendingRequests.set(unsubRequestId, NOOP_PENDING);
					const unsubReq: UnsubscribeRequest = {
						request_id: unsubRequestId,
						op: "unsubscribe",
						subscription_id,
					};
					this.sendOrQueue(unsubReq);
				}

				this.callOnClose(active.handler, "unsubscribed");
			},
		};
	}

	/**
	 * Close the connection. Rejects all pending requests with
	 * `ConnectionClosedError`, fires `onClose("connector_closed")` on
	 * every active subscription, and closes the underlying socket.
	 *
	 * Returns a Promise that resolves when the socket reaches its
	 * final CLOSED state (or immediately if it was never opened).
	 * Idempotent — subsequent calls return the same promise.
	 */
	close(): Promise<void> {
		if (this.closePromise) return this.closePromise;
		this.closePromise = this.doClose();
		return this.closePromise;
	}

	// --- private: socket lifecycle ----------------------------------

	private doClose(): Promise<void> {
		this.explicitlyClosed = true;

		if (this.reconnectTimer !== null) {
			clearTimeout(this.reconnectTimer);
			this.reconnectTimer = null;
		}

		// Clear the outbound queue before firing handler callbacks
		// so handlers that synchronously call sendRequest /
		// registerSubscription during onClose see the rejection
		// (explicitlyClosed=true) rather than queueing into a doomed
		// queue.
		this.outboundQueue.length = 0;

		const lostError = new ConnectionClosedError();
		const pending = Array.from(this.pendingRequests.values());
		this.pendingRequests.clear();
		for (const { reject } of pending) {
			try {
				reject(lostError);
			} catch (err) {
				console.warn("[fxtr-ui] pending-request reject callback threw:", err);
			}
		}

		const subs = Array.from(this.subscriptions.values());
		this.subscriptions.clear();
		for (const { handler } of subs) this.callOnClose(handler, "connector_closed");

		const socket = this.socket;
		this.socket = null;

		if (!socket || socket.readyState === WebSocket.CLOSED) {
			this.setStatus("closed");
			return Promise.resolve();
		}

		return new Promise<void>((resolve) => {
			socket.addEventListener(
				"close",
				() => {
					this.setStatus("closed");
					resolve();
				},
				{ once: true },
			);
			socket.close();
		});
	}

	private openSocket(): void {
		this.setStatus("connecting");
		const socket = new this.webSocketCtor(this.url);
		this.socket = socket;
		socket.addEventListener("open", () => this.handleOpen(socket));
		socket.addEventListener("message", (e) => this.handleMessage(e));
		socket.addEventListener("close", () => this.handleClose(socket));
		// No explicit "error" listener: the browser WebSocket "error"
		// event carries no useful detail (spec hides the reason for
		// security) and is always followed by "close". We let that
		// fire handleClose, which schedules the reconnect.
	}

	private handleOpen(socket: WebSocket): void {
		if (this.socket !== socket) {
			console.warn("[fxtr-ui] open event for stale socket; ignoring");
			return;
		}
		this.reconnectAttempt = 0;
		this.setStatus("open");
		this.drainOutboundQueue();
	}

	private handleClose(socket: WebSocket): void {
		if (this.explicitlyClosed) return; // close() already handled cleanup
		if (this.socket !== socket) {
			console.warn("[fxtr-ui] close event for stale socket; ignoring");
			return;
		}

		this.socket = null;

		// Clear the queue before firing handler callbacks so handlers
		// that re-register subscriptions during onClose("reset") will
		// have their new requests survive (they'd otherwise be cleared
		// out from under them by this same handler).
		this.outboundQueue.length = 0;

		// Reject pending requests; they'll never see a response now.
		const lostError = new ConnectionLostError();
		const pending = Array.from(this.pendingRequests.values());
		this.pendingRequests.clear();
		for (const { reject } of pending) {
			try {
				reject(lostError);
			} catch (err) {
				console.warn("[fxtr-ui] pending-request reject callback threw:", err);
			}
		}

		// Subscriptions get explicit onClose("reset"); the consumer is
		// responsible for re-registering if it still wants the sub.
		const subs = Array.from(this.subscriptions.values());
		this.subscriptions.clear();
		for (const { handler } of subs) this.callOnClose(handler, "reset");

		this.setStatus("reconnecting");
		this.scheduleReconnect();
	}

	private scheduleReconnect(): void {
		if (this.explicitlyClosed) return;
		const delay = reconnectDelay(this.reconnectAttempt++);
		this.reconnectTimer = setTimeout(() => {
			this.reconnectTimer = null;
			if (this.explicitlyClosed) return;
			this.openSocket();
		}, delay);
	}

	// --- private: inbound dispatch ----------------------------------

	private handleMessage(event: MessageEvent): void {
		if (typeof event.data !== "string") {
			// Workstation only ever sends JSON text frames; binary is
			// a protocol violation, not something to recover from.
			// Tear down the connection so reconnect can try fresh.
			this.fatalProtocolError("received binary frame; expected JSON text");
			return;
		}

		let frame: ServerFrame;
		try {
			frame = JSON.parse(event.data) as ServerFrame;
		} catch (err) {
			this.fatalProtocolError(`malformed JSON: ${(err as Error).message}`);
			return;
		}

		// The switch is exhaustive over the current ServerFrame
		// union. If a future server adds a new frame type, TS will
		// flag this switch as non-exhaustive and the new case has
		// to be added explicitly — we prefer that over silently
		// ignoring or warn-and-continue, since failing closed on
		// unknown frames keeps the protocol surface honest.
		switch (frame.type) {
			case "ack": {
				// `ack` may be the subscribe-side ack (subscription is
				// now live; the snapshot will follow as a regular
				// subscription frame) or the response to a non-subscribe
				// request (sendRequest pending). Consult subscriptions
				// first so subscribe acks don't trigger an unknown-id
				// warning.
				if (this.subscriptions.has(frame.request_id)) return;
				this.resolvePending(frame.request_id, frame);
				return;
			}
			case "error": {
				const sub = this.subscriptions.get(frame.request_id);
				if (sub) {
					this.subscriptions.delete(frame.request_id);
					this.callOnClose(
						sub.handler,
						"subscribe_failed",
						new ProtocolError(`server error: ${frame.message}`),
					);
					return;
				}
				this.rejectPending(frame.request_id, new ProtocolError(`server error: ${frame.message}`));
				return;
			}
			case "session_created":
			case "mutation_committed":
			case "mutation_rejected":
			case "load_entity_response":
			case "load_entity_error":
				this.resolvePending(frame.request_id, frame);
				return;
			case "resource_snapshot":
			case "resource_changes":
			case "session_snapshot":
			case "session_event":
			case "session_debug_snapshot":
			case "service_broken":
				this.routeSubscriptionFrame(frame);
				return;
		}
	}

	private resolvePending(request_id: number, frame: ServerFrame): void {
		const pending = this.pendingRequests.get(request_id);
		if (!pending) {
			console.warn(
				`[fxtr-ui] response for unknown request_id ${request_id} (frame type: ${frame.type})`,
			);
			return;
		}
		this.pendingRequests.delete(request_id);
		try {
			pending.resolve(frame);
		} catch (err) {
			console.warn("[fxtr-ui] pending-request resolve callback threw:", err);
		}
	}

	private rejectPending(request_id: number, error: Error): void {
		const pending = this.pendingRequests.get(request_id);
		if (!pending) {
			console.warn(
				`[fxtr-ui] error response for unknown request_id ${request_id}: ${error.message}`,
			);
			return;
		}
		this.pendingRequests.delete(request_id);
		try {
			pending.reject(error);
		} catch (err) {
			console.warn("[fxtr-ui] pending-request reject callback threw:", err);
		}
	}

	private routeSubscriptionFrame(frame: SubscriptionFrame): void {
		const active = this.subscriptions.get(frame.subscription_id);
		if (!active) {
			// Common cause: a subscription was closed locally
			// (handle.close()) just after the server sent its
			// snapshot but before our unsubscribe was processed.
			// Less common: a server-side bug. Either way, log.
			console.warn(
				`[fxtr-ui] frame for unknown subscription_id ${frame.subscription_id} (type: ${frame.type})`,
			);
			return;
		}
		this.callOnFrame(active.handler, frame);
	}

	// --- private: outbound -----------------------------------------

	private sendOrQueue(request: ClientRequest): void {
		const text = JSON.stringify(request);
		const socket = this.socket;
		if (socket && socket.readyState === WebSocket.OPEN) {
			socket.send(text);
			return;
		}
		this.outboundQueue.push(text);
	}

	private drainOutboundQueue(): void {
		const socket = this.socket;
		if (!socket || socket.readyState !== WebSocket.OPEN) return;
		const queue = this.outboundQueue.splice(0, this.outboundQueue.length);
		for (const text of queue) socket.send(text);
	}

	private fatalProtocolError(message: string): void {
		console.error(`[fxtr-ui] fatal protocol error: ${message}`);
		// Force the socket closed; handleClose will fan out the
		// reset to pending requests / subscriptions and schedule a
		// reconnect.
		this.socket?.close();
	}

	// --- private: handler exception isolation ----------------------

	private callOnFrame(handler: SubscriptionHandler, frame: SubscriptionFrame): void {
		try {
			handler.onFrame(frame);
		} catch (err) {
			console.error("[fxtr-ui] subscription handler.onFrame threw:", err);
		}
	}

	private callOnClose(
		handler: SubscriptionHandler,
		reason: SubscriptionCloseReason,
		error?: Error,
	): void {
		try {
			handler.onClose(reason, error);
		} catch (err) {
			console.error("[fxtr-ui] subscription handler.onClose threw:", err);
		}
	}

	// --- private: utilities ----------------------------------------

	private allocateRequestId(): number {
		return this.nextRequestId++;
	}

	private setStatus(status: ConnectionStatus): void {
		this.store.set(this.statusAtom, status);
	}
}

// --- Wire request building -------------------------------------------

/** Build the wire SubscribeRequest / SubscribeSessionRequest / etc. for a spec. */
function buildSubscribeRequest(spec: SubscriptionSpec, request_id: number): ClientRequest {
	switch (spec.kind) {
		case "resource":
			return {
				request_id,
				op: "subscribe",
				resource: spec.resource,
				debug: spec.debug ?? false,
			};
		case "session":
			return {
				request_id,
				op: "subscribe_session",
				session_id: spec.session_id,
			};
		case "session_debug":
			return {
				request_id,
				op: "subscribe_session_debug",
				session_id: spec.session_id,
			};
	}
}
