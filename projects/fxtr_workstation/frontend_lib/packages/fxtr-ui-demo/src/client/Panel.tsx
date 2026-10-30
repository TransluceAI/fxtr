/**
 * One panel = one session via `<FxtrSessionProvider>`. Renders the
 * lifecycle badge, a text input bound to `session.userState.message`,
 * a "load demo-note" button + result display, an invokes list, and
 * an inboxes list. Both lists use the same sub-atom pattern — each
 * entry is its own card driving its own sub-atom slice of the
 * user-state tree.
 *
 * State shape:
 *
 *   PanelUserState = {
 *     message?: string,
 *     loaded?: OpResultStub,
 *     invokes: ReadonlyArray<InvokeSlot>,
 *     inboxes: ReadonlyArray<InboxSlot>,
 *   }
 *
 *   InvokeSlot = {
 *     id: number,          // stable React key
 *     label?: string,      // last-invoked method label, for display
 *     result?: OpResultStub<unknown>,
 *   }
 *
 *   InboxSlot = {
 *     id: number,                              // stable React key
 *     inbox: CallbackInboxStub<unknown>,       // cursor stub (slot + offset)
 *     lastNotifyResult?: OpResultStub<unknown>, // result of the most recent notify invoke
 *   }
 *
 * Duplicating an inbox card pushes a new `InboxSlot` carrying the
 * *current entry's* stub instance. Both entries reference the same
 * underlying callback slot (so new events flow to both) but their
 * cursors advance independently via `sliced` because they live in
 * separate sub-atoms.
 *
 * Read + write via vanilla jotai operations against the user-state
 * atom — there are no privileged session-level hooks. The root grabs
 * a typed `FxtrSessionAtom<PanelUserState>` via `useSessionRootStateAtom`
 * (which lifts away the null), then `useSubAtom` does the rest.
 */

import type {
	WorkstationCallbackInboxStub,
	WorkstationSession,
} from "@fxtr-ui/connector-workstation";
import type {
	CallbackInboxStub,
	EntityRefStub,
	FxtrSessionAtom,
	OpResultStub,
	ResultStatus,
	ServiceRefStub,
} from "@fxtr-ui/core";
import {
	FxtrSessionProvider,
	useResult,
	useSession,
	useSessionRootStateAtom,
	useSubAtom,
} from "@fxtr-ui/react";
import { stringifyForDebug } from "@fxtr-ui/state-util";
import { useAtomValue, useSetAtom } from "jotai";
import type { ChangeEvent } from "react";
import type { _DemoServiceProto } from "./_demo_service_proto";

/** Next id for a list of `{id: number}` entries: `max(ids) + 1`. */
function nextId<T extends { id: number }>(list: ReadonlyArray<T>): number {
	let max = 0;
	for (const entry of list) if (entry.id > max) max = entry.id;
	return max + 1;
}

export interface LoadTarget {
	readonly ref: EntityRefStub;
	readonly name: string;
	readonly shortId: string;
}

export interface InvokeTarget {
	readonly ref: ServiceRefStub<_DemoServiceProto>;
	readonly name: string;
	readonly shortId: string;
}

interface InvokeSlot {
	readonly id: number;
	readonly label?: string;
	readonly result?: OpResultStub<unknown>;
}

interface InboxSlot {
	readonly id: number;
	readonly inbox: CallbackInboxStub<unknown>;
	readonly lastNotifyResult?: OpResultStub<unknown>;
}

interface PanelUserState {
	readonly message?: string;
	readonly loaded?: OpResultStub<unknown>;
	readonly invokes: ReadonlyArray<InvokeSlot>;
	readonly inboxes: ReadonlyArray<InboxSlot>;
}

const PANEL_USER_STATE_DEFAULT: PanelUserState = { invokes: [], inboxes: [] };

const LIFECYCLE_COLORS = {
	connecting: "#e0a800",
	connected: "#28a745",
	stale: "#e07700",
	closed: "#dc3545",
	unavailable: "#dc3545",
} as const;

type DemoMethod = keyof _DemoServiceProto;

interface InvocationSpec {
	readonly method: DemoMethod;
	readonly label: string;
	readonly run: (
		ctx: import("@fxtr-ui/core").FxtrSessionUpdateCtx,
		svc: ServiceRefStub<_DemoServiceProto>,
	) => OpResultStub<unknown>;
}

