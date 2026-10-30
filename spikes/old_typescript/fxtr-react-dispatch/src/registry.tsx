/**
 * Registry builder: convenience for assembling a DispatchHandler from
 * a list of (key, codec, Component) entries.
 *
 * Each entry declares:
 * - which dispatch key it serves (e.g. MainView, Figure)
 * - which entity codec it supports (determines which $fxtr.type IDs match)
 * - a React component that receives an EntityRef<T> bound to that codec
 *
 * The builder:
 * - Groups entries by dispatch key
 * - For a given (key, typeId), tries codecs in declaration order
 * - Caches the first match (via cachedSupportsEntityType)
 * - Wraps each Component so it receives EntityRef<T> instead of raw entityId
 * - Falls back to a configurable fallback component if no codec matches
 */

import {
	Eager,
	type Entity,
	type EntityLoadCodec,
	EntityRef,
	type FxtrID,
	type FxtrScope,
	type PromiseOrEager,
	cachedSupportsEntityType,
} from "@fxtr/core";
import type { ComponentType } from "react";
import type { DispatchHandler, DispatchKey, DispatchResolution } from "./types.js";

// ---------------------------------------------------------------------------
// Registry entry type
// ---------------------------------------------------------------------------

/**
 * A single dispatch registration: "for this key, if the entity's type
 * matches this codec, render with this Component."
 *
 * The Component receives `entityRef: EntityRef<T>` (bound to the codec)
 * plus whatever extra Props the dispatch key requires.
 */
export interface RegistryEntry<
	// biome-ignore lint/suspicious/noExplicitAny: used as constraint, actual types inferred per entry
	Props extends object = any,
	T extends Entity = Entity,
	Meta = unknown,
> {
	key: DispatchKey<Props>;
	codec: EntityLoadCodec<T>;
	Component: ComponentType<{ entityRef: EntityRef<T> } & Props>;
	metadata?: Meta | undefined;
}

/**
 * Helper to create a type-safe registry entry. Infers T from the codec
 * and Props from the key, ensuring the Component signature matches.
 */
export function entry<Props extends object, T extends Entity, Meta = unknown>(
	key: DispatchKey<Props>,
	codec: EntityLoadCodec<T>,
	Component: ComponentType<{ entityRef: EntityRef<T> } & Props>,
	metadata?: Meta,
): RegistryEntry<Props, T, Meta> {
	return { key, codec, Component, metadata };
}

// ---------------------------------------------------------------------------
// Registry options
// ---------------------------------------------------------------------------

export interface RegistryOptions<Meta = unknown> {
	/**
	 * Fallback component rendered when no registered codec matches the
	 * entity's type. Receives just `entityId`.
	 *
	 * If not provided, resolve() throws when no match is found.
	 */
	fallback?: ComponentType<{ entityId: FxtrID }>;

	/**
	 * Optional loading components keyed by dispatch key symbol.
	 * Used by getLoadingFallback() to provide sync placeholders.
	 * The component receives entityId plus the key's Props.
	 */
	// biome-ignore lint/suspicious/noExplicitAny: map holds components for heterogeneous keys
	loading?: Map<symbol, ComponentType<{ entityId: FxtrID } & any>>;

	/**
	 * Metadata attached to fallback resolutions.
	 */
	fallbackMetadata?: Meta;
}

// ---------------------------------------------------------------------------
// createDispatchRegistry
// ---------------------------------------------------------------------------

/**
 * Build a DispatchHandler from a list of registry entries.
 *
 * ```ts
 * const handler = createDispatchRegistry(
 *   [
 *     entry(MainView, Article, ArticleMainView),
 *     entry(MainView, Author, AuthorMainView),
 *     entry(Figure, Article, ArticleFigure, { label: "article-figure" }),
 *   ],
 *   { fallback: GenericEntityView },
 * );
 * ```
 */
