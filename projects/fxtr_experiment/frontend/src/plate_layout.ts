/**
 * ELK layout for the plate view (design: `../notes/plate_view.md`).
 * Turns the plate forest + the classified relations into positioned boxes
 * and routed edges the SVG renderer (`plate_view.tsx`) draws.
 *
 * The plate forest (`derive/plates.ts` `buildPlateForest`) is the
 * containment tree with grant-class **plate containers** spliced in
 * laminarly; it is handed to ELK as **hard compound nesting**, so every
 * plate is a real container box ELK lays its members inside (non-members
 * stay outside — the guarantee a post-hoc bounding box could not give).
 * Edges live on the root graph with `INCLUDE_CHILDREN`, so a dependency
 * crossing a container boundary routes correctly. A grant-class split
 * across parents becomes several plate boxes tied by a **rail node** — a
 * small synthetic ELK node (labeled and colored like the class's plates)
 * with a dotted tie edge into the top of each box. Because the rail is a
 * real node and the ties are real edges, ELK places them clash-free.
 *
 * Layout runs in the browser only (ELK spins a bundled worker); the pure
 * derivations it consumes (`derive/plates.ts`) are what the unit tests
 * cover.
 */

import ELK, { type ElkExtendedEdge, type ElkNode } from "elkjs/lib/elk.bundled.js";

import {
	type GrantClass,
	type PlateForestNode,
	type RelationClassification,
	buildPlateForest,
} from "./derive/plates.js";
import type { GridTree, RollupCounts } from "./derive/tree.js";
import type { GridSummary } from "./wire/types.js";

export type EdgeStyle = "grant-held" | "grant-used" | "read" | "write" | "create" | "rail";

export type Pt = { readonly x: number; readonly y: number };

export type PositionedNode = {
	readonly path: string;
	readonly key: string;
	readonly x: number;
	readonly y: number;
	readonly width: number;
	readonly height: number;
	/** A subtree / task grid with children (drawn as a plain container). */
	readonly container: boolean;
	readonly summary: GridSummary | null;
	readonly internalsOf: string | null;
	readonly axes: readonly string[];
	readonly agg: RollupCounts;
};

/** A synthetic box laid out by ELK: an axis plate (``"class"``, tight
 * around its contents), a task's internals region (``"internals"``), or a
 * split class's **rail node** (``"rail"`` — a childless anchor tied by
 * dotted edges to each of the class's plate boxes). */
export type PositionedPlate = {
	readonly id: string;
	readonly variant: "class" | "internals" | "rail";
	readonly classKey: string;
	readonly label: string;
	readonly x: number;
	readonly y: number;
	readonly width: number;
	readonly height: number;
};

export type PositionedEdge = {
	readonly id: string;
	readonly style: EdgeStyle;
	readonly label: string;
	/** The grant-class of a ``"rail"`` tie (colors the edge); `""` else. */
	readonly classKey: string;
	readonly points: readonly Pt[];
};

export type PlateLayout = {
	readonly nodes: readonly PositionedNode[];
	readonly plates: readonly PositionedPlate[];
	readonly edges: readonly PositionedEdge[];
	readonly width: number;
	readonly height: number;
};

const LEAF_W = 184;
const LEAF_H = 70;
const RAIL_H = 26;

const elk = new ELK();

/** Per synthetic container, the port ids to create on each side — **one
 * port per edge** (not a shared port), so ELK spreads them along the
 * border like the edges on any other node. Internals containers take
 * WEST/EAST ports (their self-internals edges); a split class's plate
 * boxes take a NORTH port (the rail tie into the box top). */
type PortNeed = Map<string, { west: string[]; east: string[]; north: string[] }>;

function sidePorts(need: { west: string[]; east: string[]; north: string[] }) {
	return (["west", "east", "north"] as const).flatMap((side) =>
		need[side].map((pid) => ({
			id: pid,
			width: 5,
			height: 5,
			layoutOptions: { "elk.port.side": side.toUpperCase() },
		})),
	);
}

function toElk(node: PlateForestNode, portNeed: PortNeed): ElkNode {
	const children = node.children.map((c) => toElk(c, portNeed));
	const id = node.kind === "grid" ? node.path : node.id;
	if (children.length === 0) return { id, width: LEAF_W, height: LEAF_H };
	const elkNode: ElkNode = {
		id,
		children,
		// Room at the top for the container header (grid key + axes, or the
		// plate's axis label), and enough all round for edges to route.
		layoutOptions: { "elk.padding": "[top=42,left=18,bottom=18,right=18]" },
	};
	// A task's internals container gets a port per self-internals edge
	// (WEST for the writes/creates the task sends in, EAST for the reads
	// it takes out); a split class's plate box gets a NORTH port per rail
	// tie. ELK routes each to the box border (obstacle-avoiding) and
	// spreads them along the side.
	const need = node.kind === "plate" ? portNeed.get(id) : undefined;
	if (need !== undefined) {
		elkNode.ports = sidePorts(need);
		elkNode.layoutOptions = { ...elkNode.layoutOptions, "elk.portConstraints": "FIXED_SIDE" };
	}
	return elkNode;
}

