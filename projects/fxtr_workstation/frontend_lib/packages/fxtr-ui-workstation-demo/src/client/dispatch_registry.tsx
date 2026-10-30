/**
 * Demo dispatch registry, constructed manually via
 * `createDispatchRegistry` so we can configure all five dispatch
 * keys — the workstation's four (`FullPanel` / `BlockLink` /
 * `InlineLink` / `HoverPreview`) plus the reports package's new
 * `Figure` key.
 *
 * Per-type renderers are pulled in via
 * `import.meta.env.VITE_DEMO_TYPE_IDS` from the orchestrator,
 * which reads the Python backend's `TYPE_IDS` stdout line.
 *
 * Notable: the `Figure` slot for `note` is intentionally
 * unregistered. When the demo report's body embeds
 * `<RenderFigure entity={demoNote} />`, the dispatcher falls
 * through to `FigureNoMatchFallback` — the user-visible
 * demonstration of the default fallback (note's `FullPanel`
 * wrapped in a shaded container with a reflink header).
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import {
	type DispatchedComponent,
	createDispatchRegistry,
	entityEntry,
	serviceEntry,
	staticEntityTypeDispatch,
	staticServiceTypeDispatch,
} from "@fxtr-ui/react-dispatch";
import {
	Figure,
	type FigureProps,
	RootReportBlockLink,
	RootReportFullPanel,
	RootReportHoverPreview,
	RootReportInlineLink,
	SubReportBlockLink,
	SubReportFigure,
	SubReportFullPanel,
	SubReportHoverPreview,
	SubReportInlineLink,
} from "@fxtr-ui/reports";
import {
	BASIC_TYPE_ID_STRING,
	BasicTypeBlockLink,
	BasicTypeInlineLink,
	BlockLink,
	FullPanel,
	type FullPanelProps,
	HoverPreview,
	type HoverPreviewProps,
	InlineLink,
	type LinkProps,
} from "@fxtr-ui/workstation";
import {
	CounterServiceBlockLink,
	CounterServiceFullPanel,
	DemoListNodeBlockLink,
	DemoListNodeFullPanel,
	DemoListNodeHoverPreview,
	DemoNoteBlockLink,
	DemoNoteFullPanel,
	DemoNoteHoverPreview,
	EchoServiceBlockLink,
	EchoServiceFullPanel,
	NotifyNServiceBlockLink,
	NotifyNServiceFullPanel,
} from "./handlers";

interface DemoTypeIds {
	readonly note: string;
	readonly list_node: string;
	readonly echo: string;
	readonly counter: string;
	readonly notify_n: string;
	readonly report: string;
	readonly sub_report: string;
}

const typeIdsJson = import.meta.env.VITE_DEMO_TYPE_IDS;
if (typeof typeIdsJson !== "string") {
	throw new Error(
		"VITE_DEMO_TYPE_IDS is not set. Run via `pnpm dev` so the orchestrator injects it.",
	);
}
const typeIds: DemoTypeIds = JSON.parse(typeIdsJson);

// ----- Entity-side lookup maps -----

const entityFullPanel = new Map<string, DispatchedComponent<EntityRefStub, FullPanelProps>>([
	[typeIds.note, DemoNoteFullPanel],
	[typeIds.list_node, DemoListNodeFullPanel],
	[typeIds.report, RootReportFullPanel],
	[typeIds.sub_report, SubReportFullPanel],
]);

const entityBlockLink = new Map<string, DispatchedComponent<EntityRefStub, LinkProps>>([
	[typeIds.note, DemoNoteBlockLink],
	[typeIds.list_node, DemoListNodeBlockLink],
	[typeIds.report, RootReportBlockLink],
	[typeIds.sub_report, SubReportBlockLink],
	// `FxtrBasicType` ref — surfaces `$fxtr.type` slots as a
	// `type {name}` badge instead of a plain shortId.
	[BASIC_TYPE_ID_STRING, BasicTypeBlockLink],
]);

const entityInlineLink = new Map<string, DispatchedComponent<EntityRefStub, LinkProps>>([
	[typeIds.report, RootReportInlineLink],
	[typeIds.sub_report, SubReportInlineLink],
	[BASIC_TYPE_ID_STRING, BasicTypeInlineLink],
]);

const entityHoverPreview = new Map<string, DispatchedComponent<EntityRefStub, HoverPreviewProps>>([
	[typeIds.note, DemoNoteHoverPreview],
	[typeIds.list_node, DemoListNodeHoverPreview],
	[typeIds.report, RootReportHoverPreview],
	[typeIds.sub_report, SubReportHoverPreview],
]);

// The Figure key registers only `sub_report` — `note` is
// intentionally absent so the demo report's `<RenderFigure
// entity={note} />` exercises the default `FigureNoMatchFallback`
// path (shaded FullPanel + reflink).
const entityFigure = new Map<string, DispatchedComponent<EntityRefStub, FigureProps>>([
	[typeIds.sub_report, SubReportFigure],
]);

// ----- Service-side lookup maps -----

const serviceFullPanel = new Map<string, DispatchedComponent<ServiceRefStub, FullPanelProps>>([
	[typeIds.echo, EchoServiceFullPanel],
	[typeIds.counter, CounterServiceFullPanel],
	[typeIds.notify_n, NotifyNServiceFullPanel],
]);

const serviceBlockLink = new Map<string, DispatchedComponent<ServiceRefStub, LinkProps>>([
	[typeIds.echo, EchoServiceBlockLink],
	[typeIds.counter, CounterServiceBlockLink],
	[typeIds.notify_n, NotifyNServiceBlockLink],
]);

const EMPTY_SERVICE_MAP: ReadonlyMap<string, never> = new Map<string, never>();

// ----- Assemble the registry -----

export const dispatchRegistry = createDispatchRegistry({
	entityEntries: [
		entityEntry(
			FullPanel,
			staticEntityTypeDispatch<FullPanelProps>({ lookupMap: entityFullPanel }),
		),
		entityEntry(BlockLink, staticEntityTypeDispatch<LinkProps>({ lookupMap: entityBlockLink })),
		entityEntry(InlineLink, staticEntityTypeDispatch<LinkProps>({ lookupMap: entityInlineLink })),
		entityEntry(
			HoverPreview,
			staticEntityTypeDispatch<HoverPreviewProps>({ lookupMap: entityHoverPreview }),
		),
		entityEntry(Figure, staticEntityTypeDispatch<FigureProps>({ lookupMap: entityFigure })),
	],
	serviceEntries: [
		serviceEntry(
			FullPanel,
			staticServiceTypeDispatch<FullPanelProps>({ lookupMap: serviceFullPanel }),
		),
		serviceEntry(BlockLink, staticServiceTypeDispatch<LinkProps>({ lookupMap: serviceBlockLink })),
		serviceEntry(
			InlineLink,
			staticServiceTypeDispatch<LinkProps>({ lookupMap: EMPTY_SERVICE_MAP }),
		),
		serviceEntry(
			HoverPreview,
			staticServiceTypeDispatch<HoverPreviewProps>({ lookupMap: EMPTY_SERVICE_MAP }),
		),
	],
});
