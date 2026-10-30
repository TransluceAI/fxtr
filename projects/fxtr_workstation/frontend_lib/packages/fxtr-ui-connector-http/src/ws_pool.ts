/**
 * The ws connection pool: one `WsConnection` per distinct ws `url`,
 * multiplexing every ws service on that url.
 *
 * **Connect-on-demand, no background reconnect** (design/04 Q-A): a
 * connection opens (or reopens) when an invoke needs it; invokes that
 * arrive while a connect attempt is in flight wait for it; if the
 * attempt fails they reject (→ the session fills an `invoke_error`).
 * On a drop:
 *   - every in-flight invoke on that connection rejects;
 *   - the connection's **generation** bumps (the next invoke opens a
 *     fresh socket on a new generation);
 *   - `onDrop(url, droppedGeneration)` fires, so the session can break
 *     transient stubs minted on that generation and flip the
 *     `connectionState` of inboxes that held a live subscription over it.
 * Nothing reconnects until the next use.
 *
 * Wire (one JSON object per text frame, design/01):
 *   client → server:  {type:"invoke", id, service, method, args, kwargs}
 *   server → client:  {type:"result", id, value}
 *                     {type:"error",  id, message}
 *                     {type:"notify", inbox, payload}
 *
 * The pool is transport-only: it speaks resolved-mode `WireValue`s but
 * does not interpret them. Notify frames are routed to `onNotify(inbox,
 * payload)` (the connector maps the inbox uuid → owning session);
 * results/errors resolve/reject the matching invoke promise.
 *
 * Transient services are **generation-bound**: a transient stub records
 * the (url, generation) it arrived on. `invoke` accepts an optional
 * `expectedGeneration`; if the live generation no longer matches it
 * rejects immediately ("transient service broken") rather than sending
 * onto a newer connection — the send-time half of the at-allocate-and-
 * at-send check (design/01 Q-L).
 */

import type { WireValue } from "@fxtr-ui/core";

/** The slice of the WebSocket API the pool uses (injectable for tests). */
export interface WebSocketLike {
	readonly readyState: number;
	send(data: string): void;
	close(): void;
	addEventListener(type: string, listener: (ev: { readonly data?: unknown }) => void): void;
}

/** Constructs a `WebSocketLike` for a url (defaults to the global `WebSocket`). */
export type WebSocketCtor = new (url: string) => WebSocketLike;

/** A ws invocation's payload (the pool adds the frame `type` + `id`). */
export interface WsInvokeRequest {
	readonly service: string;
	readonly method: string;
	readonly args: readonly WireValue[];
	readonly kwargs: { readonly [key: string]: WireValue };
}

/** Routes a `notify` frame to the session owning `inbox`. */
export type NotifyHandler = (inbox: string, payload: WireValue) => void;

/** Fired when a connection drops: `generation` is the epoch that died. */
export type DropHandler = (url: string, generation: number) => void;

export interface WsPoolOptions {
	/** Defaults to the global `WebSocket`. */
	readonly webSocketCtor?: WebSocketCtor;
	/** Route inbound `notify` frames (by the client-minted inbox uuid). */
	readonly onNotify: NotifyHandler;
	/** Notified when a connection's generation breaks (drop / close). */
	readonly onDrop?: DropHandler;
}

/** Raised (as a rejection) when an invoke can't complete. */
export class WsInvokeError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "WsInvokeError";
	}
}

interface Pending {
	readonly resolve: (value: WireValue) => void;
	readonly reject: (error: Error) => void;
}

interface OpenWaiter {
	readonly resolve: () => void;
	readonly reject: (error: Error) => void;
}

/** Per-url connection state. */
class WsConnection {
	socket: WebSocketLike | null = null;
	/** Current epoch; bumped each time a socket is created. 0 = never opened. */
	generation = 0;
	status: "idle" | "connecting" | "open" | "dropped" = "idle";
	readonly pending = new Map<string, Pending>();
	readonly openWaiters: OpenWaiter[] = [];

