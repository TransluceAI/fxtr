/**
 * A simple `DispatchRegistry` builder that takes a list of
 * `(DispatchKey, DispatchKeyRegistryEntry)` pairs and produces
 * a registry whose lookup walks those pairs by UUID.
 *
 * This is one specific way to construct a `DispatchRegistry`,
 * not part of the abstract surface. Future dynamic / remote
 * dispatch will produce `DispatchRegistry` instances by other
 * means (e.g. wire-loaded handlers, on-demand compilation)
 * without going through this builder.
 *
 * Each `(key, entry)` pair is built via `entityEntry(key,
 * entry)` / `serviceEntry(key, entry)`, which use a tuple-
 * wrapped conditional type to statically reject mismatched
 * ref-kinds (an entity-only key paired with a service entry
 * fails to typecheck and vice versa).
 *
 * The pair objects themselves are exposed as
 * `EntityDispatchRegistryForKey` / `ServiceDispatchRegistryForKey`
 * — "the (entity|service) part of the dispatch registry,
 * scoped to a single key" — but consumers typically only use
 * the factories + `createDispatchRegistry`, not the pair types
 * directly.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { DispatchKey, RefKind, RequireEntityAllowed, RequireServiceAllowed } from "./keys";
import type { DispatchRegistry } from "./registry";
import type {
	DispatchKeyRegistryEntry,
	EntityDispatchEntry,
	ServiceDispatchEntry,
} from "./registry_types";

// ---------------------------------------------------------------------------
// (key, entry) pair shapes
// ---------------------------------------------------------------------------

/**
 * The slice of a `DispatchRegistry` covering one entity-kind
 * dispatch key: the key itself paired with the
 * `DispatchKeyRegistryEntry` configuring its resolution. Built
 * via `entityEntry(key, entry)`; consumed by
 * `createDispatchRegistry`.
 *
 * Stored Props-erased to `object` and S-erased to `unknown`.
 * The factory enforces the (key.Props == entry.Props) constraint
 * statically before erasing.
 */
export interface EntityDispatchRegistryForKey {
	readonly key: DispatchKey<object, RefKind>;
	readonly entry: EntityDispatchEntry<object, unknown>;
}

/**
 * Service-kind mirror of `EntityDispatchRegistryForKey`. Built
 * via `serviceEntry(key, entry)`.
 */
export interface ServiceDispatchRegistryForKey {
	readonly key: DispatchKey<object, RefKind>;
	readonly entry: ServiceDispatchEntry<object, unknown>;
}

// ---------------------------------------------------------------------------
// Factories with static ref-kind narrowing
// ---------------------------------------------------------------------------

/**
 * Pair an entity-side `DispatchKeyRegistryEntry` with a
 * `DispatchKey` whose `Allowed` includes `"entity"`.
 *
 * Type-level checks:
 *   - `key`'s `Allowed` must include `"entity"`; a service-only
 *     key here is a compile error.
 *   - `entry`'s `Props` must match `key`'s `Props`.
 */
export function entityEntry<Props extends object, A extends RefKind, S>(
	key: RequireEntityAllowed<Props, A>,
	entry: DispatchKeyRegistryEntry<EntityRefStub, Props, S>,
): EntityDispatchRegistryForKey {
	// The narrow `RequireEntityAllowed<...>` parameter ensures
	// we get a real `DispatchKey` here at the type level. Cast
	// to the loose runtime form for heterogeneous storage.
	const k = key as DispatchKey<Props, A>;
	return {
		key: k as unknown as DispatchKey<object, RefKind>,
		entry: entry as unknown as EntityDispatchEntry<object, unknown>,
	};
}

/**
 * Pair a service-side `DispatchKeyRegistryEntry` with a
 * `DispatchKey` whose `Allowed` includes `"service"`. Mirror of
 * `entityEntry`.
 */
export function serviceEntry<Props extends object, A extends RefKind, S>(
	key: RequireServiceAllowed<Props, A>,
	entry: DispatchKeyRegistryEntry<ServiceRefStub, Props, S>,
): ServiceDispatchRegistryForKey {
	const k = key as DispatchKey<Props, A>;
	return {
		key: k as unknown as DispatchKey<object, RefKind>,
		entry: entry as unknown as ServiceDispatchEntry<object, unknown>,
	};
}

// ---------------------------------------------------------------------------
// Registry assembly
// ---------------------------------------------------------------------------

/**
 * Input to `createDispatchRegistry`. Two parallel arrays of
 * already-built `(key, entry)` pairs.
 */
export interface CreateDispatchRegistryInput {
	readonly entityEntries: ReadonlyArray<EntityDispatchRegistryForKey>;
	readonly serviceEntries: ReadonlyArray<ServiceDispatchRegistryForKey>;
}

/**
 * Thrown when `createDispatchRegistry` is given two pairs
 * targeting the same `DispatchKey.uuid` within the same table.
 * The same key with both an entity entry and a service entry
 * (one in each table) is the intended use case and not an
 * error.
 */
export class DuplicateDispatchKeyError extends Error {
	readonly table: "entity" | "service";
	readonly key: DispatchKey<object, RefKind>;
	constructor(table: "entity" | "service", key: DispatchKey<object, RefKind>) {
		super(
			`createDispatchRegistry: duplicate ${table} entry for DispatchKey "${key.description}" (uuid=${key.uuid}). Each key may appear at most once in the same table.`,
		);
		this.name = "DuplicateDispatchKeyError";
		this.table = table;
		this.key = key;
	}
}

/**
 * Build a `DispatchRegistry` from a list of `(key, entry)`
 * pairs. Duplicates within a table throw
 * `DuplicateDispatchKeyError`; the same key in both tables is
 * fine.
 */
export function createDispatchRegistry(input: CreateDispatchRegistryInput): DispatchRegistry {
	const entityTable = new Map<string, EntityDispatchRegistryForKey>();
	for (const pair of input.entityEntries) {
		if (entityTable.has(pair.key.uuid)) {
			throw new DuplicateDispatchKeyError("entity", pair.key);
		}
		entityTable.set(pair.key.uuid, pair);
	}

	const serviceTable = new Map<string, ServiceDispatchRegistryForKey>();
	for (const pair of input.serviceEntries) {
		if (serviceTable.has(pair.key.uuid)) {
			throw new DuplicateDispatchKeyError("service", pair.key);
		}
		serviceTable.set(pair.key.uuid, pair);
	}

	return {
		forEntity<Props extends object, A extends RefKind>(
			key: RequireEntityAllowed<Props, A>,
		): EntityDispatchEntry<Props, unknown> | undefined {
			// The `RequireEntityAllowed<Props, A>` parameter type
			// means `key` is either `DispatchKey<Props, A>` (when
			// `A` includes `"entity"`) or `never` (when it doesn't,
			// in which case the call is a compile error and we
			// never get here). Cast back to the concrete shape for
			// the runtime read.
			const k = key as DispatchKey<Props, A>;
			const pair = entityTable.get(k.uuid);
			// Cast: registration enforced (key.Props == entry.Props)
			// at the type level via `entityEntry`'s signature; we
			// recover that here.
			return pair?.entry as EntityDispatchEntry<Props, unknown> | undefined;
		},
		forService<Props extends object, A extends RefKind>(
			key: RequireServiceAllowed<Props, A>,
		): ServiceDispatchEntry<Props, unknown> | undefined {
			const k = key as DispatchKey<Props, A>;
			const pair = serviceTable.get(k.uuid);
			return pair?.entry as ServiceDispatchEntry<Props, unknown> | undefined;
		},
	};
}
