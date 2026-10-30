/**
 * TS-side `ServiceProto` declaration for `fxtr_experiment`'s
 * `UserCapabilityProto` (the `experiment-workflow` capability) — the
 * subset the dashboard drives. Hand-mirrored from
 * `projects/fxtr_experiment/fxtr_experiment/user_api.py` (the
 * protocol's source of truth — further methods can be mirrored here
 * whenever the frontend grows a use for them); pinned live by the
 * dialect round-trip harness. Wire dispatch is by method name; args
 * are positional tuples + optional kwargs (matching the Python
 * signatures).
 *
 * The dashboard's READ path is not invokes at all: it subscribes to
 * the `watch_*` methods through the workstation inbox's
 * `subscribe_to(capability, method, args)` idiom (`watch_driver.ts`),
 * so those methods are deliberately not declared. Also undeclared: the
 * grant surface (`root` / `attach` / `attach_internals` — grants ride
 * the watch payloads instead), the one-shot introspection queries (the
 * watch mirrors serve those shapes), and the write/open/export surface
 * (programmatic-only in v0 per the monitoring note). `read_cell_entity`
 * IS declared: it is the user-demand channel behind the drawer's run
 * buttons.
 */

import type { Method } from "@fxtr-ui/core";

import type { DeleteReport, TargetRecord, ViewSel, ViewToken } from "./types.js";

/** Handle entity refs cross as stubs (the transport's ref leaves). */
import type { EntityRefStub } from "@fxtr-ui/core";

export type ExperimentWorkflowProto = {
	// -- the demand channel (user demands require attached targets) --
	readonly read_cell_entity: Method<[ViewSel], Record<string, never>, EntityRefStub>;

	// -- mutations (the v0 dashboard surface) --
	readonly add_target: Method<[ViewSel], { readonly adopt?: boolean }, null>;
	readonly remove_target: Method<[ViewSel], Record<string, never>, null>;
	readonly demand_target: Method<
		[TargetRecord],
		Record<string, never>,
		Readonly<Record<string, EntityRefStub>>
	>;
	readonly clear_cells: Method<[ViewSel], Record<string, never>, null>;
	readonly delete_cell: Method<[ViewSel], Record<string, never>, boolean>;
	readonly delete_grid: Method<[ViewToken, string], { readonly recursive?: boolean }, DeleteReport>;
	readonly source_grid: Method<[ViewSel, string], Record<string, never>, null>;
	readonly source_cell: Method<[ViewSel], Record<string, never>, null>;
	readonly revoke_grid_sourcing: Method<[ViewSel, string], { readonly force?: boolean }, boolean>;
	readonly revoke_cell_sourcing: Method<[ViewSel], Record<string, never>, boolean>;
};
