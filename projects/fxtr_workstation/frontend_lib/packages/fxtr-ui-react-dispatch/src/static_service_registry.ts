/**
 * Builder for a `DispatchKeyRegistryEntry` that resolves a
 * service dispatch by walking a fixed `type_id_string →
 * component` lookup map. The state machine:
 *
 *   1. `init` → allocate a service-metadata query on `stub`;
 *      transition to `loading-type-metadata`.
 *   2. `loading-type-metadata` → wait for the metadata
 *      `result` atom. On filled, read `metadata.type_id_string`,
 *      transition to terminal `resolved`. On errored, transition
 *      to terminal `errored`.
 *   3. Terminal phases: each effect run re-checks the
 *      `componentAtom` and writes the right component if it
 *      drifts. Handles the reattach-to-existing-session case
 *      where `userState` already carries the terminal state but
 *      the in-memory `componentAtom` is fresh.
 *
 * Same `ErrorThrower` + error-boundary pattern as
 * `staticEntityTypeDispatch`. Simpler shape: no entity-load
 * hop, since the type id is reachable directly via
 * `allocateGetServiceMetadata`.
 */

import type { OpResultStub, ServiceMetadata, ServiceRefStub } from "@fxtr-ui/core";
import { useAtomValue } from "jotai";
import type { DispatchKeyRegistryEntry, DispatchedComponent } from "./registry_types";

/**
 * Tagged-union state of the static service-dispatch state
 * machine. Stored in the entry's session `stateAtom`.
 */
export type StaticServiceDispatchState =
	| { readonly phase: "init" }
	| { readonly phase: "loading-type-metadata"; readonly meta: OpResultStub<ServiceMetadata> }
	| { readonly phase: "resolved"; readonly typeIdString: string }
	| { readonly phase: "errored"; readonly message: string };

export interface StaticServiceTypeDispatchOptions<Props extends object> {
	/**
	 * Map from the service's `type_id_string` to the component
	 * to render. A miss falls back to the key's `fallbacks.NoMatch`.
	 */
	readonly lookupMap: ReadonlyMap<string, DispatchedComponent<ServiceRefStub, Props>>;
}

/**
 * Build a `DispatchKeyRegistryEntry` that resolves service
 * dispatches via the metadata → type-id-lookup pipeline
 * described in the module header. Pair the returned entry with
 * a service-allowed `DispatchKey` via `serviceEntry(key,
 * entry)`. The key supplies the Loading / NoMatch / Error
 * fallback components (see `keys.ts` and `fallbacks.ts`); this
 * helper just supplies the resolution recipe.
 */
export function staticServiceTypeDispatch<Props extends object>(
	opts: StaticServiceTypeDispatchOptions<Props>,
): DispatchKeyRegistryEntry<ServiceRefStub, Props, StaticServiceDispatchState> {
	return {
		initialState: { phase: "init" },
		initialProgressMessage: "",
		effectFn: ({ stub, stateAtom, componentAtom, progressMessageAtom, fallbacks }) => {
			const ErrorThrower: DispatchedComponent<ServiceRefStub, Props> = () => {
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
					`StaticServiceDispatchErrorThrower rendered with state.phase="${state.phase}" — should only be mounted while state.phase === "errored". This is a dispatch-helper invariant violation.`,
				);
			};
			ErrorThrower.displayName = "StaticServiceDispatchErrorThrower";

			return (get, set) => {
				const state = get(stateAtom);
				switch (state.phase) {
					case "init": {
						set(stateAtom, (_prev, ctx) => ({
							phase: "loading-type-metadata",
							meta: ctx.allocateGetServiceMetadata(stub),
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
						const typeIdString = result.value.type_id_string;
						set(stateAtom, { phase: "resolved", typeIdString });
						set(progressMessageAtom, "");
						const Component = opts.lookupMap.get(typeIdString) ?? fallbacks.NoMatch;
						if (get(componentAtom) !== Component) {
							set(componentAtom, Component);
						}
						return;
					}
					case "resolved": {
						const Component = opts.lookupMap.get(state.typeIdString) ?? fallbacks.NoMatch;
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
