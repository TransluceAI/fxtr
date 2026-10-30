/**
 * Left sidebar: live list of workstation sessions plus a "New
 * session" button. "open" pushes a fresh `attachSession` of the
 * given id onto the panel grid.
 *
 * Resource lists are connector-scoped; we read `listSessions()`'s
 * `records` atom against the surrounding jotai `<Provider
 * store={connector.store}>`. (The connector caches the underlying
 * subscription so multiple `listSessions()` calls share state.)
 */

import type { SessionRecord, WorkstationConnector } from "@fxtr-ui/connector-workstation";
import type { WorkstationSession } from "@fxtr-ui/connector-workstation";
import { useAtomValue } from "jotai";
import { useMemo, useState } from "react";

const WAIT_CONNECTED_TIMEOUT_MS = 10_000;

/**
 * Resolve once `session.lifecycle` reaches `"connected"`. Rejects if
 * it transitions to `"closed"` first, or if the timeout elapses
 * before settling. Used to gate the terminate flow on the snapshot
 * having landed (snapshot arrival is what hydrates `lastProcessedSeq`
 * past the workstation's retention horizon; any mutation before that
 * is rejected with `validation_failed`).
 */
function waitForConnected(session: WorkstationSession): Promise<void> {
	const store = session.store;
	const current = store.get(session.lifecycle);
	if (current === "connected") return Promise.resolve();
	if (current === "closed") {
		return Promise.reject(new Error("session already closed"));
	}
	return new Promise<void>((resolve, reject) => {
		const timer = setTimeout(() => {
			unsub();
			reject(new Error(`session did not reach "connected" within ${WAIT_CONNECTED_TIMEOUT_MS}ms`));
		}, WAIT_CONNECTED_TIMEOUT_MS);
		const unsub = store.sub(session.lifecycle, () => {
			const lc = store.get(session.lifecycle);
			if (lc === "connected") {
				clearTimeout(timer);
				unsub();
				resolve();
			} else if (lc === "closed") {
				clearTimeout(timer);
				unsub();
				reject(new Error("session closed before reaching connected"));
			}
		});
	});
}

export function SessionSidebar({
	connector,
	onOpen,
}: {
	readonly connector: WorkstationConnector;
	readonly onOpen: (session: WorkstationSession) => void;
}) {
	const list = useMemo(() => connector.listSessions(), [connector]);
	const records = useAtomValue(list.records) as readonly SessionRecord[];
	const status = useAtomValue(list.status);
	const [creating, setCreating] = useState(false);
	// Per-row terminating state, keyed by session_id. Disables both the
	// row's open + terminate buttons while a terminate is in flight.
	const [terminating, setTerminating] = useState<ReadonlySet<string>>(() => new Set());

	const handleCreate = async (): Promise<void> => {
		setCreating(true);
		try {
			console.log("[demo] createSession…");
			const session = await connector.createSession({});
			console.log(`[demo] createSession → ${session.sessionId}`);
			onOpen(session);
		} catch (err) {
			console.error("[demo] createSession failed:", err);
		} finally {
			setCreating(false);
		}
	};

	const handleTerminate = async (sessionId: string): Promise<void> => {
		setTerminating((s) => new Set(s).add(sessionId));
		// Attach a fresh `WorkstationSession` just for the terminate call.
		// `session.terminate()` sets `local.terminated = true` and awaits
		// the CloseOp commit; the workstation tears down the server-side
		// session and any other clients viewing this session_id observe
		// the terminal state_update (their session lifecycles flip to
		// "closed"). The `listSessions` record disappears automatically
		// via the resource-list stream.
		//
		// We must wait for the snapshot to land before terminating — the
		// session's `lastProcessedSeq` is 0 until the snapshot's `seq`
		// hydrates it, and any mutation sent before then has an `ack_seq`
		// below the workstation's retention horizon, so the server rejects
		// with `validation_failed`. Waiting for lifecycle === "connected"
		// is the snapshot-received signal.
		const s = connector.attachSession(sessionId);
		try {
			console.log(`[demo] terminate session ${sessionId}`);
			await waitForConnected(s);
			await s.terminate();
			console.log(`[demo] terminate ${sessionId} committed`);
		} catch (err) {
			console.error(`[demo] terminate ${sessionId} failed:`, err);
		} finally {
			setTerminating((prev) => {
				const next = new Set(prev);
				next.delete(sessionId);
				return next;
			});
		}
	};

	return (
		<aside
			style={{
				width: "260px",
				borderRight: "1px solid #ddd",
				background: "#fff",
				padding: "12px",
				overflow: "auto",
			}}
		>
			<div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "12px" }}>
				<strong style={{ flex: 1 }}>Sessions</strong>
				<button
					type="button"
					onClick={() => void handleCreate()}
					disabled={creating}
					style={{ padding: "4px 8px" }}
				>
					+ new
				</button>
			</div>
			<div style={{ fontSize: "11px", color: "#888", marginBottom: "8px" }}>{status}</div>
			{records.length === 0 ? (
				<div style={{ color: "#888" }}>(no sessions)</div>
			) : (
				<ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
					{records.map((r) => {
						const isTerminating = terminating.has(r.session_id);
						return (
							<li
								key={r.session_id}
								style={{
									display: "flex",
									alignItems: "center",
									gap: "6px",
									padding: "4px 0",
									borderBottom: "1px solid #eee",
								}}
							>
								<span style={{ flex: 1, fontFamily: "monospace", fontSize: "11px" }}>
									{r.session_id.slice(0, 8)}…
								</span>
								<button
									type="button"
									onClick={() => onOpen(connector.attachSession(r.session_id))}
									disabled={isTerminating}
									style={{ padding: "2px 6px", fontSize: "11px" }}
								>
									open
								</button>
								<button
									type="button"
									onClick={() => void handleTerminate(r.session_id)}
									disabled={isTerminating}
									title="Destroy this session server-side"
									style={{ padding: "2px 6px", fontSize: "11px" }}
								>
									terminate
								</button>
							</li>
						);
					})}
				</ul>
			)}
		</aside>
	);
}