export async function layoutPlates(
	tree: GridTree,
	relations: RelationClassification,
	classes: readonly GrantClass[],
): Promise<PlateLayout> {
	const forest = buildPlateForest(tree, classes);
	const plateMeta = new Map<
		string,
		{ variant: "class" | "internals" | "rail"; classKey: string; label: string }
	>();
	// Node id → parent node id (`"root"` for top-level) over the plate
	// forest — the hierarchy elkjs reports edge coordinates relative to.
	const parentOf = new Map<string, string>();
	const indexForest = (node: PlateForestNode, parentId: string): void => {
		const id = node.kind === "grid" ? node.path : node.id;
		parentOf.set(id, parentId);
		if (node.kind === "plate") {
			plateMeta.set(id, { variant: node.variant, classKey: node.classKey, label: node.label });
		}
		for (const c of node.children) indexForest(c, id);
	};
	for (const r of forest.roots) indexForest(r, "root");

	const exists = tree.byPath;

	const edges: { id: string; sources: string[]; targets: string[] }[] = [];
	const styleOf = new Map<string, { style: EdgeStyle; label: string; classKey: string }>();
	const edgeEnds = new Map<string, { src: string; dst: string }>();
	const portNeed: PortNeed = new Map();
	let n = 0;
	const need = (cid: string): { west: string[]; east: string[]; north: string[] } => {
		let e = portNeed.get(cid);
		if (e === undefined) {
			e = { west: [], east: [], north: [] };
			portNeed.set(cid, e);
		}
		return e;
	};

	const addEdge = (src: string, dst: string, style: EdgeStyle, label: string): void => {
		if (!exists.has(src) || !exists.has(dst)) return; // dangling endpoint (skew)
		const id = `e${n++}`;
		let es = src;
		let ed = dst;
		// A task ↔ its own internals edge routes to a dedicated port on the
		// internals box (WEST for the writes/creates the task sends in, EAST
		// for the reads it takes out) — one port per edge so they spread.
		const s = exists.get(src);
		const d = exists.get(dst);
		if (
			s?.summary?.kind === "task" &&
			d?.internalsOf === src &&
			plateMeta.has(`internals:${src}`)
		) {
			const port = `internals:${src}:W:${id}`;
			need(`internals:${src}`).west.push(port);
			parentOf.set(port, `internals:${src}`);
			es = port;
		} else if (
			d?.summary?.kind === "task" &&
			s?.internalsOf === dst &&
			plateMeta.has(`internals:${dst}`)
		) {
			const port = `internals:${dst}:E:${id}`;
			need(`internals:${dst}`).east.push(port);
			parentOf.set(port, `internals:${dst}`);
			ed = port;
		}
		edges.push({ id, sources: [es], targets: [ed] });
		styleOf.set(id, { style, label, classKey: "" });
		edgeEnds.set(id, { src: es, dst: ed }); // ELK endpoints (port ids included) for LCA
	};
	// Direction convention: reads/grants point INTO the task; writes /
	// creations OUT of it (data flows left→right).
	for (const g of relations.grants) {
		addEdge(
			g.target_path,
			g.task_path,
			g.port === "used" ? "grant-used" : "grant-held",
			g.is_task_fn ? "fn" : g.arg_name,
		);
	}
	for (const e of relations.reads) addEdge(e.subject_path, e.observer_path, "read", "");
	for (const e of relations.writes) addEdge(e.observer_path, e.subject_path, "write", "");
	for (const e of relations.creates) addEdge(e.observer_path, e.subject_path, "create", "");

	// A split (non-laminar) class gets a **rail node**: a small root-level
	// ELK node identifying the class, with a dotted tie edge from an
	// EAST-side port to a NORTH-side port on each of the class's plate
	// boxes. Both ends are real layout objects, so ELK keeps rails apart
	// and routes the ties clash-free (the point of not drawing rails as a
	// post-hoc overlay band).
	const railNodes: ElkNode[] = [];
	for (const [classKey, ids] of forest.railsByClass) {
		if (ids.length < 2) continue;
		const railId = `rail:${classKey}`;
		const label = plateMeta.get(ids[0] as string)?.label ?? "";
		plateMeta.set(railId, { variant: "rail", classKey, label });
		parentOf.set(railId, "root");
		const railPorts = need(railId);
		for (const boxId of ids) {
			const id = `e${n++}`;
			const srcPort = `${railId}:E:${id}`;
			railPorts.east.push(srcPort);
			parentOf.set(srcPort, railId);
			const dstPort = `${boxId}:N:${id}`;
			need(boxId).north.push(dstPort);
			parentOf.set(dstPort, boxId);
			edges.push({ id, sources: [srcPort], targets: [dstPort] });
			styleOf.set(id, { style: "rail", label: "", classKey });
			edgeEnds.set(id, { src: srcPort, dst: dstPort });
		}
		railNodes.push({
			id: railId,
			width: Math.max(64, label.length * 7 + 24),
			height: RAIL_H,
			ports: sidePorts(railPorts),
			layoutOptions: { "elk.portConstraints": "FIXED_SIDE" },
		});
	}

	const root: ElkNode = {
		id: "root",
		layoutOptions: {
			"elk.algorithm": "layered",
			"elk.direction": "RIGHT",
			"elk.hierarchyHandling": "INCLUDE_CHILDREN",
			"elk.layered.spacing.nodeNodeBetweenLayers": "84",
			"elk.spacing.nodeNode": "36",
			"elk.spacing.edgeNode": "18",
			"elk.layered.spacing.edgeNodeBetweenLayers": "18",
			"elk.padding": "[top=12,left=12,bottom=12,right=12]",
		},
		children: [...forest.roots.map((r) => toElk(r, portNeed)), ...railNodes],
		edges,
	};

	const laid = await elk.layout(root);

	const nodes: PositionedNode[] = [];
	const plates: PositionedPlate[] = [];
	// Every node's absolute top-left (grids, plate containers, root) — the
	// origins edge coordinates are offset by (elkjs reports each edge's
	// sections relative to the LCA of its endpoints).
	const absOrigin = new Map<string, Pt>([["root", { x: 0, y: 0 }]]);
	const elkEdges = new Map<string, ElkExtendedEdge>();
	const walk = (elkNode: ElkNode, ox: number, oy: number): void => {
		const x = ox + (elkNode.x ?? 0);
		const y = oy + (elkNode.y ?? 0);
		absOrigin.set(elkNode.id, { x, y });
		const width = elkNode.width ?? LEAF_W;
		const height = elkNode.height ?? LEAF_H;
		const meta = plateMeta.get(elkNode.id);
		if (meta !== undefined) {
			plates.push({ id: elkNode.id, ...meta, x, y, width, height });
		} else {
			const tn = exists.get(elkNode.id);
			if (tn !== undefined) {
				nodes.push({
					path: elkNode.id,
					key: tn.key,
					x,
					y,
					width,
					height,
					container: (elkNode.children?.length ?? 0) > 0,
					summary: tn.summary,
					internalsOf: tn.internalsOf,
					axes: tn.summary?.axes.map((a) => a.name) ?? [],
					agg: tn.agg,
				});
			}
		}
		// elkjs keeps edges on the root, but collect defensively from every
		// node (dedup by id) — coordinates are LCA-relative regardless of
		// where the edge object is stored.
		for (const e of elkNode.edges ?? []) if (e.id !== undefined) elkEdges.set(e.id, e);
		for (const c of elkNode.children ?? []) walk(c, x, y);
	};
	for (const e of laid.edges ?? []) if (e.id !== undefined) elkEdges.set(e.id, e);
	for (const c of laid.children ?? []) walk(c, 0, 0);

	// LCA of two forest nodes (deepest shared ancestor); its origin is the
	// offset for an edge between them.
	const ancestorsOf = (id: string): string[] => {
		const chain: string[] = [];
		let cur = id;
		while (true) {
			chain.push(cur);
			if (cur === "root") break;
			cur = parentOf.get(cur) ?? "root";
		}
		return chain;
	};
	const lcaOrigin = (a: string, b: string): Pt => {
		const anc = new Set(ancestorsOf(a));
		let hit = "root";
		for (const x of ancestorsOf(b)) {
			if (anc.has(x)) {
				hit = x;
				break;
			}
		}
		return absOrigin.get(hit) ?? { x: 0, y: 0 };
	};

	const outEdges: PositionedEdge[] = [];
	for (const [id, e] of elkEdges) {
		const meta = styleOf.get(id);
		const ends = edgeEnds.get(id);
		if (meta === undefined || ends === undefined || e === undefined) continue;
		const off = lcaOrigin(ends.src, ends.dst);
		let s = 0;
		for (const sec of e.sections ?? []) {
			const points: Pt[] = [
				{ x: off.x + sec.startPoint.x, y: off.y + sec.startPoint.y },
				...(sec.bendPoints ?? []).map((p) => ({ x: off.x + p.x, y: off.y + p.y })),
				{ x: off.x + sec.endPoint.x, y: off.y + sec.endPoint.y },
			];
			outEdges.push({
				id: `${id}:${s++}`,
				style: meta.style,
				label: meta.label,
				classKey: meta.classKey,
				points,
			});
		}
	}

	return {
		nodes,
		plates,
		edges: outEdges,
		width: laid.width ?? 0,
		height: laid.height ?? 0,
	};
}
