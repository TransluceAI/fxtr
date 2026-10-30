/**
 * Assembles the demo's `DispatchRegistry` from the type-id
 * strings the orchestrator injects via
 * `import.meta.env.VITE_DISPATCH_TYPE_IDS`.
 *
 * One `CardKey` registered for both entity and service kinds:
 *
 *   - entity entries: `staticEntityTypeDispatch` with a
 *     `Map<id_string, NoteCard | PhotoCard>` lookup.
 *   - service entries: `staticServiceTypeDispatch` with a
 *     `Map<type_id_string, EchoServiceCard | CounterServiceCard>`.
 *
 * Loading / NoMatch / Error / Builtin fallback components live
 * on `CardKey` itself; this module only specifies the
 * resolution recipe (the `lookupMap`).
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchedComponent,
	type ExtractDispatchProps,
	createDispatchRegistry,
	entityEntry,
	serviceEntry,
	staticEntityTypeDispatch,
	staticServiceTypeDispatch,
} from "@fxtr-ui/react-dispatch";
import { CounterServiceCard } from "./cards/CounterServiceCard";
import { EchoServiceCard } from "./cards/EchoServiceCard";
import { NoteCard } from "./cards/NoteCard";
import { PhotoCard } from "./cards/PhotoCard";
import { CardKey } from "./dispatch_keys";

interface DispatchTypeIds {
	readonly note: string;
	readonly photo: string;
	readonly echo: string;
	readonly counter: string;
}

const typeIdsJson = import.meta.env.VITE_DISPATCH_TYPE_IDS;
if (typeof typeIdsJson !== "string") {
	throw new Error(
		"VITE_DISPATCH_TYPE_IDS is not set. Run via `pnpm dev` so the orchestrator injects it.",
	);
}
const typeIds: DispatchTypeIds = JSON.parse(typeIdsJson);

// Carry the Props type through from the key declaration so the
// lookup-map values + the static helpers' generic argument all
// line up structurally.
type CardProps = ExtractDispatchProps<typeof CardKey>;

const entityLookupMap = new Map<string, DispatchedComponent<EntityRefStub, CardProps>>([
	[typeIds.note, NoteCard],
	[typeIds.photo, PhotoCard],
]);

const serviceLookupMap = new Map<string, DispatchedComponent<ServiceRefStub, CardProps>>([
	[typeIds.echo, EchoServiceCard],
	[typeIds.counter, CounterServiceCard],
]);

export const dispatchRegistry = createDispatchRegistry({
	entityEntries: [
		entityEntry(CardKey, staticEntityTypeDispatch<CardProps>({ lookupMap: entityLookupMap })),
	],
	serviceEntries: [
		serviceEntry(CardKey, staticServiceTypeDispatch<CardProps>({ lookupMap: serviceLookupMap })),
	],
});
