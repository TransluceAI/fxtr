/**
 * State machine driving the agent-MCP panel: subscribe to the
 * agent's chain head, then maintain a growing list of
 * ``(chain-head stub, chain-head load result)`` pairs as new
 * extensions arrive via the inbox.
 *
 * Architecturally a thin :func:`atomEffect` factory:
 *
 * - The panel owns a :data:`FxtrSessionAtom<PanelState>` (lifted
 *   from its ``ownStorage``).
 * - :func:`makeAgentPanelDriver` returns one ``atomEffect`` that
 *   reads the panel state, dispatches the next transition, and
 *   writes back. Subscribing to the driver atom keeps it mounted;
 *   the panel does this via ``useAtomValue(driverAtom)`` even
 *   though it discards the value.
 *
 * The state machine is intentionally tiny — three live phases
 * plus a terminal errored phase:
 *
 * 1. ``init`` — first pass. Allocate the inbox + dispatch a
 *    callback-target ``subscribe_to(serviceStub)`` invoke on the
 *    inbox's own handle (same tick, same wire mutation — the
 *    workstation subscribes the inbox to the agent service and
 *    owns the lifecycle). Transition to ``subscribing``.
 * 2. ``subscribing`` — wait for the subscribe invoke's result (an
 *    ack; ``subscribe_to`` returns ``None``). On fill, transition
 *    to ``running``. On error, transition to ``errored``.
 * 3. ``running`` — drain inbox events; each delivered head ref
 *    appends a fresh ``(stub, allocateLoad)`` entry. The current
 *    chain head (when the log is non-empty) arrives as the
 *    subscription's **first event** (the backend hub's
 *    deliver-current seed), so no separate snapshot seeding is
 *    needed; the hub's no-dup-no-miss window means we append every
 *    delivered event unconditionally.
 *
 * Per ``Session.userState`` discipline, every allocated stub
 * (inbox, subscribe invoke result, per-entry load) lands in
 * ``panelState`` before end-of-flush so the session reducer
 * doesn't GC it. The ``draftMessage`` / ``lastSend`` /
 * ``lastSendDraft`` fields are owned here too (lives on
 * ``PanelState``), even though the driver doesn't write them —
 * the send-box component does, in its own updaters.
 */

import type {
	CallbackInboxStub,
	EntityRefStub,
	FxtrSessionAtom,
	OpResultStub,
	ServiceRefStub,
} from "@fxtr-ui/core";
import type { Atom } from "jotai";
import { atomEffect } from "jotai-effect";

import type { ChainNodeValue } from "./data_shapes.js";
import type { AgentMcpRunningSessionProto } from "./proto.js";

/**
 * One entry in the panel's live list. The ``load`` slot's
 * fill value is the wire-decoded :class:`ChainNodeValue`
 * (``prev`` + ``event`` + ``timestamp``); the panel reads
 * ``load.result`` reactively and renders accordingly.
 */
export interface ChainEntry {
	readonly stub: EntityRefStub;
	readonly load: OpResultStub<ChainNodeValue>;
}

/**
 * Three live phases of the panel state machine + a terminal
 * errored phase. ``subscribing`` and ``running`` both anchor
 * the inbox stub in panel state so the session reducer keeps
 * its slot alive.
 */
export type PanelPhase =
	| { readonly kind: "init" }
	| {
			readonly kind: "subscribing";
			readonly inbox: CallbackInboxStub<EntityRefStub>;
			readonly subscribe: OpResultStub<null>;
	  }
	| {
			readonly kind: "running";
			readonly inbox: CallbackInboxStub<EntityRefStub>;
	  }
	| { readonly kind: "errored"; readonly message: string };

/**
 * Full panel-state shape persisted under the panel session's
 * ``ownStorage`` slot.
 *
 * - ``phase`` rides the subscribe lifecycle.
 * - ``entries`` is the live chronologically-ordered list of
 *   chain-head refs the panel has seen plus their per-entry
 *   load slots.
 * - ``draftMessage`` is the send-box's controlled input value.
 * - ``lastSend`` anchors the most-recent ``send`` invoke's
 *   result slot so the session reducer keeps it alive across
 *   the round-trip; the send-box reads it reactively to surface
 *   "sending…" / "send failed: …" feedback.
 * - ``lastSendDraft`` carries the text that backed
 *   ``lastSend`` so an errored send can offer the original
 *   text back to the user for retry / edit.
 */
