/**
 * Builder for a `DispatchKeyRegistryEntry` that resolves an
 * entity dispatch by walking a fixed `id_string → component`
 * lookup map. The state machine:
 *
 *   1. `init` → if `stub.shortId` carries an unloadable-tag
 *      prefix (`.` / anonymous, `!` / builtin), transition to
 *      terminal `resolved-builtin` without a load. Otherwise
 *      allocate a load on `stub`; transition to `loading-entity`.
 *   2. `loading-entity` → wait for the load's `result` atom.
 *      On filled, extract the `$fxtr.type` field of the loaded
 *      value (expected to be an `EntityRefStub` pointing at
 *      the entity's type record); allocate a metadata query on
 *      that ref; transition to `loading-type-metadata`. On
 *      errored, transition to `errored`.
 *   3. `loading-type-metadata` → wait for the metadata
 *      `result` atom. On filled, read `metadata.id_string`,
 *      transition to terminal `resolved`. On errored,
 *      transition to terminal `errored`.
 *   4. Terminal phases: each effect run re-checks the
 *      `componentAtom`. If it doesn't match what the terminal
 *      state implies (`lookupMap.get(typeIdString) ?? fallbacks.NoMatch`
 *      for `resolved`, the synthetic `ErrorThrower` for
 *      `errored`, `fallbacks.Builtin ?? fallbacks.NoMatch` for
 *      `resolved-builtin`), the right value is written through.
 *      This handles the *reattach-to-existing-session* case: the
 *      dispatcher session's `userState` carries the terminal
 *      state already, but the in-memory `componentAtom` is
 *      fresh and starts at `Loading`; the first effect run
 *      after reattach pulls everything back into sync.
 *
 * Throughout, the `progressMessageAtom` is set to a short
 * human-readable string (`"loading entity…"` →
 * `"resolving renderer…"` → cleared on terminal) for the
 * benefit of generic `useDispatchProgressMessage()`-driven
 * fallbacks. Fallbacks that want a typed view of the state
 * machine itself can call
 * `useDispatchProgressState<StaticEntityDispatchState>()`.
 *
 * The terminal `errored` rendering goes through a synthetic
 * `ErrorThrower` component that, when mounted by the
 * provider, reads `stateAtom` and throws `Error(state.message)`
 * during render. The per-entry React error boundary catches
 * the throw and renders the user's `fallbacks.Error` component
 * with the thrown error. This unifies the "state-machine
 * errored" path with the "user component threw during render"
 * path under a single error-boundary surface.
 */

import { type EntityMetadata, EntityRefStub, type OpResultStub } from "@fxtr-ui/core";
import { useAtomValue } from "jotai";
import type { DispatchKeyRegistryEntry, DispatchedComponent } from "./registry_types";

/**
 * Tagged-union state of the static entity-dispatch state
 * machine. Stored in the entry's session `stateAtom` (wire-
 * serialized through the dispatcher session's userState).
 *
 * Each phase carries only the values needed to drive the next
 * transition or to render terminal-state UI; the `OpResultStub`
 * leaves keep their underlying load / metadata slots alive
 * through normal `userState` refcounting.
 */
export type StaticEntityDispatchState =
	| { readonly phase: "init" }
	| { readonly phase: "loading-entity"; readonly load: OpResultStub<unknown> }
	| {
			readonly phase: "loading-type-metadata";
			readonly typeRef: EntityRefStub;
			readonly meta: OpResultStub<EntityMetadata>;
	  }
	| { readonly phase: "resolved"; readonly typeIdString: string }
	/**
	 * Reached without going through a load: the stub's
	 * `shortId` carries an unloadable-tag prefix (`.` /
	 * anonymous, `!` / builtin). The state machine renders the
	 * key's `fallbacks.Builtin` (or `fallbacks.NoMatch` if
	 * unset) and stays terminal — no wire traffic.
	 */
	| { readonly phase: "resolved-builtin" }
	| { readonly phase: "errored"; readonly message: string };

/**
 * Recognize stubs that the workstation can't load — entity
 * shortIds starting with one of these prefixes carry no
 * loadable content (`.` is the "here" anonymous-node tag,
 * `!` is the builtin tag, `c` is the context-provenance node
 * tag, `u` is the untracked-provenance wrap). Calling
 * `load_cbor` on them would hang or error out on the server,
 * so the static dispatcher short-circuits to a `Builtin` /
 * `NoMatch` render instead.
 *
 * `c`- and `u`-nodes are included here even though their tags
 * aren't literally a "builtin": they're system-defined
 * provenance forms that users can't replace with their own
 * renderer and that carry no loadable content (a `c`-node's
 * contents live on its `context_data` d-node; a `u`-node wraps
 * another node) — both reachable via
 * `allocateGetEntityMetadata`. Lumping them under the
 * builtin treatment keeps the dispatcher's "stubs we won't
 * try to load" set in one place. Mirrors the dynamic
 * dispatcher's `isBuiltinEntityShortId` — **keep the two in sync.**
 */
function isBuiltinEntityShortId(shortId: string): boolean {
	return (
		shortId.startsWith(".") ||
		shortId.startsWith("!") ||
		shortId.startsWith("c") ||
		shortId.startsWith("u")
	);
}

