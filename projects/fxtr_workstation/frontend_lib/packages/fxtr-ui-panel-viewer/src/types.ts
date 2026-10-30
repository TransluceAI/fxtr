/**
 * Cross-package primitives the workstation shell uses to talk about
 * "things you can open in a panel".
 *
 * The workstation's chain entries, breadcrumb segments, history-root
 * prefixes, and follow-link targets all share the same shape: a live
 * `EntityRefStub` / `ServiceRefStub` plus a display mode. We call
 * this a `PanelViewEntry`. Carrying the live stub (rather than just
 * its short id) lets `collectStubsInState` walk the panel session's
 * user state and emit Track ops for every ref currently reachable
 * from the panel's navigation structure — liveness becomes
 * correctness-by-construction.
 *
 * Drag payloads serialize through `JSON.stringify`, which would lose
 * stub class identity. The sister type `PanelViewDragData` is the
 * JSON-safe shape (kind-tagged short id + display mode). The pair of
 * conversions `dragDataOfEntry` / `entryOfDragData(connector)`
 * mediates at the `setDragData` / `parseDragData` boundary; refs
 * round-trip losslessly through the connector's intern map.
 *
 * `display` selects between the dispatched FullPanel renderer
 * (`"default"`) and a hardcoded metadata view (`"metadata"`) that
 * shows the ref's workstation metadata + (for entities) its raw
 * loaded data + the `$fxtr.ctx` / `$fxtr.type` results. The two
 * display modes for the same ref are distinct chain entries:
 * `sameRef` in `breadcrumb/history.ts` compares the `display`
 * field, so "Open metadata here" on the currently-active ref
 * appends a new entry rather than no-op'ing.
 */

import type { Connector } from "@fxtr-ui/core";
import { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";

/**
 * Which kind of ref-stub a value identifies. Mirrors the
 * `RefKind` exported by `@fxtr-ui/react-dispatch`, repeated here
 * so consumers of the workstation surface don't have to depend
 * on the dispatch package just for the literal type.
 */
export type StubKind = "entity" | "service";

/**
 * Display mode for a panel-view entry.
 *
 * - `"default"` — render via the consumer's dispatch registry
 *   (FullPanel for the active entry, BlockLink / InlineLink /
 *   HoverPreview for content refs).
 * - `"metadata"` — render the workstation's hardcoded metadata
 *   view: entity / service metadata, raw loaded data (entities
 *   only), and `$fxtr.ctx` / `$fxtr.type` results. The metadata
 *   view does not go through dispatch, so it doesn't track the
 *   ref in the dispatcher session.
 */
export type PanelViewDisplay = "default" | "metadata";

/**
 * In-process handle for "a thing you can open in a panel". Carries
 * the live, connector-interned stub plus a `display` mode that
 * selects between the dispatched view and the hardcoded metadata
 * view.
 *
 * Used uniformly as chain entries, breadcrumb segments, follow-link
 * targets, and history-root prefix entries. Because every place
 * `PanelViewEntry` lives in panel-session user state contributes its
 * stub to `collectStubsInState`, any ref reachable from the panel's
 * navigation structure is Track-pinned for the lifetime it's
 * reachable.
 *
 * For drag payloads — which cross a `JSON.stringify` boundary —
 * lower to `PanelViewDragData` first via `dragDataOfEntry`, and
 * re-lift on the receiving side via `entryOfDragData(connector)`.
 */
export interface PanelViewEntry {
	readonly stub: EntityRefStub | ServiceRefStub;
	readonly display: PanelViewDisplay;
}

/**
 * JSON-safe identifier for a panel-view entry. Used in drag
 * payloads (`reflink/drag.ts`) and anywhere else that has to
 * round-trip through plain JSON. Reconstitutes to a `PanelViewEntry`
 * via `entryOfDragData(data, connector)` — the connector's
 * `entityRef` / `serviceRef` intern maps make re-interning lossless
 * across the boundary.
 */
export interface PanelViewDragData {
	readonly kind: StubKind;
	readonly shortId: string;
	readonly display: PanelViewDisplay;
}

/**
 * Build a default-mode `PanelViewEntry` from a concrete stub
 * instance. Discriminates the kind at runtime via `instanceof`
 * against the abstract base classes from `@fxtr-ui/core` (only to
 * defend the assertion that `stub` is one of the two ref kinds —
 * the entry itself just stores the stub instance).
 *
 * Callers that want a metadata-mode entry build one explicitly:
 * `{ ...panelViewEntryOf(stub), display: "metadata" }` (or
 * `{ stub, display: "metadata" }` directly).
 */
export function panelViewEntryOf(stub: EntityRefStub | ServiceRefStub): PanelViewEntry {
	if (!(stub instanceof EntityRefStub) && !(stub instanceof ServiceRefStub)) {
		throw new Error(
			"panelViewEntryOf: value is neither an EntityRefStub nor a ServiceRefStub. Callers must pass an instance of one of the two abstract base classes from @fxtr-ui/core.",
		);
	}
	return { stub, display: "default" };
}

/**
 * `entry.stub`'s kind, derived via `instanceof` on the abstract
 * base classes from `@fxtr-ui/core`. Useful where the kind tag is
 * needed without exposing the stub itself (sidebar labels, drag
 * lowering, etc.).
 */
export function entryKind(entry: PanelViewEntry): StubKind {
	return entry.stub instanceof EntityRefStub ? "entity" : "service";
}

/**
 * Lower a `PanelViewEntry` to the JSON-safe drag-data shape.
 * Used by `setDragData` before stringifying drag payloads.
 */
export function dragDataOfEntry(entry: PanelViewEntry): PanelViewDragData {
	return {
		kind: entryKind(entry),
		shortId: entry.stub.shortId,
		display: entry.display,
	};
}

/**
 * Lift a `PanelViewDragData` back to a `PanelViewEntry` by
 * re-interning the stub through the connector. Entity and service
 * refs are connector-scoped (no per-session state) and interned
 * per-`shortId`, so two calls with the same `shortId` hand back the
 * same instance — drag round-trips losslessly within a window.
 */
export function entryOfDragData(data: PanelViewDragData, connector: Connector): PanelViewEntry {
	const stub =
		data.kind === "entity" ? connector.entityRef(data.shortId) : connector.serviceRef(data.shortId);
	return { stub, display: data.display };
}