const INVOCATIONS: readonly InvocationSpec[] = [
	{
		method: "echo",
		label: "echo(42)",
		run: (ctx, svc) => ctx.allocateInvoke(svc, "echo", [42]),
	},
	{
		method: "combine",
		label: 'combine("v", name="alice")',
		run: (ctx, svc) => ctx.allocateInvoke(svc, "combine", ["v"], { name: "alice" }),
	},
	{
		method: "fail",
		label: 'fail("oops")',
		run: (ctx, svc) => ctx.allocateInvoke(svc, "fail", ["oops"]),
	},
];

export function Panel({
	session,
	loadTarget,
	invokeTarget,
	onClose,
}: {
	readonly session: WorkstationSession;
	readonly loadTarget: LoadTarget | null;
	readonly invokeTarget: InvokeTarget | null;
	readonly onClose: () => void;
}) {
	return (
		<FxtrSessionProvider session={session}>
			<PanelBody loadTarget={loadTarget} invokeTarget={invokeTarget} onClose={onClose} />
		</FxtrSessionProvider>
	);
}

function PanelBody({
	loadTarget,
	invokeTarget,
	onClose,
}: {
	readonly loadTarget: LoadTarget | null;
	readonly invokeTarget: InvokeTarget | null;
	readonly onClose: () => void;
}) {
	const session = useSession();
	const lifecycle = useAtomValue(session.lifecycle);

	// Root-level lift: null userState → typed PanelUserState with empty
	// invokes / inboxes. Every sub-atom below reads through this.
	const userStateAtom = useSessionRootStateAtom<PanelUserState>(PANEL_USER_STATE_DEFAULT);
	const userState = useAtomValue(userStateAtom);

	const messageAtom = useSubAtom(userStateAtom, "message");
	const loadedAtom = useSubAtom(userStateAtom, "loaded");
	const invokesAtom = useSubAtom(userStateAtom, "invokes");
	const inboxesAtom = useSubAtom(userStateAtom, "inboxes");

	const message = useAtomValue(messageAtom);
	const loaded = useAtomValue(loadedAtom);
	const invokes = useAtomValue(invokesAtom);
	const inboxes = useAtomValue(inboxesAtom);
	const setMessage = useSetAtom(messageAtom);
	const setLoaded = useSetAtom(loadedAtom);
	const setInvokes = useSetAtom(invokesAtom);
	const setInboxes = useSetAtom(inboxesAtom);

	// IDs are derived from current state (`max(ids) + 1` per list)
	// rather than a component-local counter — the lists are
	// persisted in session state, so a panel remount (page reload,
	// re-attach) would otherwise restart the counter at 1 and
	// collide with existing entries.

	const handleChange = (e: ChangeEvent<HTMLInputElement>): void => {
		setMessage(e.target.value);
	};

	const handleLoad = (): void => {
		if (!loadTarget) return;
		setLoaded((_prev, ctx) => ctx.allocateLoad(loadTarget.ref));
	};

	const handleAddInvoke = (): void => {
		setInvokes((prev) => [...prev, { id: nextId(prev) }]);
	};

	const handleRemoveInvoke = (idx: number): void => {
		setInvokes((prev) => prev.filter((_, i) => i !== idx));
	};

	const handleAddInbox = (): void => {
		// allocateCallback mints a fresh slot + stub; storing the
		// stub into state fires CreateCallbackOp on the next flush.
		setInboxes((prev, ctx) => [...prev, { id: nextId(prev), inbox: ctx.allocateCallback() }]);
	};

	const handleRemoveInbox = (idx: number): void => {
		// If this was the only entry referencing the inbox's slot,
		// the reducer's diff will emit CloseCallbackOp; duplicates
		// keep the slot alive.
		setInboxes((prev) => prev.filter((_, i) => i !== idx));
	};

	const handleDuplicateInbox = (idx: number): void => {
		setInboxes((prev) => {
			const original = prev[idx];
			if (!original) return prev;
			// New entry carries the *current* stub instance verbatim
			// — same (slot, offset) initially. Each entry's cursor
			// advances independently via its own `sliced` write.
			return [...prev, { id: nextId(prev), inbox: original.inbox }];
		});
	};

	const canLoad = lifecycle === "connected" && loadTarget !== null;
	const canInvoke = lifecycle === "connected" && invokeTarget !== null;

	return (
		<div
			style={{
				border: "1px solid #ccc",
				borderRadius: "4px",
				background: "#fff",
				padding: "10px",
				display: "flex",
				flexDirection: "column",
				gap: "8px",
			}}
		>
			<div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
				<span
					title={lifecycle}
					style={{
						display: "inline-block",
						width: "8px",
						height: "8px",
						borderRadius: "50%",
						background: LIFECYCLE_COLORS[lifecycle],
					}}
				/>
				<span style={{ flex: 1, fontFamily: "monospace", fontSize: "11px", color: "#444" }}>
					{session.sessionId.slice(0, 8)}…
				</span>
				<button
					type="button"
					onClick={onClose}
					title="Close this panel (server session keeps running)"
					style={{ padding: "2px 6px", fontSize: "11px" }}
				>
					×
				</button>
			</div>

			<label style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
				<span style={{ fontSize: "11px", color: "#666" }}>message</span>
				<input
					type="text"
					value={message ?? ""}
					onChange={handleChange}
					disabled={lifecycle !== "connected"}
					placeholder={lifecycle === "connected" ? "type to write user state…" : `(${lifecycle})`}
					style={{ padding: "4px 6px", fontSize: "13px" }}
				/>
			</label>

			<div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
				<button
					type="button"
					onClick={handleLoad}
					disabled={!canLoad || loaded !== undefined}
					title={
						loadTarget
							? `Allocate LoadEntityOp for ${loadTarget.name} (${loadTarget.shortId})`
							: "entity list not loaded yet"
					}
					style={{ padding: "4px 8px", fontSize: "12px" }}
				>
					{loaded ? "loaded" : loadTarget ? `load ${loadTarget.name}` : "loading entities…"}
				</button>
			</div>
			{loaded ? <OpResultDisplay label="load" stub={loaded} /> : null}

			<div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
				<span style={{ fontSize: "11px", color: "#666" }}>invokes:</span>
				<button
					type="button"
					onClick={handleAddInvoke}
					disabled={!canInvoke}
					title={
						invokeTarget
							? "Add a new invoke entry — click a method on the card to fire"
							: "service list not loaded yet"
					}
					style={{ padding: "4px 8px", fontSize: "12px" }}
				>
					+ new invoke
				</button>
			</div>
			{invokes.length > 0 ? (
				<div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
					{invokes.map((slot, idx) => (
						<InvokeCard
							key={slot.id}
							idx={idx}
							invokesAtom={invokesAtom}
							invokeTarget={invokeTarget}
							onRemove={() => handleRemoveInvoke(idx)}
						/>
					))}
				</div>
			) : null}

			<div style={{ display: "flex", alignItems: "center", gap: "6px" }}>
				<span style={{ fontSize: "11px", color: "#666" }}>inboxes:</span>
				<button
					type="button"
					onClick={handleAddInbox}
					disabled={lifecycle !== "connected"}
					title="Allocate a fresh callback inbox — fires CreateCallbackOp"
					style={{ padding: "4px 8px", fontSize: "12px" }}
				>
					+ new inbox
				</button>
			</div>
			{inboxes.length > 0 ? (
				<div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
					{inboxes.map((slot, idx) => (
						<InboxCard
							key={slot.id}
							idx={idx}
							inboxesAtom={inboxesAtom}
							invokeTarget={invokeTarget}
							onRemove={() => handleRemoveInbox(idx)}
							onDuplicate={() => handleDuplicateInbox(idx)}
						/>
					))}
				</div>
			) : null}

			<div style={{ fontSize: "10px", color: "#888" }}>
				userState: <code>{stringifyForDebug(userState)}</code>
			</div>
		</div>
	);
}

