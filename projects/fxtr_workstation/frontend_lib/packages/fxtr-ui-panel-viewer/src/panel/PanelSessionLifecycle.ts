/**
 * `usePanelSession` — drives the pending → live → unavailable
 * state machine for one dockview panel's session.
 *
 * Attach-only: the shell owns session creation. A panel that
 * mounts with `sessionId === null` simply renders a pending body
 * and waits; once the shell has minted a session and written the
 * sessionId back into dockview params, the hook attaches.
 *
 * States:
 *
 *   - `pending`     — `sessionId` is `null`. Shell is (or will be)
 *                     creating the session out-of-band.
 *   - `live`        — `sessionId` is non-null and a
 *                     `Session` is attached. Its own
 *                     `lifecycle` atom (`connecting` → `connected`
 *                     → `stale` → `closed`) is the further-detail
 *                     signal — `live` only means "we hold a
 *                     handle"; consumers read `session.lifecycle`
 *                     for finer gating of e.g. write-button
 *                     enablement.
 *   - `unavailable` — terminal. The attach failed (server doesn't
 *                     know this session id). Render a
 *                     `SessionUnavailable` body.
 *
 * On unmount: detaches the session. Server-side state survives —
 * terminate-on-panel-close is the shell's responsibility via
 * dockview's panel-removed event.
 *
 * The effect re-fires when `sessionId` changes (the normal
 * `null → "abc"` transition is exactly this). Drifting between
 * two distinct non-null ids is supported (cleanup detaches the
 * old, setup attaches the new), but a sessionId-drift tripwire
 * warns at runtime since the shell shouldn't be doing this in
 * practice — for swapping sessions, prefer remounting the panel
 * content via a different dockview component key.
 *
 * React StrictMode dev double-invocation is harmless: the only
 * thing the effect does is a synchronous `attachSession` (and an
 * async `detach` on cleanup). One wasted subscribe/unsubscribe
 * round-trip per panel; no leaks (the session_id is owned by the
 * shell and survives the extra detach).
 */

import type { Connector, Session } from "@fxtr-ui/core";
import type { SessionLifecycleState } from "@fxtr-ui/core";
import { atom, useAtomValue } from "jotai";
import { useEffect, useMemo, useRef, useState } from "react";

export type PanelSessionStatus = "pending" | "live" | "unavailable";

export type PanelSessionState =
	| { readonly status: "pending" }
	| { readonly status: "live"; readonly session: Session }
	| { readonly status: "unavailable"; readonly missingSessionId: string };

export interface UsePanelSessionOptions {
	readonly connector: Connector;
	/**
	 * The session id to attach to, or `null` while the shell is
	 * still minting one. The shell writes the new id back into
	 * dockview params once `createSession` resolves; the panel
	 * re-renders, and this hook attaches.
	 */
	readonly sessionId: string | null;
}

export function usePanelSession({
	connector,
	sessionId,
}: UsePanelSessionOptions): PanelSessionState {
	const [session, setSession] = useState<Session | null>(null);

	useEffect(() => {
		if (sessionId === null) {
			// Nothing to do; the shell will fill `sessionId` in
			// when its `createSession` resolves.
			return;
		}
		const s = connector.attachSession(sessionId);
		setSession(s);
		return () => {
			// Drop our reference + close our subscription.
			// `setSession(null)` so the next attach (if `sessionId`
			// shifts to a new id) doesn't briefly show stale state.
			setSession(null);
			s.detach().catch(() => {});
		};
	}, [connector, sessionId]);

	// Subscribe to the session's lifecycle atom (or a fixed `null`
	// when we don't have a session yet, so `useAtomValue` can be
	// called unconditionally).
	const lifecycleSourceAtom = useMemo(
		() => session?.lifecycle ?? atom<SessionLifecycleState | null>(null),
		[session],
	);
	const lifecycle = useAtomValue(lifecycleSourceAtom);

	// Sessionid-drift tripwire. Once a non-null `sessionId` has
	// been observed, any *subsequent* change to a different non-null
	// id is unusual: the shell shouldn't be mutating a panel's
	// sessionId in place. We allow the transition silently (the
	// effect cleans up correctly) but warn once per distinct
	// drifted-to value.
	const firstNonNullSessionIdRef = useRef<string | null>(null);
	const driftWarnedForRef = useRef<string | null>(null);
	useEffect(() => {
		if (sessionId === null) return;
		if (firstNonNullSessionIdRef.current === null) {
			firstNonNullSessionIdRef.current = sessionId;
			return;
		}
		if (sessionId === firstNonNullSessionIdRef.current) return;
		if (driftWarnedForRef.current === sessionId) return;
		driftWarnedForRef.current = sessionId;
		console.warn(
			`[fxtr-ui] usePanelSession: sessionId changed from "${firstNonNullSessionIdRef.current}" to "${sessionId}". The hook handles the swap (detach old + attach new), but in-place session swaps are unusual — prefer remounting the panel content via a different dockview component key.`,
		);
	}, [sessionId]);

	if (sessionId === null || session === null) {
		return { status: "pending" };
	}
	if (lifecycle === "unavailable") {
		return { status: "unavailable", missingSessionId: session.sessionId };
	}
	return { status: "live", session };
}
