/**
 * `PanelWrapper` — dockview content renderer for a ref panel.
 *
 * Drives the panel-session lifecycle (pending → live → unavailable)
 * via `usePanelSession`, and once live, renders:
 *
 *   ┌─────────────────────────────────┐
 *   │ (BreadcrumbBar — planned)       │
 *   ├─────────────────────────────────┤
 *   │ <RenderDispatch as={FullPanel}> │  ← renderer for chain[active]
 *   └─────────────────────────────────┘
 *
 * Session creation is the shell's responsibility — see
 * `PanelViewer` (planned). The shell mints a new
 * `WorkstationSession` whenever it opens a new ref panel,
 * writes the seed chain state into that session's `userState`,
 * then sets the new `sessionId` on this panel's dockview
 * params. `PanelWrapper` itself only attaches and detaches —
 * never creates.
 *
 * Until `sessionId` is non-null, the panel shows a small
 * pending body. Once the shell writes the id back, the
 * `usePanelSession` effect re-fires and attaches.
 *
 * The chain / activeIndex / historyRoot live inside the panel
 * session's userState (not dockview params). `useSessionRootStateAtom`
 * lifts the session's nullable `userState` to a typed
 * `RefPanelChainState` view; `useSubAtom(_, "ownStorage")` carves a
 * sub-atom for the dispatched FullPanel renderer, threaded through
 * the `ownStorage` prop.
 *
 * Provides:
 *   - `PanelContext` — descendants know which dockview panel they're
 *     in + the current chain state.
 *   - `BreadcrumbSourceProvider` — seeds the trail with the active
 *     ref as `currentKey`.
 *   - `PanelErrorBoundary` — downstream renderer crashes don't take
 *     out the whole workstation.
 */

import { EntityRefStub, type ServiceRefStub } from "@fxtr-ui/core";
import { FxtrSessionProvider, useSessionRootStateAtom, useSubAtom } from "@fxtr-ui/react";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { FullPanel } from "@fxtr-ui/workstation-dispatch";
import { EntityMetadataView } from "@fxtr-ui/workstation-dispatch";
import { ServiceMetadataView } from "@fxtr-ui/workstation-dispatch";
import type { DockviewPanelApi, IDockviewPanelProps } from "dockview";
import { useAtomValue } from "jotai";
import { type CSSProperties, Suspense, useEffect, useMemo } from "react";
import { BreadcrumbBar } from "../breadcrumb/BreadcrumbBar.js";
import { BreadcrumbSourceProvider } from "../breadcrumb/BreadcrumbSourceContext.js";
import type { BreadcrumbSource, RefPanelChainState } from "../breadcrumb/types.js";
import { PanelErrorBoundary } from "../fallbacks/PanelErrorBoundary.js";
import { useConnector } from "../shell/ConnectorContext.js";
import { usePanelSessionRegistry } from "../shell/PanelSessionRegistry.js";
import { PanelContextProvider } from "./PanelContext.js";
import { type PanelSessionState, usePanelSession } from "./PanelSessionLifecycle.js";
import type { RefPanelParams } from "./RefPanelParams.js";
import { SessionUnavailable } from "./SessionUnavailable.js";

const DEFAULT_CHAIN_STATE: RefPanelChainState = {
	chain: [],
	activeIndex: -1,
	historyRoot: { kind: "roots" },
	storages: [],
};

export function PanelWrapper(props: IDockviewPanelProps<RefPanelParams>) {
	const { params, api } = props;
	const connector = useConnector();

	const state = usePanelSession({
		connector,
		sessionId: params.sessionId,
	});

	return <PanelWrapperByState state={state} api={api} />;
}

function PanelWrapperByState({
	state,
	api,
}: {
	readonly state: PanelSessionState;
	readonly api: DockviewPanelApi;
}) {
	// Register the live session with the shell's panel-session
	// registry so the `PanelViewerApi` impl can find it for
	// navigation events. No-op outside a shell (e.g. an isolated
	// test mount) — `usePanelSessionRegistry` returns null.
	const registry = usePanelSessionRegistry();
	useEffect(() => {
		if (state.status !== "live" || registry === null) return;
		return registry.register(api.id, state.session);
	}, [state, api.id, registry]);

	if (state.status === "pending") return <SessionPendingBody />;
	if (state.status === "unavailable") {
		return <SessionUnavailable sessionId={state.missingSessionId} />;
	}
	return (
		<FxtrSessionProvider session={state.session}>
			<PanelWrapperLive api={api} />
		</FxtrSessionProvider>
	);
}