export interface PanelState {
	readonly phase: PanelPhase;
	readonly entries: ReadonlyArray<ChainEntry>;
	readonly draftMessage: string;
	readonly lastSend: OpResultStub<null> | null;
	readonly lastSendDraft: string | null;
}

export const INITIAL_PANEL_STATE: PanelState = {
	phase: { kind: "init" },
	entries: [],
	draftMessage: "",
	lastSend: null,
	lastSendDraft: null,
};

/**
 * Build the driver atom for a panel. Subscribing to the returned
 * atom keeps the effect mounted; the effect re-runs on every
 * relevant atom change (the panel state, the inbox events, the
 * subscribe slot's result).
 *
 * The driver never reads ``draftMessage`` / ``lastSend`` /
 * ``lastSendDraft`` — those are the send-box's territory and
 * change frequently. Reads of unused fields would just trigger
 * pointless re-runs without affecting the FSM's transitions.
 * The driver scopes its reads narrowly to the fields it needs.
 */
export function makeAgentPanelDriver(
	panelStateAtom: FxtrSessionAtom<PanelState>,
	serviceStub: ServiceRefStub<AgentMcpRunningSessionProto>,
): Atom<void> {
	return atomEffect((get, set) => {
		const state = get(panelStateAtom);

		if (state.phase.kind === "errored") {
			// Terminal — nothing to do.
			return;
		}

		if (state.phase.kind === "init") {
			// First pass: allocate the inbox + dispatch the
			// callback-target ``subscribe_to`` invoke on its handle,
			// with the agent service as the event source — one tick,
			// one wire mutation. Both stubs anchor in the new phase so
			// they survive end-of-flush. No ``entries`` seeding — the
			// current head arrives as the first inbox event.
			set(panelStateAtom, (prev, ctx) => {
				const inbox = ctx.allocateCallback<EntityRefStub>();
				const subscribe = ctx.allocateInvoke<null>(inbox.handle, "subscribe_to", [serviceStub]);
				return {
					...prev,
					phase: { kind: "subscribing", inbox, subscribe },
				};
			});
			return;
		}

		if (state.phase.kind === "subscribing") {
			const result = get(state.phase.subscribe.result);
			if (result.status === "pending") return;
			if (result.status === "errored") {
				set(panelStateAtom, (prev) => ({
					...prev,
					phase: { kind: "errored", message: result.message },
				}));
				return;
			}
			// Filled — ``subscribe_to`` returns ``None``; the fill is
			// purely the subscription ack. Promote the phase to
			// ``running``. No seeding: the current head (when one
			// exists) arrives as the subscription's first inbox event
			// and is appended by the ``running`` arm like any other.
			const subscribingPhase = state.phase;
			set(panelStateAtom, (prev) => ({
				...prev,
				phase: { kind: "running", inbox: subscribingPhase.inbox },
			}));
			return;
		}

		// state.phase.kind === "running"
		const events = get(state.phase.inbox.events);
		if (events.length === 0) return;

		// Drain delivered head refs. Per the backend hub's
		// deliver-current no-dup-no-miss window (see
		// :meth:`AgentMcpRunningSessionImpl.subscribe`'s docstring),
		// the current head arrives exactly once — as the first event
		// — so we can append every event unconditionally without
		// dedupe.
		const runningPhase = state.phase;
		set(panelStateAtom, (prev, ctx) => {
			const newEntries: ChainEntry[] = [...prev.entries];
			for (const ref of events) {
				const load = ctx.allocateLoad(ref as EntityRefStub<ChainNodeValue>);
				newEntries.push({ stub: ref, load });
			}
			// `sliced(n)` returns a fresh inbox stub at offset + n;
			// typed identically because the payload type doesn't
			// change. We re-anchor the new stub into PanelState so
			// it survives reducer flush.
			const slicedInbox = runningPhase.inbox.sliced(
				events.length,
			) as CallbackInboxStub<EntityRefStub>;
			return {
				...prev,
				phase: { kind: "running", inbox: slicedInbox },
				entries: newEntries,
			};
		});
	});
}
