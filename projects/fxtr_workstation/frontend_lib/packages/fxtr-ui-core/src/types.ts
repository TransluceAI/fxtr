/**
 * Cross-connector value-shape types: three structured-value
 * families that differ in which stub kinds they admit. `ResultValue`
 * ⊆ `StateValue ∩ InvokeArgValue` structurally.
 *
 * - `ResultValue` — payloads decoded out of result / callback events.
 *   Pure data + entity/service refs. No client-only stubs
 *   (`OpResultStub` / `CallbackInboxStub` / `CallbackHandleStub`),
 *   since Python services can't produce them.
 * - `StateValue` — what the user can write into `Session.userState`.
 *   Adds `OpResultStub` and `CallbackInboxStub` (the receive-side
 *   stubs). Excludes `CallbackHandleStub` — handles are an invoke-arg
 *   notion only.
 * - `InvokeArgValue` — positional / keyword arguments to
 *   `allocateInvoke`. Adds `CallbackHandleStub` (send side). Excludes
 *   `OpResultStub` (caller must read the slot's value first) and
 *   `CallbackInboxStub` (consumer should pass `inbox.handle`).
 *
 * Stubs appear as class instances at leaves. Containers are
 * read-only by convention — mutations produce new container
 * instances so referential equality identifies "this subtree
 * changed."
 *
 * Connector-specific wire shapes (list-escape JSON, RFC 6902 JSON
 * Patch over that, etc.) live in the connector packages that own
 * them.
 */

import type {
	CallbackHandleStub,
	CallbackInboxStub,
	EntityRefStub,
	OpResultStub,
	ServiceRefStub,
} from "./stubs";

/**
 * Subset of structured values usable in result / callback event
 * payloads. Contains only the stub kinds Python services can
 * produce (entity / service refs); the three client-only stub kinds
 * (`OpResultStub`, `CallbackInboxStub`, `CallbackHandleStub`) are
 * absent. Structurally a subset of both `StateValue` and
 * `InvokeArgValue`.
 */
export type ResultValue =
	| null
	| boolean
	| number
	| string
	| Uint8Array
	| EntityRefStub<unknown>
	| ServiceRefStub<unknown>
	| readonly ResultValue[]
	| { readonly [key: string]: ResultValue };

/**
 * Structural form of session state. Adds the receive-side
 * client-only stubs (`OpResultStub`, `CallbackInboxStub`) on top of
 * `ResultValue`. This is the type of values the user can write into
 * `Session.userState`.
 */
export type StateValue =
	| null
	| boolean
	| number
	| string
	| Uint8Array
	| EntityRefStub<unknown>
	| ServiceRefStub<unknown>
	| OpResultStub<unknown>
	| CallbackInboxStub<unknown>
	| readonly StateValue[]
	| { readonly [key: string]: StateValue };

/**
 * Structural form of invoke arguments (positional or keyword,
 * including any depth of container nesting). Adds the send-side
 * `CallbackHandleStub` on top of `ResultValue`; excludes
 * `OpResultStub` and `CallbackInboxStub` (the user passes
 * `inbox.handle` rather than the inbox stub itself, and op-result
 * slots aren't meaningful as arguments — the caller must read the
 * value out first).
 */
export type InvokeArgValue =
	| null
	| boolean
	| number
	| string
	| Uint8Array
	| EntityRefStub<unknown>
	| ServiceRefStub<unknown>
	| CallbackHandleStub<unknown>
	| readonly InvokeArgValue[]
	| { readonly [key: string]: InvokeArgValue };

/** Plain JSON value. */
export type JsonValue =
	| null
	| boolean
	| number
	| string
	| readonly JsonValue[]
	| { readonly [key: string]: JsonValue };

/**
 * Type-only descriptor for a single method on a service protocol.
 * Used as the value type in a protocol record:
 *
 * ```ts
 * interface MyServiceProto {
 *   echo: Method<[StateValue], {}, unknown>;
 *   combine: Method<[StateValue], { name: string }, { value: unknown; name: string }>;
 *   fail: Method<[string], {}, never>;
 * }
 * ```
 *
 * Then `connector.serviceRef<MyServiceProto>("svc-a")` returns a
 * typed `ServiceRefStub<MyServiceProto>`, and
 * `session.allocateInvoke(svc, "echo", [...])` typechecks the
 * method name, args, kwargs, and return type against the
 * declaration.
 *
 * `Method` never exists at runtime — it's a purely structural
 * carrier for TS to extract types from. No runtime metadata, no
 * schema validation; this is the lightweight type-only layer (the
 * design notes' "OQ-α light"). A future stage may add a richer
 * runtime-metadata-producing builder; that builder can emit values
 * whose static type is `Method<…>` so the two layers compose.
 *
 * `Args` / `Kwargs` are constrained to `InvokeArgValue`-shaped
 * trees (the type the workstation invoke worker accepts); `Returns`
 * is constrained to `ResultValue` (no client-only stubs on the
 * return path — payloads from a Python service can only carry
 * entity / service refs in stub form).
 */
export interface Method<
	Args extends readonly InvokeArgValue[] = readonly InvokeArgValue[],
	Kwargs extends { readonly [key: string]: InvokeArgValue } = {
		readonly [key: string]: InvokeArgValue;
	},
	Returns extends ResultValue = ResultValue,
> {
	readonly args: Args;
	readonly kwargs: Kwargs;
	readonly returns: Returns;
}

/**
 * A service protocol type: a record mapping method names to
 * `Method<…>` descriptors. `ServiceRefStub<P>` for any `P
 * extends ServiceProto` enables typed `allocateInvoke` calls.
 */
export type ServiceProto = { readonly [methodName: string]: Method };

/**
 * Helpers used by `Session.allocateInvoke`'s typed signature to
 * extract args / kwargs / return-type out of a protocol's `Method`
 * declarations, with graceful fallbacks for untyped service refs
 * (`ServiceRefStub<unknown>`) and for protocol-shaped types whose
 * specific method isn't declared.
 *
 * `MethodNames<P>` is `string` when P is `unknown` (allowing any
 * method name through the typed signature for untyped service
 * refs), `Extract<keyof P, string>` otherwise.
 *
 * `MethodArgs` / `MethodKwargs` / `MethodReturns<P, M>` shape-match
 * on `P[M]` being a `Method<…>` and extract its parameters. If
 * `P[M]` isn't a `Method` (or P is `unknown`), they fall back to
 * loose `InvokeArgValue`-shaped (for args / kwargs) or
 * `ResultValue`-shaped (for returns) types so untyped calls still
 * compile.
 */
export type MethodNames<P> = unknown extends P ? string : Extract<keyof P, string>;
export type MethodArgs<P, M> = M extends keyof P
	? P[M] extends { readonly args: infer A extends readonly InvokeArgValue[] }
		? A
		: readonly InvokeArgValue[]
	: readonly InvokeArgValue[];
export type MethodKwargs<P, M> = M extends keyof P
	? P[M] extends {
			readonly kwargs: infer K extends { readonly [key: string]: InvokeArgValue };
		}
		? K
		: { readonly [key: string]: InvokeArgValue }
	: { readonly [key: string]: InvokeArgValue };
export type MethodReturns<P, M, RFallback> = M extends keyof P
	? P[M] extends { readonly returns: infer R }
		? R
		: RFallback
	: RFallback;
