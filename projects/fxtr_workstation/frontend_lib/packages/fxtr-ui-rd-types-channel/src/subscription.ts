/**
 * The channel subscription hooks — so channel renderers don't each
 * copy-paste the inbox + callback-target-invoke dance.
 *
 * All three ride the same mechanism: on mount, allocate a callback inbox
 * and a same-tick callback-target invoke `subscribe_to(reader, method)`
 * on the inbox's own handle (one wire mutation). The workstation resolves
 * the reader and calls its named subscribe method (`subscribe_full` /
 * `subscribe_state` / `subscribe_debug_subchannels`) with the inbox as the
 * subscriber — so no workstation backend change is needed; the frontend
 * just supplies the method name as `subscribe_to`'s second arg. The first
 * delivered event is the replay snapshot/seed; live deltas follow. Closing
 * the panel drops the inbox stub from userState, which closes the inbox and
 * unsubscribes server-side.
 *
 * The inbox + invoke stubs persist in the caller-provided storage slot
 * (versioned per feed), so a panel re-open within the same session resumes
 * the existing subscription instead of minting a new one.
 */

import type {
	CallbackInboxStub,
	FxtrSessionAtom,
	OpResultStub,
	ServiceRefStub,
} from "@fxtr-ui/core";
import { atomWithDefault } from "@fxtr-ui/state-util";
import { type Atom, atom, useAtomValue, useSetAtom } from "jotai";
import { useEffect, useMemo } from "react";
import {
	type ChannelStateEventWire,
	type ChannelUpdateWire,
	type ChannelViewState,
	type SubchannelOpenedWire,
	foldChannelUpdates,
	latestChannelState,
} from "./types.js";

/** Versioned persisted shape of one channel feed subscription. */
interface FeedSlot<E> {
	readonly kind: string;
	readonly inbox: CallbackInboxStub<E> | null;
	/** Anchors the `subscribe_to` callback-target invoke's slot (its return
	 * value is unused; the field keeps the allocated stub referenced from
	 * userState). */
	readonly subInvoke: OpResultStub<unknown> | null;
}

const EMPTY = atom<readonly never[]>([]);

/**
 * Subscribe to `source`'s named feed method, persisting the subscription
 * in `storageSlot`, and return the raw event list. The slot is owned by
 * this hook (stale / foreign shapes — including a different feed's `kind`
 * — are treated as empty and overwritten), so each feed needs its own
 * slot; a consumer using more than one carves a dedicated sub-slot per hook.
 */
function useChannelFeed<E>(
	source: ServiceRefStub,
	storageSlot: FxtrSessionAtom<unknown>,
	method: string,
	slotKind: string,
): readonly E[] {
	const slotAtom = useMemo(
		() =>
			atomWithDefault<FeedSlot<E>>(
				storageSlot as FxtrSessionAtom<FeedSlot<E> | null>,
				{ kind: slotKind, inbox: null, subInvoke: null },
				(s) => s?.kind === slotKind,
			),
		[storageSlot, slotKind],
	);
	const slot = useAtomValue(slotAtom);
	const setSlot = useSetAtom(slotAtom);

	// Subscribe exactly once per slot lifetime. The function-updater
	// re-checks `prev.inbox`, so StrictMode double-fires and remounts
	// against a persisted slot are no-ops.
	useEffect(() => {
		setSlot((prev, ctx) => {
			if (prev.inbox != null) return prev;
			const inbox = ctx.allocateCallback<E>();
			const subInvoke = ctx.allocateInvoke<unknown>(inbox.handle, "subscribe_to", [
				source as ServiceRefStub<unknown>,
				method,
			]);
			return { kind: slotKind, inbox, subInvoke };
		});
	}, [setSlot, source, method, slotKind]);

	const eventsAtom = (slot.inbox?.events ?? EMPTY) as Atom<readonly E[]>;
	return useAtomValue(eventsAtom);
}

/**
 * Subscribe to a channel reader's **full** event stream and return the
 * folded view state. `channel` is a channel-reader service ref.
 */
export function useChannelSubscription(
	channel: ServiceRefStub,
	storageSlot: FxtrSessionAtom<unknown>,
): ChannelViewState {
	const events = useChannelFeed<ChannelUpdateWire>(
		channel,
		storageSlot,
		"subscribe_full",
		"channel-full-sub/v1",
	);
	return useMemo(() => foldChannelUpdates(events), [events]);
}

/**
 * Subscribe to a channel reader's cheap replace-only **state** stream and
 * return the latest `ChannelStateEventWire` (or `null` before the first
 * event lands). Drives the live status pill / badge without pulling the
 * heavier event log.
 */
export function useChannelStateSubscription(
	channel: ServiceRefStub,
	storageSlot: FxtrSessionAtom<unknown>,
): ChannelStateEventWire | null {
	const events = useChannelFeed<ChannelStateEventWire>(
		channel,
		storageSlot,
		"subscribe_state",
		"channel-state-sub/v1",
	);
	return useMemo(() => latestChannelState(events), [events]);
}

/**
 * Subscribe to a channel reader's **debug subchannel** feed and return the
 * `SubchannelOpened` events received so far (one per spawned subchannel).
 * A schema-blind way to enumerate subchannels — see `GenericChannelCard`.
 */
export function useDebugSubchannels(
	channel: ServiceRefStub,
	storageSlot: FxtrSessionAtom<unknown>,
): readonly SubchannelOpenedWire[] {
	return useChannelFeed<SubchannelOpenedWire>(
		channel,
		storageSlot,
		"subscribe_debug_subchannels",
		"channel-debug-sub/v1",
	);
}