/**
 * One invoke entry. Drives a `FxtrSessionAtom<InvokeSlot>` projected
 * via `useSubAtom(invokesAtom, idx)` — its read/write surface is
 * entirely scoped to its slot. Clicking a method writes
 * `{ ...prev, label, result: ctx.allocateInvoke(...) }` to the slot;
 * "clear" resets `label` / `result`; "remove" calls `onRemove` on
 * the parent which splices the slot out of the list.
 */
function InvokeCard({
	idx,
	invokesAtom,
	invokeTarget,
	onRemove,
}: {
	readonly idx: number;
	readonly invokesAtom: import("@fxtr-ui/core").FxtrSessionAtom<ReadonlyArray<InvokeSlot>>;
	readonly invokeTarget: InvokeTarget | null;
	readonly onRemove: () => void;
}) {
	const entryAtom = useSubAtom(invokesAtom, idx);
	const entry = useAtomValue(entryAtom);
	const setEntry = useSetAtom(entryAtom);

	const handleInvoke = (spec: InvocationSpec): void => {
		if (!invokeTarget) return;
		setEntry((prev, ctx) => ({
			...prev,
			label: spec.label,
			result: spec.run(ctx, invokeTarget.ref),
		}));
	};

	const handleClear = (): void => {
		setEntry((prev) => ({ id: prev.id }));
	};

	const canFire = invokeTarget !== null;

	return (
		<div
			style={{
				border: "1px solid #ddd",
				borderRadius: "3px",
				padding: "6px 8px",
				background: "#fafafa",
				display: "flex",
				flexDirection: "column",
				gap: "4px",
			}}
		>
			<div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
				<span style={{ fontSize: "10px", color: "#888", fontFamily: "monospace" }}>
					#{entry.id}
				</span>
				{INVOCATIONS.map((spec) => (
					<button
						key={spec.method}
						type="button"
						onClick={() => handleInvoke(spec)}
						disabled={!canFire || entry.result !== undefined}
						style={{ padding: "2px 6px", fontSize: "11px" }}
					>
						{spec.label}
					</button>
				))}
				<span style={{ flex: 1 }} />
				<button
					type="button"
					onClick={handleClear}
					disabled={entry.result === undefined && entry.label === undefined}
					title="Reset this entry to empty"
					style={{ padding: "2px 6px", fontSize: "11px" }}
				>
					clear
				</button>
				<button
					type="button"
					onClick={onRemove}
					title="Remove this entry from the list"
					style={{ padding: "2px 6px", fontSize: "11px" }}
				>
					×
				</button>
			</div>
			{entry.result ? (
				<OpResultDisplay label={`invoke ${entry.label ?? "?"}`} stub={entry.result} />
			) : null}
		</div>
	);
}

