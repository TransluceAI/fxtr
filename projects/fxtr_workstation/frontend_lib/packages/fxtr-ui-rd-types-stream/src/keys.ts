/**
 * The ``StreamEntry`` dispatch key.
 *
 * A reusable rendering slot for "one entry in an ordered, scrolling
 * stream of typed log-like items": e.g. the agent plugin's chain of
 * event records (script executions, callback notifications, invoke
 * results, …). The owning panel component drives the loading +
 * iteration; each entry's actual card is dispatched through this
 * key.
 *
 * ``Props`` is ``NoProps`` — each renderer manages its own data
 * load via :func:`useDispatchSharedAtom` (mirroring the report-link
 * renderers' pattern in :mod:`@fxtr-ui/reports`). Keeping the prop
 * surface empty leaves room for a second consumer system to extend
 * the key without breaking compat — any added field would have to
 * be optional and the existing renderers stay valid.
 *
 * Both ref kinds are admitted: the agent plugin only ever
 * dispatches entity refs (each chain entry's ``event`` field is
 * an :class:`EntityRef`), but downstream consumers may want to
 * stream service-typed entries too.
 *
 * UUID is wire-stable. Persisted dispatcher state keys by
 * ``(uuid, stub.shortId)`` so changing this is a breaking change
 * for any session carrying StreamEntry-keyed entries.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchKey,
	type ErrorFallbackComponent,
	type NoProps,
	declareDispatchKey,
} from "@fxtr-ui/react-dispatch";
import {
	StreamEntryErrorFallback,
	StreamEntryLoadingFallback,
	StreamEntryNoMatchFallback,
} from "./fallbacks.js";

// The error fallback's declared props (`{stub, error: unknown}`)
// are structurally compatible with `ErrorFallbackComponent`'s
// requirement (`{stub, error: unknown} & Props`), but the type
// system needs a per-key cast because the component is shared
// across keys with different `Props` shapes. Same pattern the
// workstation-dispatch keys use.
function castErrorFallback<Props extends object>(): ErrorFallbackComponent<
	EntityRefStub | ServiceRefStub,
	Props
> {
	return StreamEntryErrorFallback as unknown as ErrorFallbackComponent<
		EntityRefStub | ServiceRefStub,
		Props
	>;
}

// Hardcoded UUID. Wire-stable; do not change.
const UUID_STREAM_ENTRY = "4b2e1a3c-7d68-4e91-a25f-9c3d8b0e1f7a";

/**
 * The ``StreamEntry`` dispatch key. Both entity and service stubs
 * are admitted (``Allowed = "entity" | "service"``), and ``Props``
 * is :data:`NoProps`. The fallback bundle covers loading,
 * no-match, and error paths via :mod:`./fallbacks`.
 */
export const StreamEntry: DispatchKey<NoProps> = declareDispatchKey<NoProps>({
	uuid: UUID_STREAM_ENTRY,
	description: "StreamEntry",
	fallbacks: {
		Loading: StreamEntryLoadingFallback,
		NoMatch: StreamEntryNoMatchFallback,
		Error: castErrorFallback<NoProps>(),
	},
});

/**
 * Type alias mirroring the workstation-dispatch convention. Present
 * for symmetry — consumers writing ``DispatchedComponent<Stub,
 * StreamEntryProps>`` is parallel to ``DispatchedComponent<Stub,
 * FullPanelProps>``. Today this is just :data:`NoProps`; the alias
 * exists so an additive prop extension later doesn't require
 * search-and-replace at every renderer.
 */
export type StreamEntryProps = NoProps;
