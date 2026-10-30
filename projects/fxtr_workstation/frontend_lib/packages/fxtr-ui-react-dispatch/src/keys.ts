/**
 * Dispatch-key declaration.
 *
 * A `DispatchKey<Props, Allowed>` identifies one *rendering slot* —
 * e.g. "Card", "DetailView", "Inline" — that components in the
 * dispatch registry agree to fill. Two type parameters carry the
 * key's contract at the type level:
 *
 *   - `Props` — the extra props (beyond `stub`) that a dispatched
 *     component for this key accepts.
 *   - `Allowed extends RefKind` — which kinds of stub the key can
 *     be invoked with: `"entity"` only, `"service"` only, or
 *     `"entity" | "service"` (the default — both kinds allowed).
 *
 * The runtime payload is a frozen object carrying a *caller-
 * supplied UUID*, a human-readable description, and the key's
 * fallback components (`fallbacks.Loading` / `.NoMatch` / `.Error`
 * / optional `.Builtin`). The UUID is the wire-stable identifier:
 * dispatch entries in the dispatcher session's `userState` are
 * keyed by `(uuid, stub.shortId)`, so the same key declared at the
 * same module path across page reloads / session reattach lands on
 * the same userState slot regardless of in-memory state.
 *
 * **UUID convention.** Generate one literal UUID (`uuidgen` / any
 * RFC 4122 string) at declaration time and hardcode it. Two
 * dispatch keys with the same UUID will collide in the
 * dispatcher's userState — there is no runtime collision check
 * (HMR + duplicate-module-load false-positive concerns), so this
 * is on the consumer to keep distinct.
 *
 * **Fallbacks on the key.** Loading / NoMatch / Error are required
 * because the dispatcher must render *something* synchronously
 * (and on failure / no-match terminals) before any wire round-trip
 * resolves. Putting them on the key means a dispatcher doesn't
 * need a per-key registration to handle the slot — the dynamic
 * dispatcher in particular can resolve any key thrown at it,
 * including keys declared in renderer modules loaded at runtime.
 * Builtin is optional and entity-only (it only fires for
 * unloadable-tag `.` / `!` shortIds); when absent the dispatcher
 * falls back to NoMatch.
 */

import type {
	AnyKeyFallbacks,
	EntityKeyFallbacks,
	KeyFallbacksFor,
	ServiceKeyFallbacks,
} from "./fallbacks";

/**
 * Which kinds of ref-stub a dispatch key accepts.
 */
export type RefKind = "entity" | "service";

// Unique-symbol brand: phantom field giving each `DispatchKey<P,
// A>` instance nominal identity at the type level + making the
// `Props` / `Allowed` type parameters variance-meaningful.
// Without this, `DispatchKey<X, "entity">` and `DispatchKey<Y,
// "service">` would collapse to the same structural shape (just
// `{uuid, description, fallbacks}`).
declare const DISPATCH_KEY_BRAND: unique symbol;

/**
 * A rendering-slot identifier. Construct via one of the three
 * `declare*DispatchKey` factories below. Frozen at runtime;
 * carries phantom type info only at compile time.
 */
export interface DispatchKey<Props extends object, Allowed extends RefKind = RefKind> {
	/**
	 * Wire-stable identifier. The dispatcher session's userState
	 * keys entries by `(uuid, stub.shortId)`, so this must be
	 * unique per process. RFC 4122 UUIDs are the recommended
	 * choice; any non-colliding string works.
	 */
	readonly uuid: string;
	/**
	 * Human-readable description; used in error messages, debug
	 * UIs, and `displayName` of derived components. Not used for
	 * identity — `uuid` is the identity field.
	 */
	readonly description: string;
	/**
	 * Fallback components: rendered before / instead of the
	 * resolved dispatched component. The shape narrows based on
	 * `Allowed` — entity-only keys carry an optional `Builtin`
	 * shortcut for unloadable-tag shortIds; service-only keys
	 * never carry a `Builtin` field at the type level.
	 */
	readonly fallbacks: KeyFallbacksFor<Props, Allowed>;
	/**
	 * Phantom-only — never read at runtime. Encodes `Props` and
	 * `Allowed` so the type checker can distinguish keys with
	 * different contracts.
	 */
	readonly [DISPATCH_KEY_BRAND]: { props: Props; allowed: Allowed };
}

/**
 * Erased view of a `DispatchKey` — accepts any `Props` shape.
 *
 * Used as the parameter type for surfaces that take a key without
 * caring about its `Props` (the dispatcher's `getEntry`,
 * `<RenderDispatch>`'s generic constraint). The `any` is load-
 * bearing: `Props` participates in `fallbacks` contravariantly
 * (via `DispatchedComponent`'s `ComponentType` prop shape), so
 * the more natural `DispatchKey<object, RefKind>` rejects keys
 * declared with non-trivial `Props` like
 * `DispatchKey<FullPanelProps, RefKind>`. `any` here trades
 * type-safety at the erased boundary for a usable surface;
 * call-sites that care about `Props` recover it via
 * `ExtractDispatchProps`.
 *
 * Same role as `AnyDispatchEntry` (in `dispatcher.ts`) — an
 * intentionally-permissive erased alias for the heterogeneous
 * positions.
 */
// biome-ignore lint/suspicious/noExplicitAny: erased Props position; see docstring
export type AnyDispatchKey = DispatchKey<any, RefKind>;

/**
 * Extract the `Props` type parameter from a `DispatchKey`. Used
 * by `RenderDispatch`'s prop shape: a `RenderDispatch` rendering
 * with `as={someKey}` accepts the key's `Props` as additional
 * props.
 */
export type ExtractDispatchProps<K> = K extends DispatchKey<infer P, RefKind> ? P : never;

