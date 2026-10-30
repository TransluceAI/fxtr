/**
 * Example custom components for MDX rendering.
 *
 * These demonstrate the main patterns:
 * 1. Components that flatten to primitives (server-side expansion)
 * 2. Components that emit client-side references
 * 3. Async components that fetch/compute data
 * 4. Fxtr-aware components that store entities and embed weak refs
 */

import { RawWeakFxtrID } from "@fxtr/core";
import type { FxtrScope } from "@fxtr/core";
import { type Component, type MdxChild, MdxNode, assertFxtrCborProps } from "@fxtr/mdx-transform";
import { Stats } from "./entities.js";

/**
 * A callout box that expands into primitive HTML-like nodes.
 *
 * Usage in MDX: <Callout type="warning">Watch out!</Callout>
 */
export const Callout: Component = (props) => {
	const { type = "info", children } = props;
	const childArray = Array.isArray(children) ? (children as MdxChild[]) : [children as MdxChild];
	return new MdxNode("div", { className: `callout callout-${type}` }, [
		new MdxNode("div", { className: "callout-icon" }, [
			type === "warning" ? "\u26a0\ufe0f" : "\u2139\ufe0f",
		]),
		new MdxNode("div", { className: "callout-content" }, childArray),
	]);
};

/**
 * A chart component that emits a client-side reference.
 * The server doesn't render this — the client handles it.
 *
 * Usage in MDX: <Chart type="bar" data={[1, 2, 3]} title="Revenue" />
 */
export const Chart: Component = (props) => {
	const { children: _children, ...chartProps } = props;
	return new MdxNode("Chart", assertFxtrCborProps(chartProps), []);
};

/**
 * A data table that mixes primitives with a client reference.
 *
 * Usage in MDX: <DataTable title="Users" endpoint="/api/users" columns={["name", "email"]} />
 */
export const DataTable: Component = (props) => {
	const { title, ...tableProps } = props;
	return new MdxNode("section", { className: "data-table-section" }, [
		...(title ? [new MdxNode("h3", {}, [title as string])] : []),
		new MdxNode("DataGrid", assertFxtrCborProps(tableProps), []),
	]);
};

/**
 * Fxtr-aware server macro: simulates async data fetching, stores the result
 * as a Stats entity in the scope, and embeds a weak reference in the tree.
 *
 * Usage in MDX: <FxtrStats timeout={50} fxtr_scope={props.fxtr_scope} />
 */
export const FxtrStats: Component = async (props) => {
	const scope = props.fxtr_scope as FxtrScope;
	const timeout = typeof props.timeout === "number" ? props.timeout : 10;

	// Simulate async data fetching (e.g. querying a database or API)
	await new Promise((resolve) => setTimeout(resolve, timeout));
	const mockData = { users: 1234, revenue: "$56,789", growth: "+12.3%" };

	// Store the fetched data as a fxtr entity
	const statsRef = await scope.store(Stats.create(mockData));

	// Emit a client-side FxtrStats node with just the entity reference.
	return new MdxNode("FxtrStats", { entityId: new RawWeakFxtrID(statsRef.entityId) }, []);
};

/**
 * Returns a components map suitable for passing to MDX's components prop.
 */
export function getComponents(): Record<string, Component> {
	return { Callout, Chart, DataTable, FxtrStats };
}
