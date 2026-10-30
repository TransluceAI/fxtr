/**
 * Workstation navigation API — the three methods the shell exposes to
 * its descendants.
 *
 * Semantic split:
 *
 * - `followLink(event)` — the user followed a content ref. Clicks,
 *   right-clicks, and drag-drops of `EntityLink` all fit this shape.
 *   Owns chain-mutation logic (`replace-in-source`) and trail-to-chain
 *   derivation for `open-new`. Requires a source entity panel; callers
 *   without one (e.g. namespace-panel rows) route through
 *   `restoreIntoNewPanel` instead.
 *
 * - `activateChainEntry(panelId, chainIndex)` — cheap in-place shift
 *   of an existing panel's active position. No new panel, no chain
 *   mutation, no breadcrumb emission. Used by breadcrumb-bar entry
 *   clicks.
 *
 * - `restoreIntoNewPanel(params, position?)` — reopen a fully-resolved
 *   state snapshot in a fresh panel. Used by breadcrumb drags,
 *   history-root menu entries, and any non-panel-origin click/drag
 *   that lands on a blank `EntityPanelParams` (e.g. namespace pin
 *   rows via `computeSnapshotFromTrail`).
 *
 * See `plans/phase0/step6_breadcrumbs.md` for the rationale and the
 * per-interaction dispatch table.
 */

import type { FxtrID } from "@fxtr/core";
import type { DockviewGroupPanel } from "dockview";
import type { Breadcrumb, EntityPanelParams } from "../breadcrumb/types.js";

/** Where dockview should place a newly-opened panel. */
export interface DropPosition {
	referenceGroup?: DockviewGroupPanel | undefined;
	direction?: "left" | "right" | "above" | "below" | undefined;
}

/**
 * Intent encoded in a `FollowLinkEvent`.
 *
 * - `replace-in-source`: mutate the source panel's chain in place.
 *   The shell looks up the source panel, runs `computeNextChainState`,
 *   and `updateParameters`.
 * - `open-new`: branch into a fresh panel whose chain/historyRoot are
 *   derived from the source panel's state + the trail.
 */
export type FollowLinkIntent =
	| { kind: "replace-in-source" }
	| { kind: "open-new"; position?: DropPosition | undefined };

export interface FollowLinkEvent {
	target: FxtrID;
	trail: Breadcrumb[];
	/**
	 * The dockview panel id the link was followed from. Must refer to
	 * an existing entity panel; interaction hooks enforce this by
	 * routing non-panel origins through `restoreIntoNewPanel` instead.
	 */
	sourcePanelId: string;
	intent: FollowLinkIntent;
}

export interface WorkstationApi {
	followLink(event: FollowLinkEvent): void;
	activateChainEntry(panelId: string, chainIndex: number): void;
	restoreIntoNewPanel(params: EntityPanelParams, position?: DropPosition): void;
}
