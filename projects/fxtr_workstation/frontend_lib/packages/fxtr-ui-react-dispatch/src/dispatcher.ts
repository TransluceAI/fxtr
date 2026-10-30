/**
 * The Dispatcher abstraction: a session-bound, runtime-stateful
 * resolver of `(DispatchKey, RefStub) -> rendered component`.
 *
 * Concrete dispatcher implementations (`createStaticDispatcher`,
 * later `createDynamicDispatcher`) own:
 *
 *   - a `Session` (transferred at construction; the dispatcher
 *     detaches it on `dispatcher.detach()`);
 *   - the session-backed `userState` projection chain;
 *   - the per-`(stub, key)` entry cache;
 *   - any in-memory caches (e.g. compiled ESModules for the
 *     dynamic dispatcher);
 *   - the state-machine implementations that drive entry
 *     resolution.
 *
 * `<DispatcherProvider>` wraps a dispatcher into React context;
 * `<RenderDispatch>` calls `dispatcher.getEntry(key, stub)` and
 * subscribes to the returned entry's atoms.
 */

import type {
	EntityRefStub,
	FxtrSessionAtom,
	ServiceRefStub,
	Session,
	StateValue,
} from "@fxtr-ui/core";
import type { Atom } from "jotai";

import type { ReactElement } from "react";
import type { AnyDispatchKey } from "./keys";
import type { DispatchedComponent, ErrorFallbackComponent } from "./registry_types";

/**
 * The dispatcher's own lifecycle. Concrete dispatchers may
 * surface their internal sub-states (e.g. "dispatch service
 * resolving") collapsed into this five-value union, but the
 * surface itself is shared so consumers branch the same way
 * regardless of which dispatcher is behind the wheel.
 *
 * For the static dispatcher in particular this mirrors
 * `session.lifecycle` directly, except that `detach()` flips
 * to `"closed"` independently of the underlying session's state
 * — a closed-by-detach dispatcher reports `"closed"` even if
 * the session is still mid-drain.
 */
export type DispatcherStatus = "connecting" | "connected" | "stale" | "closed" | "unavailable";

/**
 * The output of `dispatcher.getEntry(key, stub)` — the per-`(stub,
 * key)` bundle that `<RenderDispatch>` subscribes to.
 *
 * All atoms here are read-only at this surface. The dispatcher's
 * internal state machine owns the corresponding writables.
 *
 *   - `componentAtom` — the currently-resolved component for this
 *     entry. Subscribing to it also drives the state machine
 *     forward (the implementation chains in an `atomEffect`-mounted
 *     subscription).
 *   - `sharedComponentStateAtom` — the per-`(stub, key)` writable
 *     session atom the dispatched component reads/writes via
 *     `useDispatchSharedAtom`. The dispatcher resets it to `null`
 *     whenever the resolved component changes value, so a freshly-
 *     swapped component sees its own declared default rather than
 *     the previous one's leftover state.
 *   - `dispatchInternalStateAtom` — read-only view of the state
 *     machine's tagged-union state. Type-erased here; consumers
 *     cast through `useDispatchProgressState<S>()` using the state
 *     union exported alongside the helper that built the entry.
 *   - `dispatchProgressMessageAtom` — read-only `"loading entity…"`-
 *     style status string written by the state machine. In-memory
 *     only (not persisted in session state).
 *   - `ErrorFallback` — component the per-entry error boundary
 *     renders when the dispatched component throws (including the
 *     synthetic throw a state machine uses to surface terminal
 *     errors).
 */
export interface DispatchEntry<Stub extends EntityRefStub | ServiceRefStub, Props extends object> {
	readonly id: string;
	readonly stub: Stub;
	readonly componentAtom: Atom<DispatchedComponent<Stub, Props>>;
	readonly sharedComponentStateAtom: FxtrSessionAtom<StateValue | null>;
	readonly dispatchInternalStateAtom: Atom<unknown>;
	readonly dispatchProgressMessageAtom: Atom<string>;
	readonly ErrorFallback: ErrorFallbackComponent<Stub, Props>;
}

/**
 * Erased view of a dispatch entry. The dispatcher's
 * per-`(keyUuid, shortId)` cache holds these; the discriminator
 * between entity and service entries is `stub instanceof
 * EntityRefStub` / `ServiceRefStub`.
 */
// biome-ignore lint/suspicious/noExplicitAny: cache holds entries with heterogeneous Stub / Props
export type AnyDispatchEntry = DispatchEntry<any, any>;

/**
 * The Dispatcher abstract surface. Concrete implementations
 * provide their own per-key resolution machinery; this surface is
 * what `<DispatcherProvider>` + `<RenderDispatch>` consume.
 *
 * Session ownership transfers at construction.
 * `dispatcher.detach()` detaches the session as part of cleanup;
 * consumers must not call `session.detach()` directly after
 * handing the session to a dispatcher.
 *
 * The `session` field is exposed for read-only inspection
 * (`sessionId`, `userState`, `lifecycle` — useful in debug
 * panels). Consumers should not call `session.detach()` /
 * `session.terminate()` on it — lifecycle is the dispatcher's
 * responsibility.
 */
export interface Dispatcher {
	readonly status: Atom<DispatcherStatus>;
	readonly session: Session;
	getEntry(key: AnyDispatchKey, stub: EntityRefStub | ServiceRefStub): AnyDispatchEntry;
	/**
	 * Idempotent async cleanup. Flips `status` to `"closed"`,
	 * drops the per-entry cache, then `await session.detach()`.
	 * After detach, `getEntry` throws `DispatcherClosedError`.
	 */
	detach(): Promise<void>;
	/**
	 * Optional debug-only surface: returns a React element that
	 * renders the dispatcher's internal state (registry / caches /
	 * mounted entries / etc.) for inspection in a debug panel.
	 *
	 * Method-style so `this` is bound when the element is built —
	 * concrete dispatchers close over their own atoms in the
	 * returned element without the consumer having to thread the
	 * dispatcher reference through.
	 *
	 * Optional: dispatchers that don't have an interesting debug
	 * surface (or where exposing one is out-of-scope) leave this
	 * undefined. Consumers (e.g. the workstation shell) branch on
	 * presence to decide whether to surface a debug affordance.
	 */
	debugView?(): ReactElement;
}

/**
 * Thrown by `dispatcher.getEntry` after `dispatcher.detach()` has
 * run. The dispatcher rejects new entry lookups once closed so
 * stale-dispatcher use surfaces as an explicit error rather than
 * minting stillborn entries against a torn-down session.
 */
export class DispatcherClosedError extends Error {
	constructor(message?: string) {
		super(
			message ??
				"Dispatcher.getEntry: dispatcher has been detached and is no longer accepting lookups.",
		);
		this.name = "DispatcherClosedError";
	}
}
