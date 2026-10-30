/**
 * Dockview component id + canonical panel id for the namespace panel.
 *
 * Only one namespace panel exists at a time; `NAMESPACE_PANEL_ID` is
 * the canonical id used by the shell to find-or-create it, and
 * `onDidRemovePanel` watches for this id to auto-reopen it.
 */

export const PANEL_COMPONENT_NAMESPACE = "namespace";
export const NAMESPACE_PANEL_ID = "namespace-main";
