/**
 * ReportShell: the top-level scaffold for the report rendering system.
 *
 * Provides:
 * - Navigation context (breadcrumbs, navigateTo, navigateBack)
 * - Margin note sidebar portal target
 * - Layout (CSS Grid: breadcrumbs, main area, sidebar)
 * - Card list vs main view switching
 * - Annotation resolution via the annotationsFor callback
 *
 * Must be inside a <FxtrProvider> and <DispatchProvider>.
 */

import type { FxtrID, PromiseOrEager } from "@fxtr/core";
import { Eager } from "@fxtr/core";
import { Render } from "@fxtr/react-dispatch";
import {
	type CSSProperties,
	Suspense,
	useCallback,
	useEffect,
	useMemo,
	useReducer,
	useRef,
	useState,
} from "react";
import { flushSync } from "react-dom";
import { MainView, ReferenceLink, SummaryCard } from "./keys.js";
import { MarginNoteTargetContext } from "./marginNote.js";
import { type ReportNavigation, ReportNavigationContext, navigationReducer } from "./navigation.js";
import type { BreadcrumbEntry, ReportView } from "./types.js";
import { buildUrl, parseUrlState } from "./urlState.js";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ReportShellUrlSync {
	/**
	 * Callback to ensure an entity is loaded into the scope by its ID.
	 * Called when restoring navigation state from the URL (initial load
	 * or browser back/forward). Should fetch the entity data if needed.
	 * Return the ID if successful, null if the entity can't be found.
	 */
	tryRetrieveById: (entityId: FxtrID) => Promise<FxtrID | null>;
}

