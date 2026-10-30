/**
 * Per-`DispatchKey` fallback components.
 *
 * Every `DispatchKey` declaration carries the components a dispatcher
 * renders while resolution is in flight (Loading), when resolution
 * completes without a renderer (NoMatch), when the resolved
 * component throws during render (Error), and — for entity-allowed
 * keys — when the stub's shortId is a builtin / anonymous tag
 * (`.` / `!`) that doesn't load (Builtin, optional).
 *
 * These live on the key rather than per-dispatcher so the dispatcher
 * doesn't need to know the universe of keys it may handle in
 * advance — particularly useful for the dynamic dispatcher, where
 * keys may be declared in renderer modules loaded at runtime. The
 * key's author is also the most natural place to write fallback UI
 * that matches the key's `Props` shape.
 *
 * See `stage10aside1_dispatch_key_fallback_refactor.md` for the
 * motivation.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";

import type { RefKind } from "./keys";
import type { DispatchedComponent, ErrorFallbackComponent } from "./registry_types";

/**
 * Fallback bundle for an entity-only dispatch key. `Builtin` is
 * optional and consulted only when the entity stub's `shortId`
 * begins with `.` (anonymous) or `!` (builtin) — entities the
 * workstation can't load. Missing `Builtin` falls through to
 * `NoMatch`.
 */
export interface EntityKeyFallbacks<Props extends object> {
	readonly Loading: DispatchedComponent<EntityRefStub, Props>;
	readonly NoMatch: DispatchedComponent<EntityRefStub, Props>;
	readonly Error: ErrorFallbackComponent<EntityRefStub, Props>;
	readonly Builtin?: DispatchedComponent<EntityRefStub, Props>;
}

/**
 * Fallback bundle for a service-only dispatch key. No `Builtin`
 * field — services have no analogue of entity unloadable-tag
 * shortIds. Including the field anyway is a type error on
 * `declareServiceDispatchKey`.
 */
export interface ServiceKeyFallbacks<Props extends object> {
	readonly Loading: DispatchedComponent<ServiceRefStub, Props>;
	readonly NoMatch: DispatchedComponent<ServiceRefStub, Props>;
	readonly Error: ErrorFallbackComponent<ServiceRefStub, Props>;
}

/**
 * Fallback bundle for a dispatch key that accepts both entity and
 * service stubs. `Loading` / `NoMatch` / `Error` see either stub
 * kind at render time; `Builtin` (optional) is narrowed to
 * `EntityRefStub` because it only fires for entity stubs with
 * unloadable-tag shortIds.
 */
export interface AnyKeyFallbacks<Props extends object> {
	readonly Loading: DispatchedComponent<EntityRefStub | ServiceRefStub, Props>;
	readonly NoMatch: DispatchedComponent<EntityRefStub | ServiceRefStub, Props>;
	readonly Error: ErrorFallbackComponent<EntityRefStub | ServiceRefStub, Props>;
	readonly Builtin?: DispatchedComponent<EntityRefStub, Props>;
}

/**
 * Pick the fallback-bundle shape for a `DispatchKey<Props, Allowed>`.
 * Tuple-wrapped to suppress union distribution: a both-allowed key
 * (`Allowed = "entity" | "service"`) takes the `AnyKeyFallbacks`
 * branch rather than distributing the conditional over each member
 * and collapsing.
 */
export type KeyFallbacksFor<Props extends object, A extends RefKind> = [A] extends ["entity"]
	? EntityKeyFallbacks<Props>
	: [A] extends ["service"]
		? ServiceKeyFallbacks<Props>
		: AnyKeyFallbacks<Props>;
