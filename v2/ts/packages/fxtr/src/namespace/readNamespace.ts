/**
 * FxtrReadNamespace: abstract interface for read-only fxtr namespaces.
 *
 * Port of Python's read_namespace.py. Provides retrieve, tryRetrieve,
 * and listPins. Code can be written against this interface and accept
 * either an HTTP-backed namespace or any future local implementation.
 *
 * Key difference from Python: the `type` parameter is replaced by an
 * explicit `loadCodec` parameter (no global type registry in TS).
 */

import type { FxtrID } from "../core/entityCore.js";
import type { Entity, EntityLoadCodec, EntityRef } from "../scope/entityRef.js";
import type { FxtrScope } from "../scope/scope.js";
import type { PromiseOrEager } from "../util/eager.js";

// ---------------------------------------------------------------------------
// Retrieve options
// ---------------------------------------------------------------------------

export interface RetrieveOpts {
	/**
	 * Scope to move the retrieved entity into.
	 * Defaults to the namespace's own internal scope.
	 */
	readonly intoScope?: FxtrScope;
	/**
	 * If true and intoScope is set, copy the entity into the target scope
	 * (full transitive copy). If false (default), lift the entity
	 * (it must already be accessible via a parent scope).
	 */
	readonly copyIntoScope?: boolean;
}

// ---------------------------------------------------------------------------
// FxtrReadNamespace interface
// ---------------------------------------------------------------------------

export interface FxtrReadNamespace {
	/** The namespace's internal scope. */
	readonly scope: FxtrScope;

	/**
	 * Retrieve an entity by pin name, returning its raw FxtrID.
	 * Throws if the pin is not found.
	 */
	retrieve(key: string, opts?: RetrieveOpts): PromiseOrEager<FxtrID>;

	/**
	 * Retrieve an entity by pin name with a load codec, returning an EntityRef.
	 * Throws if the pin is not found.
	 */
	retrieve<T extends Entity>(
		key: string,
		loadCodec: EntityLoadCodec<T>,
		opts?: RetrieveOpts,
	): PromiseOrEager<EntityRef<T>>;

	/**
	 * Retrieve an entity by pin name, returning null if not found.
	 */
	tryRetrieve(key: string, opts?: RetrieveOpts): PromiseOrEager<FxtrID | null>;

	/**
	 * Retrieve an entity by pin name with a load codec, returning null if not found.
	 */
	tryRetrieve<T extends Entity>(
		key: string,
		loadCodec: EntityLoadCodec<T>,
		opts?: RetrieveOpts,
	): PromiseOrEager<EntityRef<T> | null>;

	/**
	 * List all pins, optionally filtered by prefix.
	 */
	listPins(prefix?: string): PromiseOrEager<ReadonlyMap<string, FxtrID>>;
}
