/**
 * The abstract `DispatchRegistry` surface — the lookup interface
 * `<DispatchProvider>` consumes. Two route-tables, one per
 * `RefKind`, keyed by `DispatchKey.uuid`.
 *
 * This module defines the contract only. Concrete builders
 * (e.g. `createDispatchRegistry`, which assembles a registry
 * from a list of `(key, entry)` pairs) live in their own
 * modules so the contract isn't coupled to one construction
 * style — future dynamic / remote registries can implement the
 * same interface with very different internals.
 *
 * Lookup methods are statically gated on the key's `Allowed`
 * parameter via `RequireEntityAllowed` / `RequireServiceAllowed`:
 * `forEntity(serviceOnlyKey)` is a compile error, and vice
 * versa. Keys with the default `Allowed = "entity" | "service"`
 * accept lookups in either direction. A miss (key has the
 * right `Allowed` but no entry has been registered for it)
 * returns `undefined`; the provider surfaces "no entry" via
 * its own error path.
 */

import type { RefKind, RequireEntityAllowed, RequireServiceAllowed } from "./keys";
import type { EntityDispatchEntry, ServiceDispatchEntry } from "./registry_types";

export interface DispatchRegistry {
	forEntity<Props extends object, A extends RefKind>(
		key: RequireEntityAllowed<Props, A>,
	): EntityDispatchEntry<Props, unknown> | undefined;
	forService<Props extends object, A extends RefKind>(
		key: RequireServiceAllowed<Props, A>,
	): ServiceDispatchEntry<Props, unknown> | undefined;
}
