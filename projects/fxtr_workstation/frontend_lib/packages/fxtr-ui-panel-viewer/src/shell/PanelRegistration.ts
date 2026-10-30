/**
 * `PanelRegistration` — the injected description of a non-ref dockview
 * panel (roots, debug surfaces, …).
 *
 * The panel viewer's core is connector-agnostic and owns only the
 * built-in **ref panel** (`PanelWrapper`). Every other panel is supplied
 * by the app as a registration: the viewer registers its component with
 * dockview, renders a sidebar button for it (if `sidebarButton` is set),
 * and exposes a generic `openPanel(id, params?)` so one panel can open
 * another programmatically.
 *
 * (Lives in `@fxtr-ui/workstation` today; moves to `@fxtr-ui/panel-viewer`
 * with the rest of the shell mechanics.)
 */

import type { IDockviewPanelProps } from "dockview";
import type { FunctionComponent } from "react";

export interface PanelRegistration {
	/**
	 * Dockview component key. Also the panel id for singleton panels
	 * (those without `forSession`); per-session panels derive their id
	 * from this plus the session id.
	 */
	readonly id: string;
	/**
	 * Tab title for panels of this kind. Either a static string, or a
	 * function deriving the title from the open params (e.g. a
	 * per-session panel labelling its tab from `params.sessionId`). The
	 * derivation lives at the registration site so the generic viewer
	 * never needs to know a panel's params shape.
	 */
	readonly title: string | ((params: Record<string, unknown>) => string);
	/**
	 * The dockview content component. Closes over the app's connector /
	 * dispatcher as needed (the viewer passes only dockview's
	 * `IDockviewPanelProps`).
	 */
	// biome-ignore lint/suspicious/noExplicitAny: dockview erases panel param shapes
	readonly component: FunctionComponent<IDockviewPanelProps<any>>;
	/**
	 * When set, the sidebar shows a find-or-open button with this label
	 * (`+ <label>`). Omit for panels opened only programmatically (e.g. a
	 * per-session debug panel opened from a row button).
	 */
	readonly sidebarButton?: { readonly label: string };
	/**
	 * A per-session panel variant: at most one open per session id, keyed
	 * `<id>-<sessionId>`. Opened via `openPanel(id, params)` where `params`
	 * carries a `sessionId` string — the viewer reads it to compute the
	 * keyed dockview id. (Singleton panels — `forSession` unset — are keyed
	 * by `id` alone.)
	 */
	readonly forSession?: boolean;
}
