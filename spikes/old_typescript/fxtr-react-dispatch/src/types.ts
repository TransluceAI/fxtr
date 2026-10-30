/**
 * Core dispatch types: DispatchKey, DispatchResolution, DispatchHandler.
 *
 * These are framework-agnostic (no React imports) except for the ComponentType
 * references in resolution types.
 */

import type { FxtrID, FxtrScope, PromiseOrEager } from "@fxtr/core";
import type { ComponentType } from "react";

// ---------------------------------------------------------------------------
// DispatchKey
// ---------------------------------------------------------------------------

declare const __DISPATCH_KEY_BRAND: unique symbol;

/**
 * A branded symbol identifying a dispatch interface (rendering slot).
 *
 * `Props` is the extra props type that dispatched components accept beyond
 * `entityId: FxtrID`.
 *
 * Children support: Props can include `children` for function-as-child or
 * render-prop patterns.
 */
export type DispatchKey<Props extends object = object> = symbol & {
	readonly [__DISPATCH_KEY_BRAND]: Props;
};

/**
 * Create a new dispatch key. Each call produces a unique symbol.
 *
 * ```ts
 * const MainView = declareDispatchKey<{ highlighted?: boolean }>("MainView");
 * const Figure = declareDispatchKey<{ caption: string }>("Figure");
 * ```
 */
export function declareDispatchKey<Props extends object = object>(
	description: string,
): DispatchKey<Props> {
	return Symbol(description) as DispatchKey<Props>;
}

/**
 * Extract the Props type from a DispatchKey.
 */
export type ExtractDispatchProps<K> = K extends DispatchKey<infer P> ? P : never;

// ---------------------------------------------------------------------------
// DispatchResolution
// ---------------------------------------------------------------------------

/**
 * The result of resolving a dispatch lookup: the component to render,
 * plus optional metadata for debugging or display.
 *
 * Generic over `Props` (the extra props beyond `entityId`) so the
 * Component's signature is fully typed through the dispatch chain.
 *
 * `Meta` is handler-defined (e.g. `{ label: string; source: string }` for
 * debug info). `<Render>` ignores it; the provider can expose it.
 */
export interface DispatchResolution<Props extends object = object, Meta = unknown> {
	Component: ComponentType<{ entityId: FxtrID } & Props>;
	metadata?: Meta | undefined;
}

// ---------------------------------------------------------------------------
// DispatchHandler
// ---------------------------------------------------------------------------

/**
 * Handler interface consumed by `<DispatchProvider>`.
 *
 * Implementors look up a (key, typeId) pair to find a DispatchResolution.
 * The simplest implementation is `createDispatchRegistry(...)` from registry.ts,
 * but custom handlers can do anything (remote lookup, feature flags, etc).
 *
 * The `lookup` method is generic over Props — TypeScript infers it from
 * the DispatchKey, so the returned Component is typed to match the key.
 */
export interface DispatchHandler<Meta = unknown> {
	/**
	 * Look up a renderer for the given dispatch key and entity type.
	 *
	 * Returns `Eager` when the result can be determined synchronously
	 * (e.g. from a static registry with cached codec checks), allowing
	 * `<Render>` to skip Suspense entirely.
	 *
	 * Must always return a resolution (use a fallback internally if no
	 * specific handler matches). Throwing is reserved for hard errors.
	 *
	 * @param key    The dispatch key (which rendering slot)
	 * @param typeId The entity's $fxtr.type ID
	 * @param scope  A scope for checking codec support (via cachedSupportsEntityType)
	 */
	lookup<Props extends object>(
		key: DispatchKey<Props>,
		typeId: FxtrID,
		scope: FxtrScope,
	): PromiseOrEager<DispatchResolution<Props, Meta>>;

	/**
	 * Get a loading placeholder component for a dispatch key.
	 *
	 * If this returns a component, `<Render>` shows it during async lookup
	 * instead of suspending. If absent or returns undefined, `<Render>` suspends.
	 *
	 * The loading component receives `entityId` plus the key's Props, so it
	 * can render a shaped placeholder appropriate for the rendering slot.
	 */
	getLoadingFallback?<Props extends object>(
		key: DispatchKey<Props>,
	): ComponentType<{ entityId: FxtrID } & Props> | undefined;
}
