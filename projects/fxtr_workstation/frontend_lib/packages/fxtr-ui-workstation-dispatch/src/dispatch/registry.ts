/**
 * `createWorkstationDispatchRegistry` — thin builder around
 * `createDispatchRegistry` from `@fxtr-ui/react-dispatch` that
 * configures the four workstation dispatch keys for both entity
 * and service ref kinds.
 *
 * For each `(kind, key)` combo:
 *
 *   - Consumer supplies a `ReadonlyMap<typeIdString, Component>`
 *     via `opts.entity.<key>` / `opts.service.<key>`. An absent
 *     map (or an empty one) means "no consumer renderers
 *     registered for this slot"; every dispatch lands on the
 *     key's `fallbacks.NoMatch` shipped by this package.
 *   - The wrapper picks the appropriate `staticEntityTypeDispatch`
 *     / `staticServiceTypeDispatch` helper and registers the
 *     entry.
 *
 * Fallback components (Loading / NoMatch / Error) live on the
 * `DispatchKey` itself (see `./keys.ts`); the registry no longer
 * configures them per-entry. Consumers that want to customise
 * specific fallbacks per dispatcher can use
 * `overrideKeyFallbacks(key, partial)` and pass the overrides to
 * `createStaticDispatcher`.
 *
 * Consumers who want full control of an entry — e.g. a custom
 * effect-fn factory — can skip this helper and call
 * `createDispatchRegistry` from `@fxtr-ui/react-dispatch`
 * directly.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { DispatchRegistry, DispatchedComponent } from "@fxtr-ui/react-dispatch";
import {
	createDispatchRegistry,
	entityEntry,
	serviceEntry,
	staticEntityTypeDispatch,
	staticServiceTypeDispatch,
} from "@fxtr-ui/react-dispatch";
import {
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "./keys.js";

/**
 * Per-key lookup maps for one ref kind. Each map is keyed by the
 * type-entity / service-type's `id_string`. Missing or empty
 * maps fall through to the default no-match fallback.
 */
export interface WorkstationDispatchSide<Stub> {
	readonly fullPanel?: ReadonlyMap<string, DispatchedComponent<Stub, FullPanelProps>>;
	readonly blockLink?: ReadonlyMap<string, DispatchedComponent<Stub, LinkProps>>;
	readonly inlineLink?: ReadonlyMap<string, DispatchedComponent<Stub, LinkProps>>;
	readonly hoverPreview?: ReadonlyMap<string, DispatchedComponent<Stub, HoverPreviewProps>>;
}

export interface WorkstationDispatchRegistryOptions {
	readonly entity?: WorkstationDispatchSide<EntityRefStub>;
	readonly service?: WorkstationDispatchSide<ServiceRefStub>;
}

const EMPTY_ENTITY_MAP: ReadonlyMap<string, never> = new Map<string, never>();

export function createWorkstationDispatchRegistry(
	opts: WorkstationDispatchRegistryOptions = {},
): DispatchRegistry {
	const entityFullPanel = opts.entity?.fullPanel ?? EMPTY_ENTITY_MAP;
	const entityBlockLink = opts.entity?.blockLink ?? EMPTY_ENTITY_MAP;
	const entityInlineLink = opts.entity?.inlineLink ?? EMPTY_ENTITY_MAP;
	const entityHoverPreview = opts.entity?.hoverPreview ?? EMPTY_ENTITY_MAP;
	const serviceFullPanel = opts.service?.fullPanel ?? EMPTY_ENTITY_MAP;
	const serviceBlockLink = opts.service?.blockLink ?? EMPTY_ENTITY_MAP;
	const serviceInlineLink = opts.service?.inlineLink ?? EMPTY_ENTITY_MAP;
	const serviceHoverPreview = opts.service?.hoverPreview ?? EMPTY_ENTITY_MAP;

	return createDispatchRegistry({
		entityEntries: [
			entityEntry(
				FullPanel,
				staticEntityTypeDispatch<FullPanelProps>({
					lookupMap: entityFullPanel,
				}),
			),
			entityEntry(
				BlockLink,
				staticEntityTypeDispatch<LinkProps>({
					lookupMap: entityBlockLink,
				}),
			),
			entityEntry(
				InlineLink,
				staticEntityTypeDispatch<LinkProps>({
					lookupMap: entityInlineLink,
				}),
			),
			entityEntry(
				HoverPreview,
				staticEntityTypeDispatch<HoverPreviewProps>({
					lookupMap: entityHoverPreview,
				}),
			),
		],
		serviceEntries: [
			serviceEntry(
				FullPanel,
				staticServiceTypeDispatch<FullPanelProps>({
					lookupMap: serviceFullPanel,
				}),
			),
			serviceEntry(
				BlockLink,
				staticServiceTypeDispatch<LinkProps>({
					lookupMap: serviceBlockLink,
				}),
			),
			serviceEntry(
				InlineLink,
				staticServiceTypeDispatch<LinkProps>({
					lookupMap: serviceInlineLink,
				}),
			),
			serviceEntry(
				HoverPreview,
				staticServiceTypeDispatch<HoverPreviewProps>({
					lookupMap: serviceHoverPreview,
				}),
			),
		],
	});
}
