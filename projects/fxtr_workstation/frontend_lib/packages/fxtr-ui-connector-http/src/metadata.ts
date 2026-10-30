/**
 * Local resolution of the metadata ops the connector answers without a
 * server round trip.
 *
 * Service metadata is fully local (this module): the server bundles a
 * `service_type_id` (the type entity's `id_string`) on every descriptor
 * it introduces — discovery entries and descriptors returned from
 * invokes (design/01) — so the connector reads it straight off the
 * stub's descriptor, filling the same `ServiceMetadata` shape the
 * workstation emits (resolved Q-H). No invoke, no "unknown" sentinel.
 *
 * Entity metadata also resolves locally, but from the `/data` precomp
 * closure + id_string parsing, so it's built alongside the arena (the
 * connector, step F) where those inputs live — see design/03.
 */

import type { ServiceMetadata } from "@fxtr-ui/core";
import { type ServiceDescriptor, descriptorToWire } from "./descriptor";

/**
 * Thrown when a service descriptor lacks the `service_type_id`
 * annotation. The connector turns this into an errored metadata slot
 * (matching the workstation's "record disappeared" error style) rather
 * than fabricating a value — in practice every server-introduced
 * descriptor carries the annotation.
 */
export class MissingServiceTypeError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "MissingServiceTypeError";
	}
}

/**
 * Resolve a service's metadata from its descriptor. The
 * `type_id_string` is the descriptor's `service_type_id` (the type
 * entity's `id_string`) — note this is the *string* id, distinct from
 * the *loadable* type entity the dynamic dispatcher fetches via the
 * `$fxtr.type` invoke.
 *
 * @throws MissingServiceTypeError if the descriptor carries no
 *   `service_type_id`.
 */
export function buildServiceMetadata(descriptor: ServiceDescriptor): ServiceMetadata {
	if (descriptor.service_type_id === undefined) {
		throw new MissingServiceTypeError(
			`service descriptor has no service_type_id; cannot resolve service metadata: ${JSON.stringify(
				descriptorToWire(descriptor),
			)}`,
		);
	}
	return { type_id_string: descriptor.service_type_id };
}