export interface StaticEntityTypeDispatchOptions<Props extends object> {
	/**
	 * Map from the entity-type entity's `id_string` to the
	 * component to render for that type. Lookup uses the
	 * `EntityMetadata.id_string` returned by
	 * `allocateGetEntityMetadata` on the entity's `$fxtr.type`
	 * ref. A miss falls back to the key's `fallbacks.NoMatch`.
	 */
	readonly lookupMap: ReadonlyMap<string, DispatchedComponent<EntityRefStub, Props>>;
}

/**
 * Build a `DispatchKeyRegistryEntry` that resolves entity
 * dispatches via the load → metadata → type-id-lookup pipeline
 * described in the module header. Pair the returned entry with
 * an entity-allowed `DispatchKey` via `entityEntry(key,
 * entry)`. The key supplies the Loading / NoMatch / Error /
 * optional Builtin fallback components (see `keys.ts` and
 * `fallbacks.ts`); this helper just supplies the resolution
 * recipe.
 */
export function staticEntityTypeDispatch<Props extends object>(
	opts: StaticEntityTypeDispatchOptions<Props>,
): DispatchKeyRegistryEntry<EntityRefStub, Props, StaticEntityDispatchState> {
	return {
		initialState: { phase: "init" },
		initialProgressMessage: "",
		effectFn: ({ stub, stateAtom, componentAtom, progressMessageAtom, fallbacks }) => {
			// Synthetic component used for terminal `errored` rendering.
			// Closes over `stateAtom` so it always reads the latest
			// message; identity-stable so the terminal-state
			// re-sync's `componentAtom !== ErrorThrower` guard works
			// across effect runs.
			const ErrorThrower: DispatchedComponent<EntityRefStub, Props> = () => {
				const state = useAtomValue(stateAtom);
				if (state.phase === "errored") {
					throw new Error(state.message);
				}
				// Unreachable: the effect only writes `ErrorThrower` to
				// `componentAtom` from the `errored` arm, and reverts
				// it via the resolved/loading arms before transitioning
				// out. Surfacing the invariant violation here beats
				// rendering nothing and hiding the bug.
				throw new Error(
					`StaticEntityDispatchErrorThrower rendered with state.phase="${state.phase}" — should only be mounted while state.phase === "errored". This is a dispatch-helper invariant violation.`,
				);
			};
			ErrorThrower.displayName = "StaticEntityDispatchErrorThrower";

			return (get, set) => {
				const state = get(stateAtom);
				switch (state.phase) {
					case "init": {
						if (isBuiltinEntityShortId(stub.shortId)) {
							set(stateAtom, { phase: "resolved-builtin" });
							return;
						}
						set(stateAtom, (_prev, ctx) => ({
							phase: "loading-entity",
							load: ctx.allocateLoad(stub),
						}));
						set(progressMessageAtom, "loading entity…");
						return;
					}
					case "loading-entity": {
						const result = get(state.load.result);
						if (result.status === "pending") return;
						if (result.status === "errored") {
							set(stateAtom, { phase: "errored", message: result.message });
							return;
						}
						// `result.status === "filled"`. The loaded value
						// should be a record carrying a `$fxtr.type`
						// EntityRefStub leaf; reject anything else.
						const value = result.value;
						const typeField =
							value !== null && typeof value === "object" && !Array.isArray(value)
								? (value as Record<string, unknown>)["$fxtr.type"]
								: undefined;
						if (!(typeField instanceof EntityRefStub)) {
							set(stateAtom, {
								phase: "errored",
								message:
									"Entity loaded value has no `$fxtr.type` entity-ref field — static entity dispatch cannot resolve a renderer without one.",
							});
							return;
						}
						set(stateAtom, (_prev, ctx) => ({
							phase: "loading-type-metadata",
							typeRef: typeField,
							meta: ctx.allocateGetEntityMetadata(typeField),
						}));
						set(progressMessageAtom, "resolving renderer…");
						return;
					}
					case "loading-type-metadata": {
						const result = get(state.meta.result);
						if (result.status === "pending") return;
						if (result.status === "errored") {
							set(stateAtom, { phase: "errored", message: result.message });
							return;
						}
						const typeIdString = result.value.id_string;
						set(stateAtom, { phase: "resolved", typeIdString });
						set(progressMessageAtom, "");
						const Component = opts.lookupMap.get(typeIdString) ?? fallbacks.NoMatch;
						if (get(componentAtom) !== Component) {
							set(componentAtom, Component);
						}
						return;
					}
					case "resolved": {
						// Terminal re-sync: covers the reattach case
						// (state already terminal, componentAtom fresh).
						const Component = opts.lookupMap.get(state.typeIdString) ?? fallbacks.NoMatch;
						if (get(componentAtom) !== Component) {
							set(componentAtom, Component);
						}
						return;
					}
					case "resolved-builtin": {
						// Terminal — render the optional `Builtin`, or
						// fall through to `NoMatch`. Same re-sync
						// pattern as `resolved` for the reattach case.
						const Component = fallbacks.Builtin ?? fallbacks.NoMatch;
						if (get(componentAtom) !== Component) {
							set(componentAtom, Component);
						}
						return;
					}
					case "errored": {
						if (get(componentAtom) !== ErrorThrower) {
							set(componentAtom, ErrorThrower);
						}
						return;
					}
				}
			};
		},
	};
}
