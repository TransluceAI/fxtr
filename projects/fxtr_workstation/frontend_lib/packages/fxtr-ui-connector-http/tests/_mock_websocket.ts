/**
 * Minimal WebSocket mock for `WsPool` tests — satisfies the pool's
 * `WebSocketLike` surface and exposes `simulate*` helpers to drive
 * lifecycle events synchronously. Every constructed instance is pushed
 * to `MockWebSocket.instances`; call `resetMockWebSockets()` in
 * `beforeEach`.
 */

type WsListener = (ev: { readonly data?: unknown }) => void;

const CONNECTING = 0;
const OPEN = 1;
const CLOSED = 3;

export class MockWebSocket {
	static readonly instances: MockWebSocket[] = [];

	readonly url: string;
	readyState: number = CONNECTING;
	readonly sent: string[] = [];
	private readonly listeners = new Map<string, Set<WsListener>>();

	constructor(url: string) {
		this.url = url;
		MockWebSocket.instances.push(this);
	}

	addEventListener(type: string, listener: WsListener): void {
		let set = this.listeners.get(type);
		if (!set) {
			set = new Set();
			this.listeners.set(type, set);
		}
		set.add(listener);
	}

	send(data: string): void {
		if (this.readyState !== OPEN) {
			throw new Error(`MockWebSocket: cannot send on readyState ${this.readyState}`);
		}
		this.sent.push(data);
	}

	close(): void {
		if (this.readyState === CLOSED) return;
		this.readyState = CLOSED;
		this.fire("close", {});
	}

	// --- test-only drivers ------------------------------------------

	/** CONNECTING → OPEN + fire `open`. */
	simulateOpen(): void {
		this.readyState = OPEN;
		this.fire("open", {});
	}

	/** Deliver a server frame (JSON-encoded as the `message` event data). */
	simulateMessage(frame: unknown): void {
		this.fire("message", { data: JSON.stringify(frame) });
	}

	/** Server-side drop (e.g. peer crash). */
	simulateClose(): void {
		this.readyState = CLOSED;
		this.fire("close", {});
	}

	/** Transport error. */
	simulateError(): void {
		this.fire("error", {});
	}

	/** Parsed view of every frame the pool has sent on this socket. */
	get sentParsed(): Array<Record<string, unknown>> {
		return this.sent.map((m) => JSON.parse(m) as Record<string, unknown>);
	}

	private fire(type: string, ev: { readonly data?: unknown }): void {
		for (const l of [...(this.listeners.get(type) ?? [])]) l(ev);
	}
}

/** Most-recently-constructed mock socket. */
export function currentMockSocket(): MockWebSocket {
	const last = MockWebSocket.instances.at(-1);
	if (!last) throw new Error("no MockWebSocket constructed yet");
	return last;
}

/** Clear the instance registry. Call from `beforeEach`. */
export function resetMockWebSockets(): void {
	MockWebSocket.instances.length = 0;
}
