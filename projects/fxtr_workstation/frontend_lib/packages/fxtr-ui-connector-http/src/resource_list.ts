/**
 * Discovery lists for the HTTP connector.
 *
 * Unlike the workstation's subscription-backed `ReactiveList` (snapshot
 * + live deltas), discovery here is a one-shot `GET /discovery` fetched
 * at construction, re-fetchable via the connector's `refresh()`. There
 * is nothing to push, so the lifecycle is just `loading → ready` (or
 * `loading → error` if the `/discovery` fetch fails — re-`refresh()` to
 * retry; records retain their last value), plus `closed` after
 * `connector.close()`. The interface keeps the `{status, records}` shape
 * so the connector's own roots panel can consume it like any reactive
 * list.
 *
 * These lists are **`HttpConnector`-specific** — after the PanelViewer
 * refactor (Part 4) there is no portable list/record abstraction; each
 * connector ships its own roots panel over its own lists.
 */

import { type Atom, atom, type getDefaultStore } from "jotai";

type JotaiStore = ReturnType<typeof getDefaultStore>;

/**
 * Lifecycle of a discovery list (no `stale` — there are no live deltas).
 * `error` means the last `/discovery` fetch failed; records keep their
 * previous value and a later `refresh()` can recover to `ready`.
 */
export type ReactiveListStatus = "loading" | "ready" | "error" | "closed";

/** The consumer-facing reactive list surface (mirrors the workstation's). */
export interface ReactiveList<T> {
	readonly status: Atom<ReactiveListStatus>;
	readonly records: Atom<readonly T[]>;
}

/**
 * A reactive list the connector drives: `publish` installs the records
 * + flips to `ready`; `setLoading` / `close` move the status. Backed by
 * the connector's jotai store.
 */
export class DiscoveryList<T> implements ReactiveList<T> {
	readonly #status = atom<ReactiveListStatus>("loading");
	readonly #records = atom<readonly T[]>([]);

	constructor(private readonly store: JotaiStore) {}

	get status(): Atom<ReactiveListStatus> {
		return this.#status;
	}
	get records(): Atom<readonly T[]> {
		return this.#records;
	}

	/** Install a fresh record set and flip to `ready`. */
	publish(records: readonly T[]): void {
		this.store.set(this.#records, records);
		this.store.set(this.#status, "ready");
	}

	/** Back to `loading` (e.g. at the start of a `refresh()`). */
	setLoading(): void {
		this.store.set(this.#status, "loading");
	}

	/** The `/discovery` fetch failed; records (if any) keep their value. */
	setError(): void {
		this.store.set(this.#status, "error");
	}

	/** Terminal — the connector closed. Records retain their last value. */
	close(): void {
		this.store.set(this.#status, "closed");
	}
}
