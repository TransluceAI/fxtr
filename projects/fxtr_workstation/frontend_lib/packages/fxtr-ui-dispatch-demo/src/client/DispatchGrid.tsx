/**
 * Grid of dispatch-rendered cards. Reads
 * `connector.listEntities()` and `connector.listServices()`,
 * and renders each as a `<RenderDispatch as={CardKey} stub=…/>`
 * card. Each card type-resolves independently via the
 * dispatcher.
 *
 * To demonstrate the shared-state aspect, every card is
 * rendered TWICE in two side-by-side columns. The same
 * `(stub, CardKey)` pair → same dispatch entry → same shared
 * atom. Service cards' `lastInvoke` slot is therefore visible
 * from both copies (clicking "+1" on the left counter card
 * updates the right one too).
 */

import type {
	EntityRecord,
	ServiceRecord,
	WorkstationConnector,
} from "@fxtr-ui/connector-workstation";
import { RenderDispatch } from "@fxtr-ui/react-dispatch";
import { useAtomValue } from "jotai";
import { useMemo } from "react";
import { CardKey } from "./dispatch_keys";

export function DispatchGrid({ connector }: { readonly connector: WorkstationConnector }) {
	const entityList = useMemo(() => connector.listEntities(), [connector]);
	const serviceList = useMemo(() => connector.listServices(), [connector]);
	const entityRecords = useAtomValue(entityList.records) as readonly EntityRecord[];
	const serviceRecords = useAtomValue(serviceList.records) as readonly ServiceRecord[];

	return (
		<div style={{ padding: "12px", display: "flex", flexDirection: "column", gap: "16px" }}>
			<div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }}>
				<DispatchColumn
					label="mount A"
					connector={connector}
					entities={entityRecords}
					services={serviceRecords}
				/>
				<DispatchColumn
					label="mount B"
					connector={connector}
					entities={entityRecords}
					services={serviceRecords}
				/>
			</div>
		</div>
	);
}

function DispatchColumn({
	label,
	connector,
	entities,
	services,
}: {
	readonly label: string;
	readonly connector: WorkstationConnector;
	readonly entities: readonly EntityRecord[];
	readonly services: readonly ServiceRecord[];
}) {
	return (
		<div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
			<div
				style={{
					fontSize: "11px",
					color: "#888",
					textTransform: "uppercase",
					letterSpacing: "0.05em",
				}}
			>
				{label}
			</div>
			{entities.length === 0 && services.length === 0 ? (
				<div style={{ color: "#888", fontStyle: "italic", padding: "8px" }}>
					(no entities or services)
				</div>
			) : null}
			{entities.map((record) => (
				<RenderDispatch
					key={`entity:${record.short_id}`}
					as={CardKey}
					stub={connector.entityRef(record.short_id)}
				/>
			))}
			{services.map((record) => (
				<RenderDispatch
					key={`service:${record.short_id}`}
					as={CardKey}
					stub={connector.serviceRef(record.short_id)}
				/>
			))}
		</div>
	);
}
