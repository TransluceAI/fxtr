/**
 * Per-key fallback overrides on a `Dispatcher`.
 *
 * Each `DispatchKey` declares its own fallback bundle (see
 * `fallbacks.ts`). Most consumers are happy with the key's fallbacks
 * as-is, but some dispatchers want to override one or more entries —
 * e.g. the dynamic dispatcher might want a `Loading` that surfaces
 * its in-flight dispatch state, or a consumer of the static
 * dispatcher might want to swap in a panel-specific `Error`
 * fallback.
 *
 * Overrides are constructed via the typed `overrideKeyFallbacks`
 * helper (which narrows the partial shape against the key's `Props`
 * + `Allowed`), passed as an array to the dispatcher's `overrides`
 * option, and internally consulted at `getEntry` time via a uuid-
 * keyed map.
 */

import type { KeyFallbacksFor } from "./fallbacks";
import type { DispatchKey, RefKind } from "./keys";

// Phantom brand: gives `KeyFallbackOverride` nominal identity at the
// type level so consumers can't construct one structurally — they
// must go through `overrideKeyFallbacks`.
declare const KEY_FALLBACK_OVERRIDE_BRAND: unique symbol;

/**
 * Opaque carrier for one per-key fallback override. Constructed via
 * `overrideKeyFallbacks`; consumed by `Dispatcher` factories via
 * their `overrides` option.
 */
export interface KeyFallbackOverride {
	readonly [KEY_FALLBACK_OVERRIDE_BRAND]: true;
	/** The key's wire-stable uuid. Read by the dispatcher. */
	readonly uuid: string;
	/**
	 * The fallback subset to override with. Stored as `unknown`
	 * because the dispatcher's lookup is type-erased; the typing
	 * contract is enforced at `overrideKeyFallbacks` construction
	 * time.
	 */
	readonly partial: unknown;
}

/**
 * Build a typed override for one specific dispatch key. The
 * `partial` argument's shape is narrowed against the key's
 * fallback bundle — e.g. an entity-only key whose `Props = NoProps`
 * accepts `{ Loading?, NoMatch?, Error?, Builtin? }`.
 *
 * Pass the result (or several) to a dispatcher factory's
 * `overrides?: ReadonlyArray<KeyFallbackOverride>` option.
 */
export function overrideKeyFallbacks<Props extends object, A extends RefKind>(
	key: DispatchKey<Props, A>,
	partial: Partial<KeyFallbacksFor<Props, A>>,
): KeyFallbackOverride {
	return {
		uuid: key.uuid,
		partial,
	} as KeyFallbackOverride;
}

/**
 * Build a uuid-keyed lookup map from a list of overrides. Used
 * internally by dispatcher factories to resolve per-key overrides
 * at `getEntry` time. Duplicate uuids: later entry wins (consistent
 * with the spirit of "last override applies").
 */
export function buildOverrideMap(
	overrides: ReadonlyArray<KeyFallbackOverride> | undefined,
): Map<string, Readonly<Record<string, unknown>>> {
	const m = new Map<string, Readonly<Record<string, unknown>>>();
	if (overrides === undefined) return m;
	for (const o of overrides) {
		m.set(o.uuid, o.partial as Readonly<Record<string, unknown>>);
	}
	return m;
}

/**
 * Merge a key's base fallbacks with the dispatcher's override for
 * that key (if any). The override's defined fields replace the
 * base's; absent fields fall through to the base. Returned object
 * is a fresh shallow merge — safe to mutate references in callers.
 *
 * Generic over `F` so the caller's concrete fallback-bundle type
 * survives the merge.
 */
export function resolveKeyFallbacks<F extends object>(
	baseFallbacks: F,
	overrideMap: Map<string, Readonly<Record<string, unknown>>>,
	keyUuid: string,
): F {
	const partial = overrideMap.get(keyUuid);
	if (partial === undefined) return baseFallbacks;
	return { ...baseFallbacks, ...partial } as F;
}
