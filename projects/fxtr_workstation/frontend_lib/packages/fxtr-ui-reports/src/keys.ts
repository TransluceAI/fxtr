/**
 * `Figure` dispatch key — the rendering slot for entities embedded
 * inline inside a report body via `<RenderFigure entity={...}/>`.
 *
 * "Interactive" key (like `FullPanel`): its Props carries an
 * `ownStorage: FxtrSessionAtom<unknown>` that the report's tree
 * renderer threads down per-figure (keyed by the MDX-assigned
 * `figureKey`). Figures get a stable storage slot scoped to the
 * containing report's panel session — so e.g. a figure renderer
 * with expand/collapse UI can persist that state across the panel
 * session's lifetime.
 *
 * Both entity and service stubs are allowed at this key, matching
 * the workstation's other keys. The shipped no-match fallback
 * re-dispatches to `FullPanel` (see `./fallbacks.tsx`).
 */

import type { EntityRefStub, FxtrSessionAtom, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchKey,
	type ErrorFallbackComponent,
	declareDispatchKey,
} from "@fxtr-ui/react-dispatch";
import { FigureErrorFallback, FigureLoadingFallback, FigureNoMatchFallback } from "./fallbacks.js";

export interface FigureProps {
	readonly ownStorage: FxtrSessionAtom<unknown>;
}

// Hardcoded UUID. Wire-stable; do not change.
const UUID_FIGURE = "f4a3e2c1-9d0b-4d8a-9e2c-5f1b3a8e0d12";

// FigureErrorFallback's declared props are narrower than the
// ErrorFallbackComponent shape the dispatch system wants. The
// component reads `stub` + `error` only and ignores the extra
// Props at render time, so casting is sound. Mirrors the helper
// pattern in `@fxtr-ui/workstation/dispatch/keys.ts`.
function castErrorFallback(): ErrorFallbackComponent<EntityRefStub | ServiceRefStub, FigureProps> {
	return FigureErrorFallback as unknown as ErrorFallbackComponent<
		EntityRefStub | ServiceRefStub,
		FigureProps
	>;
}

export const Figure: DispatchKey<FigureProps> = declareDispatchKey<FigureProps>({
	uuid: UUID_FIGURE,
	description: "Figure",
	fallbacks: {
		Loading: FigureLoadingFallback,
		NoMatch: FigureNoMatchFallback,
		Error: castErrorFallback(),
	},
});
