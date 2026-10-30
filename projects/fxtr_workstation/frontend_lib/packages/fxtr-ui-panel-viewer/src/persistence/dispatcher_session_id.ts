/**
 * Persistence of the dispatcher session id.
 *
 * Lives in its own key in the consumer's `Storage`, separate from
 * the layout payload (which carries `dockview` + `sidebar` state
 * only). The dispatcher id is written by the consumer's
 * session-acquire effect as soon as `createSession` resolves, so a
 * subsequent mount sees the id without depending on any reactive
 * bookkeeping. Pass the same `Storage` the viewer's `storage` prop
 * uses (e.g. `localStorage`) so the id and the layout age together.
 */

export const DISPATCHER_SESSION_ID_KEY = "fxtr-workstation-dispatcher-session-id";

/**
 * Read the persisted dispatcher session id from the given
 * `storage`. Returns `null` when no key is present or the stored
 * value is empty.
 */
export function readDispatcherSessionId(storage: Storage): string | null {
	const raw = storage.getItem(DISPATCHER_SESSION_ID_KEY);
	if (raw === null || raw.length === 0) return null;
	return raw;
}

/**
 * Write the dispatcher session id to the given `storage`. Called by
 * the consumer the instant `createSession` resolves — the next
 * mount or page reload sees it without any further round-trip.
 */
export function writeDispatcherSessionId(storage: Storage, id: string): void {
	try {
		storage.setItem(DISPATCHER_SESSION_ID_KEY, id);
	} catch (err) {
		console.warn("[fxtr-ui] failed to persist dispatcher session id:", err);
	}
}

/**
 * Clear the persisted dispatcher session id from the given
 * `storage`. Called from the "Clear local state and reload" UX when
 * the server-side session is gone.
 */
export function clearDispatcherSessionId(storage: Storage): void {
	try {
		storage.removeItem(DISPATCHER_SESSION_ID_KEY);
	} catch {
		// best-effort
	}
}