	constructor(readonly url: string) {}
}

export class WsPool {
	private readonly ctor: WebSocketCtor;
	private readonly onNotify: NotifyHandler;
	private readonly onDrop: DropHandler | undefined;
	private readonly conns = new Map<string, WsConnection>();
	private nextId = 1;
	private closed = false;

	constructor(opts: WsPoolOptions) {
		// Fall back to the global WebSocket (browser / Node ≥ 22).
		const ctor = opts.webSocketCtor ?? (globalThis as { WebSocket?: WebSocketCtor }).WebSocket;
		if (!ctor) {
			throw new Error("WsPool: no `webSocketCtor` supplied and no global WebSocket available");
		}
		this.ctor = ctor;
		this.onNotify = opts.onNotify;
		this.onDrop = opts.onDrop;
	}

	/** The live generation for `url`, or 0 if no socket is currently open. */
	liveGeneration(url: string): number {
		const conn = this.conns.get(url);
		return conn && conn.status === "open" ? conn.generation : 0;
	}

	/** Whether `generation` is the currently-open epoch for `url`. */
	isGenerationLive(url: string, generation: number): boolean {
		const conn = this.conns.get(url);
		return conn !== undefined && conn.status === "open" && conn.generation === generation;
	}

	/**
	 * Invoke a ws service. Resolves with the result value (resolved-mode
	 * `WireValue`); rejects with `WsInvokeError` on a server `error`
	 * frame, a failed/lost connection, or a stale `expectedGeneration`.
	 */
	async invoke(
		url: string,
		req: WsInvokeRequest,
		opts?: { readonly expectedGeneration?: number },
	): Promise<WireValue> {
		if (this.closed) throw new WsInvokeError("ws pool is closed");
		const conn = this.ensureConn(url);
		// Send-time generation check for transient services (design/01 Q-L):
		// reject a stale transient *before* opening a fresh socket — a dead
		// generation never routes onto a newer connection.
		if (
			opts?.expectedGeneration !== undefined &&
			!this.isGenerationLive(url, opts.expectedGeneration)
		) {
			throw new WsInvokeError(
				`transient service broken: generation ${opts.expectedGeneration} is no longer live on ${url}`,
			);
		}
		await this.ensureOpen(conn);
		// The connection can drop during the await microtask; re-check.
		if (opts?.expectedGeneration !== undefined && conn.generation !== opts.expectedGeneration) {
			throw new WsInvokeError(
				`transient service broken: connection ${url} is on generation ${conn.generation}, not ${opts.expectedGeneration}`,
			);
		}
		const id = String(this.nextId++);
		const frame = JSON.stringify({
			type: "invoke",
			id,
			service: req.service,
			method: req.method,
			args: req.args,
			kwargs: req.kwargs,
		});
		return new Promise<WireValue>((resolve, reject) => {
			conn.pending.set(id, { resolve, reject });
			try {
				// `ensureOpen` resolved, but the socket can die between ticks.
				if (conn.status !== "open" || conn.socket === null) {
					throw new WsInvokeError(`ws connection to ${url} is not open`);
				}
				conn.socket.send(frame);
			} catch (e) {
				conn.pending.delete(id);
				reject(e instanceof Error ? e : new WsInvokeError(String(e)));
			}
		});
	}

	/** Close every connection + reject all in-flight invokes. Idempotent. */
	close(): void {
		this.closed = true;
		for (const conn of this.conns.values()) {
			// Detach the socket *before* closing it: the mock (and a real
			// browser socket) fires a "close" event, and the listener below
			// early-returns once `conn.socket` no longer points at it — so we
			// don't re-enter `dropConnection`/`onDrop` during teardown.
			const socket = conn.socket;
			conn.socket = null;
			conn.status = "dropped";
			this.failConnection(conn, new WsInvokeError("ws pool is closed"), /* fireDrop */ false);
			// Actually close the underlying socket. Without this the server
			// side keeps the connection open (its read loop stays parked),
			// which blocks any graceful server shutdown.
			if (socket) {
				try {
					socket.close();
				} catch {
					// best-effort — a socket mid-handshake may throw.
				}
			}
		}
	}

