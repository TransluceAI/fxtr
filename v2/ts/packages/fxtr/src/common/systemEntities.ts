/**
 * System entity definitions: FxtrBasicType and defineType().
 *
 * FxtrBasicType is the bootstrap entity — its type ID is the builtin !basic_type.
 * defineType() stores a FxtrBasicType entity to create content-addressed type IDs
 * for user-defined entity types.
 */

import { FxtrID } from "../core/entityCore.js";
import type { FxtrScope } from "../scope/scope.js";
import { GLOBAL_SCOPE } from "../scope/scope.js";
import * as bicodec from "./bicodec.js";
import type { InferEntity } from "./bicodec.js";

// --- UUID branded type ---

/** Type-level brand for namespace UUIDs. */
declare const UUID_BRAND: unique symbol;

/**
 * A phantom-branded string representing a namespace UUID.
 * Use the `UUID()` function to create values of this type.
 */
export type UUID = string & { readonly [UUID_BRAND]: true };

/**
 * Brand a string as a namespace UUID.
 *
 * If you need to create a new namespace, randomly generate a UUID (e.g. via
 * `crypto.randomUUID()` or `uuidgen`) and pass it here. Each namespace should
 * have its own unique, stable UUID that never changes once chosen.
 */
export function UUID(value: string): UUID {
	return value as UUID;
}

/**
 * Track a builtin ID in GLOBAL_SCOPE so bicodec can lift it into child scopes.
 * Mirrors Python's track_builtin_id().
 */
function trackBuiltinTypeId(name: string): FxtrID {
	const id = FxtrID.builtin(name);
	GLOBAL_SCOPE.trackBuiltin(id);
	return id;
}

// --- FxtrBasicType ---
// Type ID: !basic_type (builtin, tracked in GLOBAL_SCOPE)
const _FxtrBasicType = bicodec.entity(trackBuiltinTypeId("basic_type"), {
	namespace_uuid: bicodec.string(),
	type_name: bicodec.string(),
});
export interface FxtrBasicType extends InferEntity<typeof _FxtrBasicType> {}
export const FxtrBasicType: bicodec.EntityDefinition<FxtrBasicType> = _FxtrBasicType;

/**
 * Create a content-addressed type ID by storing a FxtrBasicType.
 * Mirrors Python's define_type().
 *
 * Since type entities are content-addressed, calling defineType with the same
 * namespace+name always produces the same ID (same CBOR → same SHA-256 hash).
 */
export async function defineType(
	namespaceUuid: UUID,
	typeName: string,
	scope: FxtrScope = GLOBAL_SCOPE,
): Promise<FxtrID> {
	const entity = FxtrBasicType.create({
		namespace_uuid: namespaceUuid,
		type_name: typeName,
	});
	const ref = await scope.store(entity);
	return ref.entityId;
}
