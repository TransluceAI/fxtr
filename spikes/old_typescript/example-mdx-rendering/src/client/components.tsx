/**
 * Client-side component registry.
 *
 * When the component tree contains a node with a custom type (e.g. "Chart", "DataGrid",
 * "FxtrStats"), the renderer looks up the type here. Registered components receive the
 * node's props (FxtrCbor values) and can render themselves using React.
 *
 * The FxtrStats component demonstrates fxtr integration: it extracts a RawWeakFxtrID
 * from props, creates an EntityRef, and uses useFxtrLoad to load the entity data.
 */

import { EntityRef, type FxtrCbor, RawWeakFxtrID } from "@fxtr/core";
import { useFxtrLoad } from "@fxtr/react";
import { type ReactNode, Suspense } from "react";
import { type Stats, Stats as StatsCodec } from "../entities.js";

interface ClientComponentProps {
	type: string;
	props: { readonly [key: string]: FxtrCbor };
	jsonChildren: readonly FxtrCbor[];
	renderChild: (child: FxtrCbor, key: number) => ReactNode;
}

type ClientComponent = (props: ClientComponentProps) => ReactNode;

function PropLine({ label, value }: { label: string; value: unknown }) {
	return (
		<div className="client-prop">
			<strong>{label}:</strong> {typeof value === "string" ? value : JSON.stringify(value)}
		</div>
	);
}

function Chart({ props }: ClientComponentProps) {
	return (
		<div className="client-component client-chart">
			<div className="client-component-label">Chart</div>
			<div className="client-component-props">
				{props.title != null && <PropLine label="title" value={props.title} />}
				{props.type != null && <PropLine label="chartType" value={props.type} />}
				{props.data != null && <PropLine label="data" value={props.data} />}
				{props.labels != null && <PropLine label="labels" value={props.labels} />}
			</div>
		</div>
	);
}

function DataGrid({ props }: ClientComponentProps) {
	return (
		<div className="client-component client-datagrid">
			<div className="client-component-label">DataGrid</div>
			<div className="client-component-props">
				{props.endpoint != null && <PropLine label="endpoint" value={props.endpoint} />}
				{props.columns != null && <PropLine label="columns" value={props.columns} />}
			</div>
		</div>
	);
}

/**
 * Inner component that calls useFxtrLoad (must be inside Suspense).
 */
function FxtrStatsInner({ entityRef }: { entityRef: EntityRef<Stats> }) {
	const stats = useFxtrLoad(entityRef);
	return (
		<div className="stats-grid">
			<div className="stat-card">
				<span className="stat-value">{stats.users}</span>
				<span className="stat-label">users</span>
			</div>
			<div className="stat-card">
				<span className="stat-value">{stats.revenue}</span>
				<span className="stat-label">revenue</span>
			</div>
			<div className="stat-card">
				<span className="stat-value">{stats.growth}</span>
				<span className="stat-label">growth</span>
			</div>
		</div>
	);
}

/**
 * Fxtr-aware client component. Extracts the entity ID from props,
 * creates an EntityRef with the Stats codec, and loads it via useFxtrLoad.
 */
function FxtrStats({ props }: ClientComponentProps) {
	const rawId = props.entityId;
	if (!(rawId instanceof RawWeakFxtrID)) {
		return <div className="error">FxtrStats: missing entityId</div>;
	}
	const entityRef = new EntityRef<Stats>(rawId.innerId, StatsCodec);

	return (
		<Suspense fallback={<div className="stats-grid loading">Loading stats...</div>}>
			<FxtrStatsInner entityRef={entityRef} />
		</Suspense>
	);
}

/**
 * Registry of client-side components.
 */
export const clientComponents: Record<string, ClientComponent> = {
	Chart,
	DataGrid,
	FxtrStats,
};