	private ensureConn(url: string): WsConnection {
		let conn = this.conns.get(url);
		if (!conn) {
			conn = new WsConnection(url);
			this.conns.set(url, conn);
		}
		return conn;
	}

	/** Resolve once `conn` is open; opening it on demand. Rejects if the attempt fails. */
	private ensureOpen(conn: WsConnection): Promise<void> {
		if (conn.status === "open") return Promise.resolve();
		const waiter = new Promise<void>((resolve, reject) => {
			conn.openWaiters.push({ resolve, reject });
		});
		if (conn.status === "idle" || conn.status === "dropped") this.openSocket(conn);
		return waiter;
	}

	private openSocket(conn: WsConnection): void {
		conn.generation += 1;
		conn.status = "connecting";
		const generation = conn.generation;
		let socket: WebSocketLike;
		try {
			socket = new this.ctor(conn.url);
		} catch (e) {
			conn.status = "dropped";
			this.flushOpenWaiters(conn, e instanceof Error ? e : new WsInvokeError(String(e)));
			return;
		}
		conn.socket = socket;
		socket.addEventListener("open", () => {
			// Ignore a stale socket's events (a newer epoch superseded it).
			if (conn.socket !== socket) return;
			conn.status = "open";
			for (const w of conn.openWaiters.splice(0)) w.resolve();
		});
		socket.addEventListener("message", (ev) => {
			if (conn.socket !== socket) return;
			this.handleFrame(conn, ev.data);
		});
		socket.addEventListener("close", () => {
			if (conn.socket !== socket) return;
			this.dropConnection(conn, generation, `ws connection to ${conn.url} closed`);
		});
		socket.addEventListener("error", () => {
			if (conn.socket !== socket) return;
			this.dropConnection(conn, generation, `ws connection to ${conn.url} errored`);
		});
	}

	private handleFrame(conn: WsConnection, data: unknown): void {
		let frame: {
			type?: unknown;
			id?: unknown;
			value?: WireValue;
			message?: unknown;
			inbox?: unknown;
			payload?: WireValue;
		};
		try {
			frame = JSON.parse(typeof data === "string" ? data : String(data));
		} catch {
			return; // ignore unparseable frames
		}
		if (frame.type === "notify") {
			if (typeof frame.inbox === "string") this.onNotify(frame.inbox, frame.payload ?? null);
			return;
		}
		if (frame.type === "result" && typeof frame.id === "string") {
			const p = conn.pending.get(frame.id);
			if (p) {
				conn.pending.delete(frame.id);
				p.resolve(frame.value ?? null);
			}
			return;
		}
		if (frame.type === "error" && typeof frame.id === "string") {
			const p = conn.pending.get(frame.id);
			if (p) {
				conn.pending.delete(frame.id);
				p.reject(
					new WsInvokeError(typeof frame.message === "string" ? frame.message : "ws invoke error"),
				);
			}
			return;
		}
		// Unknown / id-less frames are ignored.
	}

	/** A live connection dropped: reject pending, mark dropped, fire onDrop. */
	private dropConnection(conn: WsConnection, generation: number, reason: string): void {
		conn.socket = null;
		conn.status = "dropped";
		this.failConnection(conn, new WsInvokeError(reason), /* fireDrop */ true, generation);
	}

	private failConnection(
		conn: WsConnection,
		error: Error,
		fireDrop: boolean,
		generation?: number,
	): void {
		for (const p of conn.pending.values()) p.reject(error);
		conn.pending.clear();
		this.flushOpenWaiters(conn, error);
		if (fireDrop && this.onDrop) this.onDrop(conn.url, generation ?? conn.generation);
	}

	private flushOpenWaiters(conn: WsConnection, error: Error): void {
		for (const w of conn.openWaiters.splice(0)) w.reject(error);
	}
}