export function createDispatchRegistry<Meta = unknown>(
	// biome-ignore lint/suspicious/noExplicitAny: entries have heterogeneous entity/props types
	entries: RegistryEntry<any, any, Meta>[],
	options?: RegistryOptions<Meta>,
): DispatchHandler<Meta> {
	// Group entries by dispatch key for efficient lookup.
	const byKey = new Map<symbol, RegistryEntry<object, Entity, Meta>[]>();
	for (const e of entries) {
		let group = byKey.get(e.key);
		if (!group) {
			group = [];
			byKey.set(e.key, group);
		}
		group.push(e);
	}

	return {
		lookup<Props extends object>(
			key: DispatchKey<Props>,
			typeId: FxtrID,
			scope: FxtrScope,
		): PromiseOrEager<DispatchResolution<Props, Meta>> {
			const group = byKey.get(key);
			if (!group || group.length === 0) {
				// Cast: fallbackOrThrow returns object-erased resolution;
				// safe because fallback components accept any props via spread.
				return fallbackOrThrow(key, typeId, options) as PromiseOrEager<
					DispatchResolution<Props, Meta>
				>;
			}

			// Try codecs in declaration order via sequential .then() chaining.
			// Returns Eager when all cachedSupportsEntityType calls return Eager
			// (i.e. all codec checks are cached).
			// Cast: tryEntry works with object-erased Props; safe because
			// entries are grouped by key which preserves the key→Props association.
			return tryEntry(group, 0, key, typeId, scope, options) as PromiseOrEager<
				DispatchResolution<Props, Meta>
			>;
		},

		getLoadingFallback<Props extends object>(key: DispatchKey<Props>) {
			// Cast: the loading map stores components for heterogeneous keys;
			// the caller registered this component for this specific key.
			return options?.loading?.get(key) as ComponentType<{ entityId: FxtrID } & Props> | undefined;
		},
	};
}

// ---------------------------------------------------------------------------
// Internal: sequential codec matching via .then() chaining
// ---------------------------------------------------------------------------

/**
 * Try entries[index] and recurse. Returns Eager when all
 * cachedSupportsEntityType calls in the chain return Eager.
 */
function tryEntry<Meta>(
	entries: RegistryEntry<object, Entity, Meta>[],
	index: number,
	key: DispatchKey<object>,
	typeId: FxtrID,
	scope: FxtrScope,
	options: RegistryOptions<Meta> | undefined,
): PromiseOrEager<DispatchResolution<object, Meta>> {
	if (index >= entries.length) {
		return fallbackOrThrow(key, typeId, options);
	}

	// biome-ignore lint/style/noNonNullAssertion: bounds checked above
	const e = entries[index]!;
	const supported = cachedSupportsEntityType(e.codec, typeId, scope);

	return supported.then((matches) => {
		if (matches) {
			return Eager.resolve({
				Component: wrapWithEntityRef(e.Component, e.codec),
				metadata: e.metadata,
			} as DispatchResolution<object, Meta>);
		}
		return tryEntry(entries, index + 1, key, typeId, scope, options);
	});
}

function fallbackOrThrow<Meta>(
	key: DispatchKey<object>,
	typeId: FxtrID,
	options: RegistryOptions<Meta> | undefined,
): PromiseOrEager<DispatchResolution<object, Meta>> {
	if (options?.fallback) {
		return Eager.resolve({
			Component: options.fallback,
			metadata: options.fallbackMetadata,
		} as DispatchResolution<object, Meta>);
	}
	return Eager.reject(
		new Error(
			`No dispatch handler registered for key ${String(key)} and type ${typeId.idString()}`,
		),
	);
}

// ---------------------------------------------------------------------------
// Internal: wrap a Component<{ entityRef, ...props }> into
//           Component<{ entityId, ...props }>
// ---------------------------------------------------------------------------

function wrapWithEntityRef<T extends Entity>(
	// biome-ignore lint/suspicious/noExplicitAny: wrapper passes through arbitrary extra props
	Inner: ComponentType<{ entityRef: EntityRef<T> } & Record<string, any>>,
	codec: EntityLoadCodec<T>,
	// biome-ignore lint/suspicious/noExplicitAny: outer component accepts entityId + arbitrary extra props
): ComponentType<{ entityId: FxtrID } & Record<string, any>> {
	function Wrapped({ entityId, ...rest }: { entityId: FxtrID }) {
		const entityRef = new EntityRef(entityId, codec);
		return <Inner entityRef={entityRef} {...rest} />;
	}
	Wrapped.displayName = `Dispatch(${(Inner as { displayName?: string }).displayName ?? Inner.name ?? "Anonymous"})`;
	return Wrapped;
}