export interface ReportShellProps {
	/** Initial view to show before any navigation happens. */
	initialView: ReportView;
	/**
	 * Async function that returns annotation entity IDs for a given entity.
	 * Called whenever the main view entity changes.
	 */
	annotationsFor?: (entityId: FxtrID) => PromiseOrEager<FxtrID[]>;
	/**
	 * If provided, synchronize navigation state with the browser URL via
	 * pushState/replaceState. The entity ID becomes the URL path,
	 * and navState + breadcrumbs are stored in query parameters.
	 *
	 * Must provide `tryRetrieveById` so the shell can load entities
	 * referenced in the URL into the scope.
	 */
	syncUrl?: ReportShellUrlSync | undefined;
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const shellStyle: CSSProperties = {
	display: "grid",
	gridTemplateColumns: "1fr 280px",
	gridTemplateRows: "auto 1fr",
	gridTemplateAreas: '"breadcrumbs breadcrumbs" "main sidebar"',
	minHeight: "100vh",
	gap: "0",
};

const shellCardsStyle: CSSProperties = {
	display: "grid",
	gridTemplateColumns: "1fr",
	gridTemplateRows: "auto 1fr",
	gridTemplateAreas: '"breadcrumbs" "main"',
	minHeight: "100vh",
};

const breadcrumbsStyle: CSSProperties = {
	gridArea: "breadcrumbs",
	display: "flex",
	alignItems: "center",
	gap: "8px",
	padding: "8px 16px",
	borderBottom: "1px solid #e0e0e0",
	backgroundColor: "#fafafa",
	fontSize: "14px",
	flexWrap: "wrap",
	viewTransitionName: "breadcrumbs",
};

const mainStyle: CSSProperties = {
	gridArea: "main",
	padding: "24px 32px",
	overflow: "auto",
};

const sidebarStyle: CSSProperties = {
	gridArea: "sidebar",
	padding: "16px",
	borderLeft: "1px solid #e0e0e0",
	backgroundColor: "#fafafa",
	overflow: "auto",
	display: "flex",
	flexDirection: "column",
	gap: "12px",
	viewTransitionName: "sidebar",
};

const cardGridStyle: CSSProperties = {
	display: "grid",
	gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
	gap: "16px",
};

const breadcrumbSeparator: CSSProperties = {
	color: "#999",
	userSelect: "none",
};

const breadcrumbItemStyle: CSSProperties = {
	cursor: "pointer",
};

const breadcrumbCurrentStyle: CSSProperties = {
	fontWeight: 600,
};

// ---------------------------------------------------------------------------
// View transition helper
// ---------------------------------------------------------------------------

/**
 * Wrap a synchronous state update in a view transition if the browser supports it.
 * Uses `document.startViewTransition` to capture a snapshot of the old state,
 * then `flushSync` to commit the React update synchronously so the browser
 * can diff old vs new and animate.
 */
function withViewTransition(update: () => void): void {
	if (typeof document !== "undefined" && "startViewTransition" in document) {
		console.log("[vt] starting view transition");
		(document as { startViewTransition: (cb: () => void) => void }).startViewTransition(() => {
			console.log("[vt] inside callback, before flushSync");
			flushSync(update);
			// Log all elements with view-transition-name after DOM update
			const named = document.querySelectorAll("[style*='view-transition-name']");
			console.log(
				"[vt] after flushSync, elements with viewTransitionName:",
				Array.from(named).map((el) => (el as HTMLElement).style.viewTransitionName),
			);
		});
	} else {
		update();
	}
}

// ---------------------------------------------------------------------------
// ReportShell
// ---------------------------------------------------------------------------

export function ReportShell({ initialView, annotationsFor, syncUrl }: ReportShellProps) {
	const [navState, dispatch] = useReducer(navigationReducer, initialView, (view) => ({
		breadcrumbs: view.kind === "main" ? [{ entityId: view.rootEntity }] : [],
	}));
	const [urlReady, setUrlReady] = useState(!syncUrl);
	const [annotations, setAnnotations] = useState<FxtrID[]>([]);
	const marginNoteRef = useRef<HTMLDivElement | null>(null);
	// Track whether the current state change came from popstate (skip pushState)
	const isPopstateRef = useRef(false);

	// On mount, restore navigation state from URL (async — needs to fetch entities first)
	useEffect(() => {
		if (!syncUrl) return;
		const { tryRetrieveById } = syncUrl;
		let cancelled = false;

		async function restoreFromUrl() {
			const urlState = parseUrlState();
			if (!urlState.entityId) {
				if (!cancelled) setUrlReady(true);
				return;
			}

			// Fetch all referenced entities into the scope
			const allIds = [urlState.entityId, ...urlState.breadcrumbs.map((bc) => bc.entityId)];
			await Promise.all(allIds.map((id) => tryRetrieveById(id)));

			if (cancelled) return;

			// Build breadcrumb stack: history entries + current as top
			const entry: BreadcrumbEntry = { entityId: urlState.entityId };
			if (urlState.navState !== undefined) entry.navState = urlState.navState;
			dispatch({ type: "restore", breadcrumbs: [...urlState.breadcrumbs, entry] });
			setUrlReady(true);
		}

		restoreFromUrl().catch((err) => {
			console.error("Failed to restore from URL:", err);
			if (!cancelled) setUrlReady(true);
		});

		return () => {
			cancelled = true;
		};
	}, [syncUrl]);

	// Determine what to show: if there are breadcrumbs, show the top of the stack.
	// Otherwise show the initial view.
	const lastBreadcrumb = navState.breadcrumbs[navState.breadcrumbs.length - 1];
	const currentEntry: BreadcrumbEntry | null = lastBreadcrumb !== undefined ? lastBreadcrumb : null;

	const showingMainView = currentEntry !== null;

	// Sync navigation state → URL
	const prevBreadcrumbsRef = useRef(navState.breadcrumbs);
	useEffect(() => {
		if (!syncUrl || !urlReady) return;
		// Skip URL update if this state change came from popstate
		if (isPopstateRef.current) {
			isPopstateRef.current = false;
			prevBreadcrumbsRef.current = navState.breadcrumbs;
			return;
		}

		const prevBreadcrumbs = prevBreadcrumbsRef.current;
		prevBreadcrumbsRef.current = navState.breadcrumbs;

		const url = buildUrl(
			currentEntry?.entityId ?? null,
			currentEntry?.navState,
			// Breadcrumbs param = all entries except the current (top) one
			navState.breadcrumbs.slice(0, -1),
		);

		// Determine push vs replace:
		// - Same entity count and last entity same → replaceState (navState change)
		// - Otherwise → pushState
		const prevTop = prevBreadcrumbs[prevBreadcrumbs.length - 1];
		const curTop = navState.breadcrumbs[navState.breadcrumbs.length - 1];
		const sameTop = prevTop && curTop && prevTop.entityId.key() === curTop.entityId.key();
		const sameLength = prevBreadcrumbs.length === navState.breadcrumbs.length;

		if (sameTop && sameLength) {
			window.history.replaceState(null, "", url);
		} else {
			window.history.pushState(null, "", url);
		}
	}, [syncUrl, urlReady, navState.breadcrumbs, currentEntry]);

	// Listen for popstate (browser back/forward)
	useEffect(() => {
		if (!syncUrl) return;
		const { tryRetrieveById } = syncUrl;
		function onPopstate() {
			const urlState = parseUrlState();
			const entityId = urlState.entityId;
			if (!entityId) {
				isPopstateRef.current = true;
				dispatch({ type: "restore", breadcrumbs: [] });
				return;
			}

			// Fetch entities then restore state
			const allIds = [entityId, ...urlState.breadcrumbs.map((bc) => bc.entityId)];
			Promise.all(allIds.map((id) => tryRetrieveById(id))).then(
				() => {
					isPopstateRef.current = true;
					const entry: BreadcrumbEntry = { entityId };
					if (urlState.navState !== undefined) entry.navState = urlState.navState;
					dispatch({ type: "restore", breadcrumbs: [...urlState.breadcrumbs, entry] });
				},
				(err) => {
					console.error("Failed to restore from popstate:", err);
				},
			);
		}
		window.addEventListener("popstate", onPopstate);
		return () => window.removeEventListener("popstate", onPopstate);
	}, [syncUrl]);

	// Resolve annotations when the main view entity changes
	const currentEntityId = currentEntry?.entityId;
	useEffect(() => {
		if (!currentEntityId || !annotationsFor) {
			setAnnotations([]);
			return;
		}

		const result = annotationsFor(currentEntityId);
		if (result instanceof Eager) {
			try {
				setAnnotations(result.unwrap());
			} catch {
				setAnnotations([]);
			}
		} else {
			let cancelled = false;
			result.then(
				(ids) => {
					if (!cancelled) setAnnotations(ids);
				},
				() => {
					if (!cancelled) setAnnotations([]);
				},
			);
			return () => {
				cancelled = true;
			};
		}
	}, [currentEntityId, annotationsFor]);

	// Build the navigation context value
	const navigation: ReportNavigation = useMemo(
		() => ({
			navigateTo(entityId, navState) {
				const sameEntity = currentEntry && currentEntry.entityId.key() === entityId.key();
				if (sameEntity) {
					dispatch({ type: "navigate", entityId, navState });
				} else {
					withViewTransition(() => {
						dispatch({ type: "navigate", entityId, navState });
					});
				}
			},
			navigateBack(index) {
				withViewTransition(() => {
					dispatch({ type: "back", index });
				});
			},
			breadcrumbs: navState.breadcrumbs,
			currentEntry,
		}),
		[navState.breadcrumbs, currentEntry],
	);

	const handleBackToList = useCallback(() => {
		withViewTransition(() => {
			// Clear all breadcrumbs to return to initial view
			dispatch({ type: "back", index: -1 });
		});
	}, []);

	if (!urlReady) {
		return <div style={{ padding: "24px", fontSize: "16px" }}>Loading...</div>;
	}

	return (
		<ReportNavigationContext value={navigation}>
			<MarginNoteTargetContext value={marginNoteRef}>
				<div style={showingMainView ? shellStyle : shellCardsStyle}>
					{/* Breadcrumbs */}
					<div style={breadcrumbsStyle}>
						{showingMainView && initialView.kind === "cards" && (
							<>
								<span
									style={breadcrumbItemStyle}
									onClick={handleBackToList}
									onKeyDown={(e) => {
										if (e.key === "Enter") handleBackToList();
									}}
									role="button"
									tabIndex={0}
								>
									All Reports
								</span>
								{navState.breadcrumbs.length > 0 && <span style={breadcrumbSeparator}>›</span>}
							</>
						)}
						<Breadcrumbs
							entries={navState.breadcrumbs}
							onNavigateBack={(index) =>
								withViewTransition(() => dispatch({ type: "back", index }))
							}
						/>
					</div>

					{/* Main content area */}
					<div style={mainStyle}>
						<Suspense fallback={<div>Loading...</div>}>
							{currentEntry ? (
								<Render
									entity={currentEntry.entityId}
									as={MainView}
									navState={currentEntry.navState}
									annotations={annotations}
								/>
							) : initialView.kind === "cards" ? (
								<CardGrid entities={initialView.entities} />
							) : null}
						</Suspense>
					</div>

					{/* Margin notes sidebar (only in main view mode) */}
					{showingMainView && <div ref={marginNoteRef} style={sidebarStyle} />}
				</div>
			</MarginNoteTargetContext>
		</ReportNavigationContext>
	);
}

// ---------------------------------------------------------------------------
// Internal: Breadcrumbs
// ---------------------------------------------------------------------------

function Breadcrumbs({
	entries,
	onNavigateBack,
}: {
	entries: readonly BreadcrumbEntry[];
	onNavigateBack: (index: number) => void;
}) {
	return (
		<>
			{entries.map((entry, i) => {
				const isLast = i === entries.length - 1;
				return (
					<span key={`${i}-${entry.entityId.key()}`}>
						{i > 0 && <span style={breadcrumbSeparator}> › </span>}
						<span
							style={isLast ? breadcrumbCurrentStyle : breadcrumbItemStyle}
							onClickCapture={
								isLast
									? undefined
									: (e) => {
											// Capture phase: fire before the inner ReferenceLink's
											// onClick (which calls navigateTo) to prevent two
											// view transitions from firing.
											e.stopPropagation();
											onNavigateBack(i);
										}
							}
							onKeyDownCapture={
								isLast
									? undefined
									: (e) => {
											if (e.key === "Enter") {
												e.stopPropagation();
												onNavigateBack(i);
											}
										}
							}
							role={isLast ? undefined : "button"}
							tabIndex={isLast ? undefined : 0}
						>
							<Suspense fallback="...">
								<Render entity={entry.entityId} as={ReferenceLink} />
							</Suspense>
						</span>
					</span>
				);
			})}
		</>
	);
}

// ---------------------------------------------------------------------------
// Internal: Card grid
// ---------------------------------------------------------------------------

function CardGrid({ entities }: { entities: FxtrID[] }) {
	return (
		<div style={cardGridStyle}>
			{entities.map((id) => (
				<Suspense key={id.key()} fallback={<div>Loading card...</div>}>
					<Render entity={id} as={SummaryCard} />
				</Suspense>
			))}
		</div>
	);
}
