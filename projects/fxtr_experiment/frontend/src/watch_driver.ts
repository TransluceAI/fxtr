/**
 * The generic watch driver: one `atomEffect` FSM per watch slot that
 * keeps a workstation inbox subscribed to one of the capability's
 * `watch_*` methods and folds delivered events into the slot's mirror
 * (`derive/folds.ts`). The client half of `notes/event_system.md`'s
 * integration idiom:
 *
 * - a wanted spec with no live entry (or a stale one) allocates a
 *   FRESH inbox + the callback-target
 *   `subscribe_to(capability, method, **spec kwargs)` invoke in one
 *   tick — always fresh, because a live inbox refuses a second
 *   `subscribe_to`, and the workstation parker owns the unsubscribe
 *   lifecycle (nothing is torn down renderer-side). The spec rides as
 *   `subscribe_to`'s kwargs (the workstation forwards them to the watch
 *   method as `method(subscriber, **spec)` — the watch methods declare
 *   `path`/`coords` keyword-only);
 * - `spec === null` (nothing selected) drops the entry — removing the
 *   inbox stub from state IS the unsubscribe (the session reducer's
 *   `CloseCallbackOp` on unreferenced slots);
 * - events drain in delivery order through the family's pure fold into
 *   the mirror, re-anchoring `inbox.sliced(n)` in the same write;
 * - recovery edges reset the entry to null (the next run reallocates):
 *   `connectionState === "broken"` (the subscription's crash backstop
 *   — resubscribing for a fresh snapshot is the reconciliation
 *   protocol), a capability-stub change (a runner revive re-registers
 *   the service; old grants and subscriptions are dead vocabulary),
 *   and a spec change (navigation);
 * - a failed subscribe parks the entry `errored` (surfaced with a
 *   retry that writes the entry back to null).
 *
 * Writes go through the entry's own sub-atom with an identity guard
 * against entries that moved within the same flush; reads are the
 * entry atom + the inbox's reactive atoms, so the effect re-runs
 * exactly on subscription-relevant changes.
 */

import type {
	CallbackInboxStub,
	FxtrSessionAtom,
	FxtrSessionUpdateCtx,
	OpResultStub,
	ServiceRefStub,
} from "@fxtr-ui/core";
import type { Atom } from "jotai";
import { atomEffect } from "jotai-effect";

import type { ExperimentWorkflowProto } from "./wire/proto.js";

export type WatchPhase = "subscribing" | "running" | "errored";

/** One live watch slot: the subscription's plumbing + the folded
 * mirror, persisted in `ownStorage` (stubs are `StateValue`s). */
export type WatchEntry<Spec, Mirror> = {
	/** The capability stub this subscription belongs to (`shortId`):
	 * a fresh stub invalidates the entry. */
	readonly stubKey: string;
	/** What this watch addresses (the `subscribe_to` extra args). */
	readonly spec: Spec;
	readonly inbox: CallbackInboxStub<unknown>;
	/** The `subscribe_to` invoke — anchors its slot and drives the
	 * subscribing → running / errored transition. */
	readonly subscribe: OpResultStub<null>;
	readonly phase: WatchPhase;
	/** The subscribe failure, when `phase === "errored"`. */
	readonly message: string | null;
	readonly mirror: Mirror;
};

export function makeWatchDriver<Spec, Mirror>({
	entryAtom,
	specAtom,
	capability,
	method,
	specKwargs,
	sameSpec,
	emptyMirror,
	fold,
}: {
	readonly entryAtom: FxtrSessionAtom<WatchEntry<Spec, Mirror> | null>;
	/** What the view wants watched right now; null = no watch (the
	 * entry is dropped, unsubscribing server-side). */
	readonly specAtom: Atom<Spec | null>;
	readonly capability: ServiceRefStub<ExperimentWorkflowProto>;
	/** The capability's watch method name (`watch_workflow` / …). */
	readonly method: string;
	/** The spec's keyword args, forwarded by `subscribe_to` to the watch
	 * method as `method(subscriber, **specKwargs)`. */
	readonly specKwargs: (spec: Spec) => Readonly<Record<string, string>>;
	readonly sameSpec: (a: Spec, b: Spec) => boolean;
	readonly emptyMirror: Mirror;
	readonly fold: (mirror: Mirror, event: unknown) => Mirror;
}): Atom<void> {
	return atomEffect((get, set) => {
		const spec = get(specAtom);
		const entry = get(entryAtom);
		const stubKey = capability.shortId;

		// Guard every write against an entry that moved under us within
		// the same flush (cheap insurance; transitions are idempotent).
		type Entry = WatchEntry<Spec, Mirror>;
		const write = (fn: (prev: Entry, ctx: FxtrSessionUpdateCtx) => Entry | null) =>
			set(entryAtom, (prev, ctx) => (prev === entry && prev !== null ? fn(prev, ctx) : prev));

		if (spec === null) {
			// Nothing selected: dropping the inbox stub from state is the
			// unsubscribe (CloseCallbackOp → parker → source unsubscribe).
			if (entry !== null) set(entryAtom, (prev) => (prev === entry ? null : prev));
			return;
		}

		if (entry === null || entry.stubKey !== stubKey || !sameSpec(entry.spec, spec)) {
			// (Re)subscribe: a fresh inbox + the subscribe_to invoke in one
			// tick. Replacing the entry drops any old inbox stub in the
			// same write, so the stale subscription tears down server-side.
			set(entryAtom, (prev, ctx) => {
				if (prev !== entry) return prev;
				const inbox = ctx.allocateCallback<unknown>();
				const subscribe = ctx.allocateInvoke<null>(
					inbox.handle,
					"subscribe_to",
					[capability as ServiceRefStub<unknown>, method],
					specKwargs(spec),
				);
				return {
					stubKey,
					spec,
					inbox,
					subscribe,
					phase: "subscribing",
					message: null,
					mirror: emptyMirror,
				};
			});
			return;
		}

		if (entry.phase === "errored") return; // parked until retry / stub or spec change

		if (get(entry.inbox.connectionState) === "broken") {
			// The subscription broke (source death / delivery failure):
			// drop the entry; the next run resubscribes with a fresh inbox
			// for a fresh snapshot.
			set(entryAtom, (prev) => (prev === entry ? null : prev));
			return;
		}

		let phase: WatchPhase = entry.phase;
		if (entry.phase === "subscribing") {
			const result = get(entry.subscribe.result);
			if (result.status === "errored") {
				write((prev) => ({ ...prev, phase: "errored", message: result.message }));
				return;
			}
			if (result.status === "filled") phase = "running";
		}

		// Drain in any live phase (deliveries can race the subscribe
		// ack's fill), folding + re-anchoring in one write.
		const events = get(entry.inbox.events);
		if (events.length === 0 && phase === entry.phase) return;
		write((prev) => ({
			...prev,
			phase,
			mirror: events.reduce(fold, prev.mirror),
			inbox: events.length > 0 ? prev.inbox.sliced(events.length) : prev.inbox,
		}));
	});
}