/**
 * Extract the `Allowed` type parameter from a `DispatchKey`.
 * Used to narrow `RenderDispatch`'s `stub` prop type at the
 * call-site (entity-only keys reject service stubs, etc.).
 */
export type ExtractDispatchAllowed<K> = K extends DispatchKey<object, infer A> ? A : never;

/**
 * Conditional type asserting that the key's `Allowed` parameter
 * `A` includes `"entity"`. If so, resolves to the key type
 * itself; otherwise resolves to `never`, surfacing as a compile
 * error when a consumer hands an entity-side API (registration
 * factory or `forEntity` lookup) a key whose `Allowed` is
 * `"service"` only.
 *
 * Tuple-wrapped to prevent the conditional from distributing
 * over `A` when `A` is a union (`"entity" | "service"`); without
 * the wrap, the check would distribute to `("entity" extends
 * "entity" ? K : never) | ("entity" extends "service" ? K :
 * never)`, which the type-checker simplifies to `K | never = K`
 * regardless of `A`'s contents — the opposite of what we want.
 */
export type RequireEntityAllowed<Props extends object, A extends RefKind> = ["entity"] extends [A]
	? DispatchKey<Props, A>
	: never;

/**
 * Mirror of `RequireEntityAllowed`, asserting that `A` includes
 * `"service"`.
 */
export type RequireServiceAllowed<Props extends object, A extends RefKind> = ["service"] extends [A]
	? DispatchKey<Props, A>
	: never;

/**
 * The "no extra props" Props type. Aliased to `object` — its
 * intersection with `{as, stub}` simplifies away (`{as, stub}`
 * already extends `object`), so dispatched components get just
 * `{stub}` and `RenderDispatch` doesn't trip on JSX-level
 * attributes like React's `key`.
 *
 * The non-obvious foot-gun this avoids: `Record<string, never>`
 * looks like the natural "empty record" type, but its
 * "every-string-key-maps-to-`never`" index signature collapses
 * intersections (`{as: K} & Record<string, never>` forces
 * `as: never`) and blocks JSX object-literal attributes. Use
 * `NoProps` (or `object`) for keys without extra props.
 */
export type NoProps = object;

function makeKey<Props extends object, Allowed extends RefKind>(
	uuid: string,
	description: string,
	fallbacks: KeyFallbacksFor<Props, Allowed>,
): DispatchKey<Props, Allowed> {
	// Frozen at runtime so accidental mutation can't desync the
	// uuid from the keyed userState entries.
	return Object.freeze({
		uuid,
		description,
		fallbacks,
	}) as unknown as DispatchKey<Props, Allowed>;
}

/**
 * Options accepted by `declareDispatchKey`. Carries the wire
 * identifier (`uuid`), the human description, and the fallback
 * components in the both-allowed shape (`AnyKeyFallbacks`).
 */
export interface DeclareDispatchKeyOptions<Props extends object> {
	readonly uuid: string;
	readonly description: string;
	readonly fallbacks: AnyKeyFallbacks<Props>;
}

/**
 * Declare a dispatch key that accepts both entity and service
 * refs. The fallback components must accept either stub kind at
 * runtime — `RenderDispatch` routes by
 * `stub instanceof EntityRefStub | ServiceRefStub` and the same
 * fallback gets rendered regardless. The optional `Builtin` field
 * is narrowed to `EntityRefStub` and only fires when the stub is
 * an entity stub with an unloadable-tag (`.` / `!`) shortId.
 *
 * `Props` defaults to `NoProps` (i.e. `object`) — a key without
 * extra props beyond `stub`. Override the default to pass
 * additional typed props through `RenderDispatch` to the
 * rendered component. Avoid `Record<string, never>`: see the
 * `NoProps` doc for the trap.
 */
export function declareDispatchKey<Props extends object = NoProps>(
	opts: DeclareDispatchKeyOptions<Props>,
): DispatchKey<Props, RefKind> {
	return makeKey<Props, RefKind>(
		opts.uuid,
		opts.description,
		opts.fallbacks as unknown as KeyFallbacksFor<Props, RefKind>,
	);
}

/**
 * Options accepted by `declareEntityDispatchKey`.
 */
export interface DeclareEntityDispatchKeyOptions<Props extends object> {
	readonly uuid: string;
	readonly description: string;
	readonly fallbacks: EntityKeyFallbacks<Props>;
}

/**
 * Declare a dispatch key that accepts entity refs only. Using
 * this key with a service stub at `RenderDispatch` is a compile-
 * time error. `Props` defaults to `NoProps`; see
 * `declareDispatchKey` for details.
 */
export function declareEntityDispatchKey<Props extends object = NoProps>(
	opts: DeclareEntityDispatchKeyOptions<Props>,
): DispatchKey<Props, "entity"> {
	return makeKey<Props, "entity">(
		opts.uuid,
		opts.description,
		opts.fallbacks as unknown as KeyFallbacksFor<Props, "entity">,
	);
}

/**
 * Options accepted by `declareServiceDispatchKey`.
 */
export interface DeclareServiceDispatchKeyOptions<Props extends object> {
	readonly uuid: string;
	readonly description: string;
	readonly fallbacks: ServiceKeyFallbacks<Props>;
}

/**
 * Declare a dispatch key that accepts service refs only. Using
 * this key with an entity stub at `RenderDispatch` is a compile-
 * time error. `Props` defaults to `NoProps`; see
 * `declareDispatchKey` for details.
 */
export function declareServiceDispatchKey<Props extends object = NoProps>(
	opts: DeclareServiceDispatchKeyOptions<Props>,
): DispatchKey<Props, "service"> {
	return makeKey<Props, "service">(
		opts.uuid,
		opts.description,
		opts.fallbacks as unknown as KeyFallbacksFor<Props, "service">,
	);
}
