/**
 * Service descriptors — the self-describing service identity carried on
 * the stateless HTTP/ws wire (the TS twin of the Python
 * `service_codec.ServiceDescriptor`). In resolved mode a service ref
 * travels as `["service", <descriptor>]`; the connector interns each
 * descriptor under a client-minted shortId.
 *
 *   - `{kind: "http", url, method}`           — a REST service
 *   - `{kind: "ws",   url, name}`             — a ws service (multiplexed
 *                                               on `url`, addressed by
 *                                               `name`)
 *   - `{kind: "transient", id}`               — a connection-scoped
 *                                               service handed out during
 *                                               a ws exchange (no `url`)
 *
 * Any descriptor may carry a `service_type_id` annotation (the type
 * entity's `id_string`). It is a **non-identity** annotation — descriptor
 * canonicalization for shortId hashing excludes it (design/01 Q-J), so
 * two descriptors that differ only in `service_type_id` intern to the
 * same shortId.
 */

import type { WireValue } from "@fxtr-ui/core";

/** The descriptor `kind` discriminant. */
export type ServiceDescriptorKind = "http" | "ws" | "transient";

/** A self-describing service reference (see module docstring). */
export interface ServiceDescriptor {
	readonly kind: ServiceDescriptorKind;
	/** http/ws: the route / ws-endpoint URL (absolute or connector-relative). */
	readonly url?: string;
	/** http: `"GET"` | `"POST"`. */
	readonly method?: string;
	/** ws: the routing name the client sends as `service`. */
	readonly name?: string;
	/** transient: the connection-scoped id. */
	readonly id?: string;
	/** Non-identity annotation: the service's type entity `id_string`. */
	readonly service_type_id?: string;
}

const _KINDS: ReadonlySet<string> = new Set(["http", "ws", "transient"]);

/** Thrown when a wire value isn't a well-formed service descriptor. */
export class ServiceDescriptorError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "ServiceDescriptorError";
	}
}

/**
 * Validate + narrow a raw wire payload (the `value[1]` of a
 * `["service", <descriptor>]` tag) into a `ServiceDescriptor`. Throws
 * `ServiceDescriptorError` on a missing/invalid `kind` or a non-string
 * field.
 */
export function descriptorFromWire(payload: WireValue): ServiceDescriptor {
	if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
		throw new ServiceDescriptorError(
			`service descriptor must be an object, got ${Array.isArray(payload) ? "array" : typeof payload}`,
		);
	}
	const obj = payload as { readonly [key: string]: WireValue };
	const kind = obj.kind;
	if (typeof kind !== "string" || !_KINDS.has(kind)) {
		throw new ServiceDescriptorError(
			`service descriptor: invalid/missing 'kind' (expected http | ws | transient): ${JSON.stringify(payload)}`,
		);
	}
	// Build with only present fields — `exactOptionalPropertyTypes`
	// forbids assigning `undefined` to the optional descriptor fields.
	const desc: {
		kind: ServiceDescriptorKind;
		url?: string;
		method?: string;
		name?: string;
		id?: string;
		service_type_id?: string;
	} = { kind: kind as ServiceDescriptorKind };
	for (const field of ["url", "method", "name", "id", "service_type_id"] as const) {
		const v = obj[field];
		if (v === undefined) continue;
		if (typeof v !== "string") {
			throw new ServiceDescriptorError(`service descriptor: '${field}' must be a string`);
		}
		desc[field] = v;
	}
	return desc;
}

/**
 * Encode a descriptor to its wire object form, omitting unset fields
 * (mirrors the Python `ServiceDescriptor.to_wire`). This is the payload
 * the resolved-mode codec puts under the `["service", ...]` tag.
 */
export function descriptorToWire(desc: ServiceDescriptor): { [key: string]: WireValue } {
	const out: { [key: string]: WireValue } = { kind: desc.kind };
	for (const field of ["url", "method", "name", "id", "service_type_id"] as const) {
		const v = desc[field];
		if (v !== undefined) out[field] = v;
	}
	return out;
}

/**
 * Resolve a possibly-relative URL to absolute against `baseUrl`. Used
 * only for canonicalization (hash identity), so an unparseable URL
 * falls back to its raw string rather than throwing — two equal raw
 * URLs still canonicalize identically.
 */
function resolveUrl(url: string, baseUrl: string): string {
	try {
		return new URL(url, baseUrl).href;
	} catch {
		return url;
	}
}

/**
 * Canonical identity key for a descriptor, for shortId hashing
 * (design/01 Q-J): resolve `url` to absolute, **drop** the
 * `service_type_id` annotation, sort keys, JSON-stringify. Two
 * descriptors that differ only in `service_type_id` (or in relative-vs-
 * absolute URL spelling) produce the same key — and therefore the same
 * shortId.
 */
export function canonicalizeDescriptor(desc: ServiceDescriptor, baseUrl: string): string {
	const canon: { [key: string]: string } = {};
	// Insert in sorted key order so JSON.stringify is order-stable.
	for (const field of ["id", "kind", "method", "name", "url"] as const) {
		const v = desc[field];
		if (v === undefined) continue;
		canon[field] = field === "url" ? resolveUrl(v, baseUrl) : v;
	}
	return JSON.stringify(canon);
}
