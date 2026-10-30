// Dispatch keys
export { MainView, FigureBlock, ReferenceLink, MarginNote, SummaryCard } from "./keys.js";

// Navigation
export {
	type ReportNavigation,
	useReportNavigation,
} from "./navigation.js";

// Margin note portal
export { AttachMarginNote } from "./marginNote.js";

// Types
export type { BreadcrumbEntry, JsonValue, ReportView } from "./types.js";

// View transition name utility
export { vtName } from "./viewTransitionName.js";

// Shell
export { ReportShell, type ReportShellProps } from "./ReportShell.js";
