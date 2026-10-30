/**
 * Minimal WebSocket spec-compatible mock for testing
 * `WorkstationServerConnection`. Records sent messages, exposes
 * `simulate*` helpers to drive lifecycle events synchronously, and
 * pushes every constructed instance into a module-level
 * `createdSockets` array so tests can grab the "current" socket
 * (typically the most recently constructed one) without weaving a
 * factory through every test.
 *
 * Call `resetMockWebSockets()` in `beforeEach` to clear the registry.
 */

/** Constants match the WebSocket spec (and the global WebSocket class). */
const READY_STATE_CONNECTING = 0;
const READY_STATE_OPEN = 1;
// const READY_STATE_CLOSING = 2;
const READY_STATE_CLOSED = 3;

type Listener = (event: unknown) => void;

export class MockWebSocket {
	static readonly CONNECTING = READY_STATE_CONNECTING;
	static readonly OPEN = READY_STATE_OPEN;
	static readonly CLOSED = READY_STATE_CLOSED;

	readonly url: string;
	readyState: number = READY_STATE_CONNECTING;
	readonly sentMessages: string[] = [];

	private readonly listeners = new Map<string, Set<Listener>>();

	constructor(url: string) {
		this.url = url;
		createdSockets.push(this);
	}

	addEventListener(type: string, listener: Listener, opts?: { once?: boolean }): void {
		if (!this.listeners.has(type)) this.listeners.set(type, new Set());
		if (opts?.once) {
			const wrapped: Listener = (event) => {
				this.listeners.get(type)?.delete(wrapped);
				listener(event);
			};
			this.listeners.get(type)?.add(wrapped);
		} else {
			this.listeners.get(type)?.add(listener);
		}
	}

	send(data: string): void {
		if (this.readyState !== READY_STATE_OPEN) {
			throw new Error(`MockWebSocket: cannot send on socket with readyState ${this.readyState}`);
		}
		this.sentMessages.push(data);
	}

	close(): void {
		if (this.readyState === READY_STATE_CLOSED) return;
		this.readyState = READY_STATE_CLOSED;
		this.dispatch("close", {});
	}

	// --- test-only helpers ------------------------------------------

	/** Transition CONNECTING → OPEN and fire the open event. */
	simulateOpen(): void {
		this.readyState = READY_STATE_OPEN;
		this.dispatch("open", {});
	}

	/** Deliver a JSON-string text frame. */
	simulateMessage(payload: string): void {
		this.dispatch("message", { data: payload });
	}

	/** Server closed the connection. */
	simulateClose(): void {
		this.readyState = READY_STATE_CLOSED;
		this.dispatch("close", {});
	}

	/** Parsed view of every JSON message the connection has sent. */
	get sentParsed(): unknown[] {
		return this.sentMessages.map((m) => JSON.parse(m));
	}

	private dispatch(type: string, event: unknown): void {
		const set = this.listeners.get(type);
		if (!set) return;
		// Iterate a copy so listeners that detach themselves
		// (once-listeners) don't disturb the iteration.
		for (const l of [...set]) l(event);
	}
}

/** Every `MockWebSocket` constructed since the last `resetMockWebSockets()`. */
export const createdSockets: MockWebSocket[] = [];

/** Returns the most recently constructed mock socket. */
export function currentMockSocket(): MockWebSocket {
	const last = createdSockets[createdSockets.length - 1];
	if (!last) throw new Error("no MockWebSocket has been constructed yet");
	return last;
}

/** Clear the registry. Call from `beforeEach`. */
export function resetMockWebSockets(): void {
	createdSockets.length = 0;
}
