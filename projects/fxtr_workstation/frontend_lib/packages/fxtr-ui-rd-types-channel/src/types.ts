/**
 * Wire-level types for output-channel updates as they arrive through a
 * workstation callback inbox, plus pure folds from an event sequence to
 * a renderable view.
 *
 * Mirrors the mashumaro encodings of the dataclasses in
 * `fxtr2.common.channels`, after the workstation's wire escape + the
 * connector's `unescapeWire` have run: ref leaves in message / status /
 * result values arrive as `EntityRefStub` instances inside `ResultValue`
 * payloads. **No event carries the tag** — a channel's tag is a property
 * fetched once via the reader's `get_tag` (the renderer holds it). And
 * **no channel payload carries a service ref** — a sub-computation is
 * referenced by its subchannel *key* (a string), opened via the reader's
 * `open_subchannel_reader`.
 *
 * Three streams ride one reader:
 * - **full** (`subscribe_full`): snapshot then live batched deltas —
 *   `ChannelUpdateWire`, folded by `foldChannelUpdates`.
 * - **state** (`subscribe_state`): cheap replace-only lifecycle/status —
 *   `ChannelStateEventWire`, reduced latest-wins by `latestChannelState`.
 * - **debug subchannels** (`subscribe_debug_subchannels`): a
 *   `SubchannelOpenedWire` per spawned subchannel.
 */

import type { ResultValue } from "@fxtr-ui/core";

/**
 * Wire shape of `ChannelState` — the state stream's per-event payload for
 * a started channel: current status + lifecycle `kind`.
 */
export type ChannelStateWire = {
	readonly kind: "open" | "complete" | "abort";
	readonly status: ResultValue;
};

/**
 * What the **state** stream delivers: a `ChannelStateWire` for a started
 * channel, or the statusless `aborted_before_start` variant if the channel
 * aborted before it ever started. Discriminated by `kind`.
 */
export type ChannelStateEventWire =
	| ChannelStateWire
	| { readonly kind: "aborted_before_start"; readonly reason: string };

/**
 * Wire shape of `SubchannelOpened` — one element of the debug subchannel
 * feed. `name` is the optional debug prefix passed to `spawn_subchannel`.
 * To render the subchannel, call `open_subchannel_reader(key)`.
 */
export type SubchannelOpenedWire = {
	readonly key: string;
	readonly name: string | null;
	readonly kind?: "subchannel_opened";
};

/**
 * Wire shape of `ChannelUpdate` — one element of a channel's **full**
 * subscription stream, discriminated by `kind`. The first event is a
 * snapshot (`snapshot_open` with the message log so far, or
 * `snapshot_complete` / `snapshot_abort` / `aborted_before_start` for a
 * terminal channel); live deltas (`update` / `complete` / `abort`) follow.
 * Every started-channel event carries the current `status`.
 */
export type ChannelUpdateWire =
	| {
			readonly kind: "snapshot_open";
			readonly status: ResultValue;
			readonly values: readonly ResultValue[];
	  }
	| {
			readonly kind: "snapshot_complete";
			readonly status: ResultValue;
			readonly result: ResultValue;
	  }
	| { readonly kind: "snapshot_abort"; readonly status: ResultValue; readonly reason: string }
	| {
			readonly kind: "update";
			readonly events: readonly ResultValue[];
			readonly status: ResultValue;
			readonly replace_history: boolean;
	  }
	| { readonly kind: "complete"; readonly result: ResultValue; readonly status: ResultValue }
	| { readonly kind: "abort"; readonly status: ResultValue; readonly reason: string }
	| { readonly kind: "aborted_before_start"; readonly reason: string };

/**
 * Renderable view of a channel's full subscription: what a card should
 * show after folding the updates received so far.
 *
 * `connecting` = no event yet (the snapshot hasn't landed, or the channel
 * hasn't started). The tag is **not** here — the renderer holds it from
 * `get_tag`. `status` threads through every started-channel phase (a
 * custom card may show it); `aborted`'s `status` is `null` for the
 * before-start case.
 */
export type ChannelViewState =
	| { readonly phase: "connecting" }
	| {
			readonly phase: "open";
			readonly values: readonly ResultValue[];
			readonly status: ResultValue;
	  }
	| { readonly phase: "complete"; readonly result: ResultValue; readonly status: ResultValue }
	| { readonly phase: "aborted"; readonly reason: string; readonly status: ResultValue | null };

const CONNECTING: ChannelViewState = { phase: "connecting" };

/**
 * Reduce the **state** stream to its latest event (or `null` if none yet).
 * The state hub is replace-only (coalesce), so latest-wins is the whole
 * fold; a renderer switches on the returned `kind`.
 */
export function latestChannelState(
	events: readonly ChannelStateEventWire[],
): ChannelStateEventWire | null {
	return events.length > 0 ? (events[events.length - 1] ?? null) : null;
}

/**
 * Fold a channel's **full** update sequence into a `ChannelViewState`.
 *
 * Pure; renderers call it with the inbox's full `events` array on every
 * render. Batched `update`s append all their `events`; `replace_history`
 * resets the accumulated log to that batch (live history-condense). The
 * current `status` threads through. Malformed input (deltas before any
 * snapshot, unknown kinds) folds to `aborted` with a diagnostic reason
 * rather than throwing, so one bad event can't take down the panel.
 */
export function foldChannelUpdates(events: readonly ChannelUpdateWire[]): ChannelViewState {
	let state: ChannelViewState = CONNECTING;
	for (const event of events) {
		state = foldOne(state, event);
		if (state.phase === "aborted") break;
	}
	return state;
}

function diagnosticAbort(state: ChannelViewState, message: string): ChannelViewState {
	const status = state.phase === "open" || state.phase === "complete" ? state.status : null;
	return { phase: "aborted", reason: message, status };
}

function foldOne(state: ChannelViewState, event: ChannelUpdateWire): ChannelViewState {
	switch (event.kind) {
		// Snapshots replace the whole state. Defensively accepted at any
		// position, not just as event 0.
		case "snapshot_open":
			return { phase: "open", values: event.values, status: event.status };
		case "snapshot_complete":
			return { phase: "complete", result: event.result, status: event.status };
		case "snapshot_abort":
			return { phase: "aborted", reason: event.reason, status: event.status };
		case "aborted_before_start":
			return { phase: "aborted", reason: event.reason, status: null };
		case "update": {
			if (state.phase !== "open") {
				return diagnosticAbort(state, `protocol error: "update" while ${state.phase}`);
			}
			const values = event.replace_history ? [...event.events] : [...state.values, ...event.events];
			return { phase: "open", values, status: event.status };
		}
		case "complete":
			if (state.phase !== "open") {
				return diagnosticAbort(state, `protocol error: "complete" while ${state.phase}`);
			}
			return { phase: "complete", result: event.result, status: event.status };
		case "abort":
			if (state.phase !== "open") {
				return diagnosticAbort(state, `protocol error: "abort" while ${state.phase}`);
			}
			return { phase: "aborted", reason: event.reason, status: event.status };
		default: {
			// Unknown kind: a newer backend speaking a richer protocol.
			const kind = (event as { readonly kind?: unknown }).kind;
			return diagnosticAbort(state, `protocol error: unknown event kind ${JSON.stringify(kind)}`);
		}
	}
}
