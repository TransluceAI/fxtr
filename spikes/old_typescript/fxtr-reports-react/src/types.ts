/**
 * Shared types for the report rendering system.
 */

import type { FxtrID } from "@fxtr/core";

/** JSON-serializable value. Used for navState so it can be round-tripped through URL query params. */
export type JsonValue =
	| string
	| number
	| boolean
	| null
	| JsonValue[]
	| { [key: string]: JsonValue };

/** A single entry in the navigation breadcrumb stack. */
export interface BreadcrumbEntry {
	entityId: FxtrID;
	navState?: JsonValue | undefined;
}

/** The two view modes the report shell supports. */
export type ReportView =
	| { kind: "cards"; entities: FxtrID[] }
	| { kind: "main"; rootEntity: FxtrID };
