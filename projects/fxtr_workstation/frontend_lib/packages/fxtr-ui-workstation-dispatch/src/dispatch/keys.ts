/**
 * The four workstation dispatch keys + their `Props` types.
 *
 * One key per rendering slot:
 *
 *   - `FullPanel` — the body of a dockview panel. *Interactive*:
 *     the consumer (`PanelWrapper`) passes a `FxtrSessionAtom<unknown>`
 *     sub-atom of the panel's session userState as `ownStorage`,
 *     and the dispatched renderer reads/writes there for its own
 *     per-mount state. The renderer typically narrows by wrapping
 *     in `atomWithDefault` + `useSubAtom`.
 *
 *   - `BlockLink` / `InlineLink` — display rendering of a ref
 *     from elsewhere (a row label, an entry in a JsonTree, etc.).
 *     *Non-interactive*: no consumer atom prop. Per-(stub, key)
 *     state, if any, lives on the dispatcher session via
 *     `useDispatchSharedAtom`.
 *
 *   - `HoverPreview` — base-ui PreviewCard body rendered when a
 *     ref is hovered. Non-interactive; same model as the link
 *     keys.
 *
 * The interactive / non-interactive distinction is purely a
 * convention on the `Props` type. The dispatch system from
 * `@fxtr-ui/react-dispatch` itself doesn't know or care.
 *
 * **UUID convention.** Each key carries a wire-stable UUID
 * hardcoded here (RFC 4122). The dispatcher session keys its
 * `entries[uuid][shortId]` userState slot on this; changing a
 * UUID is a breaking change for any persisted dispatch state.
 *
 * Both ref kinds are allowed at every key (constructed via
 * `declareDispatchKey`). Each key's `fallbacks` bundle ships the
 * Loading / NoMatch / Error components used by the dispatcher
 * when there's no consumer renderer registered (or while one is
 * resolving / errored). The bundled fallbacks branch internally
 * on the stub's instanceof to dispatch to the kind-specific
 * components in `../fallbacks/`.
 */

import type { EntityRefStub, FxtrSessionAtom, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchKey,
	type ErrorFallbackComponent,
	type NoProps,
	declareDispatchKey,
} from "@fxtr-ui/react-dispatch";
import { DispatchErrorFallback } from "../fallbacks/DispatchErrorFallback.js";
import {
	BlockLinkFallback,
	FullPanelBuiltin,
	FullPanelLoading,
	FullPanelNoMatch,
	HoverPreviewLoading,
	HoverPreviewNoMatch,
	InlineLinkFallback,
} from "./combined_fallbacks.js";

/**
 * `DispatchErrorFallback`'s declared props type
 * (`{stub, error: Error}`) is narrower than the `ErrorFallbackComponent`
 * shape the dispatch system wants (`{stub, error: unknown} & Props`).
 * The component reads `stub` + `error` only and ignores the extra Props
 * at render time, so casting per-key is sound. Mirrors the
 * `castErrorFallback` helper that used to live in `./registry.ts`.
 */
function castErrorFallback<Props extends object>(): ErrorFallbackComponent<
	EntityRefStub | ServiceRefStub,
	Props
> {
	return DispatchErrorFallback as unknown as ErrorFallbackComponent<
		EntityRefStub | ServiceRefStub,
		Props
	>;
}

/**
 * Props passed to a `FullPanel`-dispatched component beyond
 * `stub`. The owning `PanelWrapper` slices a key out of its
 * panel session's userState and threads it through as
 * `ownStorage`. The renderer typically wraps it in
 * `atomWithDefault` + `useSubAtom` to project to a typed shape
 * specific to that renderer.
 *
 * Typed `unknown` because the consumer (`PanelWrapper`) can't
 * know what shape the renderer wants — the renderer narrows on
 * its end. See design note
 * `05_dispatch_and_inline_loading.md` for the rationale.
 */
export interface FullPanelProps {
	readonly ownStorage: FxtrSessionAtom<unknown>;
}

/**
 * Props passed to `BlockLink` / `InlineLink`-dispatched
 * components beyond `stub`. `linkText` lets callers override
 * the renderer's default label.
 */
export interface LinkProps {
	readonly linkText?: string;
}

/**
 * `HoverPreview` has no extra props beyond `stub`. Aliased to
 * `NoProps` (i.e. `object`) — using `Record<string, never>`
 * would block JSX-level attributes like React's `key`.
 */
export type HoverPreviewProps = NoProps;

// Hardcoded UUIDs. Wire-stable; do not change.
const UUID_FULL_PANEL = "21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234";
const UUID_BLOCK_LINK = "b3c4d5e6-7f89-4a01-9b23-4c5d6e7f8901";
const UUID_INLINE_LINK = "c4d5e6f7-8901-4a12-9b34-5d6e7f890123";
const UUID_HOVER_PREVIEW = "d5e6f7a8-9012-4a23-9b45-6e7f89012345";

export const FullPanel: DispatchKey<FullPanelProps> = declareDispatchKey<FullPanelProps>({
	uuid: UUID_FULL_PANEL,
	description: "FullPanel",
	fallbacks: {
		Loading: FullPanelLoading,
		NoMatch: FullPanelNoMatch,
		Error: castErrorFallback(),
		// Rendered when the dispatcher short-circuits on a
		// `c` / `!` / `.` stub — see `isBuiltinEntityShortId` in
		// `@fxtr-ui/react-dispatch`. The fallback does a metadata
		// call (no load) and surfaces the populated fields
		// including `context_data` for c-tag nodes.
		Builtin: FullPanelBuiltin,
	},
});

export const BlockLink: DispatchKey<LinkProps> = declareDispatchKey<LinkProps>({
	uuid: UUID_BLOCK_LINK,
	description: "BlockLink",
	// Single fallback for both loading and no-match (a link looks
	// the same while resolving as when no renderer is registered).
	// Consumers that want a distinct loading display can register
	// a `Loading` override via `overrideKeyFallbacks`.
	fallbacks: {
		Loading: BlockLinkFallback,
		NoMatch: BlockLinkFallback,
		Error: castErrorFallback(),
	},
});

export const InlineLink: DispatchKey<LinkProps> = declareDispatchKey<LinkProps>({
	uuid: UUID_INLINE_LINK,
	description: "InlineLink",
	fallbacks: {
		Loading: InlineLinkFallback,
		NoMatch: InlineLinkFallback,
		Error: castErrorFallback(),
	},
});

export const HoverPreview: DispatchKey<HoverPreviewProps> = declareDispatchKey<HoverPreviewProps>({
	uuid: UUID_HOVER_PREVIEW,
	description: "HoverPreview",
	fallbacks: {
		Loading: HoverPreviewLoading,
		NoMatch: HoverPreviewNoMatch,
		Error: castErrorFallback(),
	},
});
