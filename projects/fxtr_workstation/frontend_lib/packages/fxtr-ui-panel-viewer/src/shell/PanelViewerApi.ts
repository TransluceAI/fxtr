/**
 * Workstation navigation API — the three methods the shell
 * exposes to its descendants.
 *
 * Semantic split:
 *
 *   - `followLink(event)` — the user followed a content ref.
 *     Clicks, right-clicks, and drag-drops of `RefLink` all fit
 *     this shape. Owns chain-mutation logic
 *     (`replace-in-source`) and trail-to-chain derivation for
 *     `open-new`. Requires a source ref panel; callers without
 *     one (e.g. roots-panel rows) route through
 *     `restoreIntoNewPanel` instead.
 *
 *   - `activateChainEntry(panelId, chainIndex)` — cheap in-place
 *     shift of an existing panel's active position. No new
 *     panel, no chain mutation, no breadcrumb emission. Used by
 *     breadcrumb-bar entry clicks.
 *
 *   - `restoreIntoNewPanel(state, position?)` — reopen a fully-
 *     resolved chain state in a fresh panel. Used by breadcrumb
 *     drags, history-root menu entries, and any non-panel-origin
 *     click/drag that lands on a blank chain state (e.g. roots-
 *     panel rows via `computeSnapshotFromTrail`).
 *
 * The interface is typed only here; the implementation lives in
 * `PanelViewer`. Descendants that try to use
 * `usePanelViewer()` outside a shell-mounted tree will throw.
 */

import type { DockviewGroupPanel } from "dockview";
import type { Breadcrumb, RefPanelChainState } from "../breadcrumb/types.js";
import type { PanelViewEntry } from "../types.js";

/** Where dockview should place a newly-opened panel. */
export interface DropPosition {
	readonly referenceGroup?: DockviewGroupPanel | undefined;
	readonly direction?: "left" | "right" | "above" | "below" | undefined;
}

/**
 * Intent encoded in a `FollowLinkEvent`.
 *
 *   - `replace-in-source`: mutate the source panel's chain in
 *     place. The shell looks up the source panel, runs
 *     `computeNextChainState`, and writes the new chain state
 *     into the panel's session userState.
 *   - `open-new`: branch into a fresh panel whose chain /
 *     historyRoot are derived from the source panel's state +
 *     the trail.
 */
export type FollowLinkIntent =
	| { readonly kind: "replace-in-source" }
	| { readonly kind: "open-new"; readonly position?: DropPosition | undefined };

export interface FollowLinkEvent {
	readonly target: PanelViewEntry;
	readonly trail: ReadonlyArray<Breadcrumb>;
	/**
	 * The dockview panel id the link was followed from. Must
	 * refer to an existing ref panel; interaction hooks enforce
	 * this by routing non-panel origins through
	 * `restoreIntoNewPanel` instead.
	 */
	readonly sourcePanelId: string;
	readonly intent: FollowLinkIntent;
}

export interface PanelViewerApi {
	followLink(event: FollowLinkEvent): void;
	activateChainEntry(panelId: string, chainIndex: number): void;
	restoreIntoNewPanel(state: RefPanelChainState, position?: DropPosition): void;
	/**
	 * Open (or focus an existing) registered panel by its
	 * registration `id` — the generic, registry-driven counterpart to
	 * the ref-panel navigation methods above. Find-or-focus semantics:
	 *
	 *   - Singleton registrations (no `forSession`) are keyed by `id`,
	 *     so at most one is ever open.
	 *   - `forSession` registrations are keyed by `<id>-<sessionId>`;
	 *     the `sessionId` is read from `params.sessionId`.
	 *
	 * `params` is passed straight through to the panel as its dockview
	 * params; the viewer only inspects `params.sessionId` (for
	 * `forSession` keying) and hands `params` to the registration's
	 * `title` deriver. Used for programmatic opens from one panel to
	 * another — e.g. the workstation-debug panel opening a per-session
	 * debug panel, or the dispatcher-debug panel.
	 */
	openPanel(id: string, params?: Record<string, unknown>, position?: DropPosition): void;
	/**
	 * Close a panel by id. For ref panels, also terminates the
	 * backing `WorkstationSession` server-side before removing the
	 * dockview panel — wired up to the tab close X (via
	 * `closeActionOverride`) and the open-panels sidebar's close
	 * row so user-initiated closes don't leave orphan sessions.
	 *
	 * Idempotent. Non-ref panels (roots / workstation_debug /
	 * session_debug) have no session to terminate, so this is just
	 * a `removePanel` for them.
	 */
	closePanel(panelId: string): void;
}
