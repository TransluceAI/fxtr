/**
 * Grid of dispatch-rendered cards. Reads
 * `connector.listEntities()` and renders each as a
 * `<RenderDispatch as={CardKey} stub=…/>`. Same shape as the
 * sibling `dispatch-demo`'s grid, just minus the services side —
 * the dynamic dispatcher handles services symmetrically, but the
 * demo backend doesn't register any.
 */

import type { EntityRecord, WorkstationConnector } from "@fxtr-ui/connector-workstation";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { useAtomValue } from "jotai";
import { useMemo } from "react";
import { CardKey } from "./dispatch_keys";

export function DispatchGrid({ connector }: { readonly connector: WorkstationConnector }) {
	const entityList = useMemo(() => connector.listEntities(), [connector]);
	const entityRecords = useAtomValue(entityList.records) as readonly EntityRecord[];

	return (
		<div style={{ padding: "12px", display: "flex", flexDirection: "column", gap: "8px" }}>
			<div style={{ fontSize: 12, color: "#666", marginBottom: 4 }}>
				Entities ({entityRecords.length})
			</div>
			{entityRecords.length === 0 ? (
				<div style={{ color: "#888", fontStyle: "italic", padding: "8px" }}>
					(no entities yet — the demo backend should be seeding a few)
				</div>
			) : null}
			{entityRecords.map((record) => (
				<RenderDispatch
					key={`entity:${record.short_id}`}
					as={CardKey}
					stub={connector.entityRef(record.short_id)}
				/>
			))}
		</div>
	);
}