function PanelWrapperLive({
	api,
}: {
	readonly api: DockviewPanelApi;
}) {
	const userStateAtom = useSessionRootStateAtom<RefPanelChainState>(DEFAULT_CHAIN_STATE);
	const userState = useAtomValue(userStateAtom);

	const { chain, activeIndex, historyRoot } = userState;
	const activeEntry = chain[activeIndex];

	// One storage slot per chain entry, projected by index. Called
	// unconditionally to satisfy the rules of hooks; the empty-chain
	// early return below renders without reading the projected atom,
	// so a stale `activeIndex === -1` projection is harmless.
	const storagesAtom = useSubAtom(userStateAtom, "storages");
	const storageAtom = useSubAtom(storagesAtom, activeIndex);

	// Chain entries carry the live stub directly — no
	// connector.entityRef reconstruction needed. Stubs in the chain
	// are connector-interned (either programmatically when the
	// shell seeds new panels, or via unescapeWire on snapshot
	// decode) and identical to what `connector.entityRef(shortId)`
	// would return.
	const concreteStub: EntityRefStub | ServiceRefStub | null = activeEntry?.stub ?? null;

	const breadcrumbSource = useMemo<BreadcrumbSource | null>(() => {
		if (activeEntry === undefined) return null;
		return {
			pendingBreadcrumbs: [],
			currentKey: activeEntry,
			via: "panel-ref",
		};
	}, [activeEntry]);

	const panelContextValue = useMemo(
		() => ({
			panelId: api.id,
			kind: "ref" as const,
			chain,
			activeIndex,
			historyRoot,
		}),
		[api.id, chain, activeIndex, historyRoot],
	);

	if (activeEntry === undefined || concreteStub === null) {
		return (
			<div className="fw-panel" style={panelStyle}>
				<PanelContextProvider value={panelContextValue}>
					<BreadcrumbBar
						panelId={api.id}
						chain={chain}
						activeIndex={activeIndex}
						historyRoot={historyRoot}
					/>
					<EmptyChainBody />
				</PanelContextProvider>
			</div>
		);
	}

	// `display: "metadata"` → render the hardcoded metadata view
	// (entity or service variant). Bypasses the consumer's dispatch
	// registry so the active stub is never tracked in the dispatcher
	// session — important for the workstation-debug surfaces, where
	// the dispatcher session itself appears as content.
	const isEntity = concreteStub instanceof EntityRefStub;
	const body =
		activeEntry.display === "metadata" ? (
			isEntity ? (
				<EntityMetadataView stub={concreteStub} storage={storageAtom} />
			) : (
				<ServiceMetadataView stub={concreteStub as ServiceRefStub} storage={storageAtom} />
			)
		) : (
			<Suspense fallback={<FallbackLoading />}>
				<RenderDispatch
					key={`${isEntity ? "entity" : "service"}:${concreteStub.shortId}`}
					as={FullPanel}
					stub={concreteStub}
					ownStorage={storageAtom}
				/>
			</Suspense>
		);

	return (
		<div className="fw-panel" style={panelStyle}>
			<PanelContextProvider value={panelContextValue}>
				<BreadcrumbBar
					panelId={api.id}
					chain={chain}
					activeIndex={activeIndex}
					historyRoot={historyRoot}
				/>
				<div style={bodyWrapperStyle}>
					<BreadcrumbSourceProvider value={breadcrumbSource}>
						<PanelErrorBoundary>{body}</PanelErrorBoundary>
					</BreadcrumbSourceProvider>
				</div>
			</PanelContextProvider>
		</div>
	);
}

function SessionPendingBody() {
	return <div style={statusBodyStyle}>Creating session…</div>;
}

function EmptyChainBody() {
	return <div style={statusBodyStyle}>This panel has no active ref yet.</div>;
}

function FallbackLoading() {
	return <div style={statusBodyStyle}>Loading…</div>;
}

const panelStyle: CSSProperties = {
	height: "100%",
	display: "flex",
	flexDirection: "column",
};

const bodyWrapperStyle: CSSProperties = {
	flex: "1 1 auto",
	minHeight: 0,
	display: "flex",
	flexDirection: "column",
};

const statusBodyStyle: CSSProperties = {
	padding: "var(--sp-5, 16px)",
	color: "var(--text-muted, #888)",
	fontFamily: "var(--font-prose)",
	fontSize: "var(--text-body-sm, 12px)",
};
