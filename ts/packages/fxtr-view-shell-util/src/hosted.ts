/** Messages between a hub page and its sandboxed viewer. This entry has no runtime dependencies. */

export const READY_MESSAGE = "fxtr-viewer-ready";
export const CONFIG_MESSAGE = "fxtr-viewer-config";
export const ACTION_MESSAGE = "fxtr-viewer-action";
/** The frame has something to show: the job, or why it has none. Until then, the page around it
 * shows its own message. */
export const SHOWN_MESSAGE = "fxtr-viewer-shown";

/** The configuration a parent sends to its viewer; URLs resolve on the viewer's origin. */
export interface HostedConfig {
  readonly apiBase: string;
  readonly token: string;
  readonly jobId: string;
  readonly bundle: { readonly baseUrl: string } | null;
  /** Comparison of recorded build and job commits; unknown when build provenance is absent. */
  readonly bundleNote: "commit" | "different-commit" | "unknown-commit" | null;
}

/** The token response, including metadata used only by the parent page. */
export interface ViewerToken extends HostedConfig {
  readonly expiresInSeconds: number;
  readonly sourceBase: string | null;
}