/**
 * One inbox entry. Drives a `FxtrSessionAtom<InboxSlot>` projected
 * via `useSubAtom(inboxesAtom, idx)`. Renders the cursor identity
 * (slot / offset / state), the visible events, the
 * `lastNotifyResult` (if any), and per-entry action buttons:
 *
 *   - "notify random" → allocates an `invoke(notify_random, [handle])`
 *     and stores the resulting `OpResultStub` as `lastNotifyResult`
 *     for display. The Python side picks an int in [0, 99] and calls
 *     `cb.notify(value)`; the `CallbackEvent` appears in this entry's
 *     events list (and any duplicate's, since they share the slot).
 *   - "drop first" → `inbox.sliced(1)`. Disabled when the cursor sees
 *     no events.
 *   - "duplicate" → parent pushes a new `InboxSlot` carrying the same
 *     stub instance. Cursors advance independently from there.
 *   - "×" → removes the entry. If this was the last entry referencing
 *     this inbox's slot, the reducer emits `CloseCallbackOp` on the
 *     next flush.
 */
function InboxCard({
	idx,
	inboxesAtom,
	invokeTarget,
	onRemove,
	onDuplicate,
}: {
	readonly idx: number;
	readonly inboxesAtom: FxtrSessionAtom<ReadonlyArray<InboxSlot>>;
	readonly invokeTarget: InvokeTarget | null;
	readonly onRemove: () => void;
	readonly onDuplicate: () => void;
}) {
	const entryAtom = useSubAtom(inboxesAtom, idx);
	const entry = useAtomValue(entryAtom);
	const setEntry = useSetAtom(entryAtom);

	const events = useAtomValue(entry.inbox.events);
	const state = useAtomValue(entry.inbox.state);
	const committed = useAtomValue(entry.inbox.committed);
	// `slot` is exposed by the concrete `WorkstationCallbackInboxStub`
	// — the abstract base hides it (it's connector-specific
	// identity). The demo is workstation-bound, so reaching for the
	// concrete class is fine here.
	const slot = (entry.inbox as WorkstationCallbackInboxStub).slot;
	const offset = entry.inbox.offset;

	const handleNotify = (): void => {
		if (!invokeTarget) return;
		setEntry((prev, ctx) => ({
			...prev,
			lastNotifyResult: ctx.allocateInvoke(invokeTarget.ref, "notify_random", [prev.inbox.handle]),
		}));
	};

	const handleDropFirst = (): void => {
		setEntry((prev) => ({ ...prev, inbox: prev.inbox.sliced(1) }));
	};

	const canNotify = invokeTarget !== null && state !== "closed";
	const canDropFirst = events.length > 0;

	return (
		<div
			style={{
				border: "1px solid #ddd",
				borderRadius: "3px",
				padding: "6px 8px",
				background: "#fafafa",
				display: "flex",
				flexDirection: "column",
				gap: "4px",
			}}
		>
			<div style={{ display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
				<span style={{ fontSize: "10px", color: "#888", fontFamily: "monospace" }}>
					#{entry.id}
				</span>
				<span style={{ fontSize: "10px", color: "#666", fontFamily: "monospace" }}>
					slot={slot} offset={offset} · {state} · {committed ? "committed" : "uncommitted"}
				</span>
				<span style={{ flex: 1 }} />
				<button
					type="button"
					onClick={handleNotify}
					disabled={!canNotify}
					title={
						invokeTarget
							? "Invoke notify_random(this.handle) — server picks an int and notifies"
							: "service list not loaded yet"
					}
					style={{ padding: "2px 6px", fontSize: "11px" }}
				>
					notify random
				</button>
				<button
					type="button"
					onClick={handleDropFirst}
					disabled={!canDropFirst}
					title="Advance the cursor by 1 (sliced(1)) — unpreserves the first visible event"
					style={{ padding: "2px 6px", fontSize: "11px" }}
				>
					drop first
				</button>
				<button
					type="button"
					onClick={onDuplicate}
					title="Push a new inbox entry sharing this slot — cursors advance independently"
					style={{ padding: "2px 6px", fontSize: "11px" }}
				>
					duplicate
				</button>
				<button
					type="button"
					onClick={onRemove}
					title="Remove this entry. If it's the last entry for this slot, fires CloseCallbackOp."
					style={{ padding: "2px 6px", fontSize: "11px" }}
				>
					×
				</button>
			</div>
			{events.length > 0 ? (
				<div
					style={{
						border: "1px solid #eee",
						borderRadius: "3px",
						padding: "4px 6px",
						fontSize: "11px",
						background: "#fff",
						display: "flex",
						flexDirection: "column",
						gap: "2px",
					}}
				>
					<div style={{ fontSize: "10px", color: "#666" }}>events ({events.length}):</div>
					{events.map((ev, i) => (
						// biome-ignore lint/suspicious/noArrayIndexKey: payloads aren't unique; index is fine for display
						<div key={i} style={{ fontFamily: "monospace", fontSize: "11px" }}>
							{stringifyForDebug(ev)}
						</div>
					))}
				</div>
			) : (
				<div style={{ fontSize: "11px", color: "#888", fontStyle: "italic" }}>
					no events visible from this cursor
				</div>
			)}
			{entry.lastNotifyResult ? (
				<OpResultDisplay label="notify_random" stub={entry.lastNotifyResult} />
			) : null}
		</div>
	);
}

function OpResultDisplay({
	label,
	stub,
}: {
	readonly label: string;
	readonly stub: OpResultStub<unknown>;
}) {
	const result: ResultStatus<unknown> = useResult(stub);
	const committed = useAtomValue(stub.committed);
	return (
		<div
			style={{
				border: "1px solid #eee",
				borderRadius: "3px",
				padding: "6px 8px",
				fontSize: "11px",
				background: "#fff",
				display: "flex",
				flexDirection: "column",
				gap: "3px",
			}}
		>
			<div style={{ display: "flex", gap: "6px", color: "#666" }}>
				<span style={{ fontWeight: 600 }}>
					{label}: {result.status}
				</span>
				<span>·</span>
				<span>{committed ? "committed" : "uncommitted"}</span>
			</div>
			{result.status === "filled" ? (
				<pre
					style={{
						margin: 0,
						padding: "4px 6px",
						background: "#fafafa",
						border: "1px solid #eee",
						borderRadius: "2px",
						fontSize: "11px",
						overflow: "auto",
					}}
				>
					{stringifyForDebug(result.value, 2)}
				</pre>
			) : result.status === "errored" ? (
				<div style={{ color: "#c00" }}>{result.message}</div>
			) : (
				<div style={{ color: "#888", fontStyle: "italic" }}>pending…</div>
			)}
		</div>
	);
}
