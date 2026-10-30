/** Unit tests for the plate-view derivations (pure — no backend, no
 * layout): grant-class transitive closure + the edge-dedup grammar. */

import { describe, expect, it } from "vitest";

import {
	type GrantClass,
	type PlateForestNode,
	buildPlateForest,
	classifyRelations,
	grantClasses,
} from "../src/derive/plates.js";
import { buildGridTree, encodeKeyPath } from "../src/derive/tree.js";
import type {
	AxisCorrespondence,
	GraphEdge,
	GraphEdgeKind,
	GridGraph,
	GridKind,
	GridSummary,
	InputGrantEdge,
	SliceMode,
} from "../src/wire/types.js";

function corr(gridA: string, axisA: string, gridB: string, axisB: string): AxisCorrespondence {
	return { grid_a: gridA, axis_a: axisA, grid_b: gridB, axis_b: axisB };
}

function edge(observer: string, subject: string, kind: GraphEdgeKind): GraphEdge {
	return { observer_path: observer, subject_path: subject, kind, count: 1 };
}

function grant(
	task: string,
	target: string,
	argName: string,
	opts: { isTaskFn?: boolean; mode?: SliceMode; kept?: boolean } = {},
): InputGrantEdge {
	return {
		task_path: task,
		target_path: target,
		arg_name: argName,
		is_task_fn: opts.isTaskFn ?? false,
		mode: opts.mode ?? "read",
		kept: opts.kept ?? false,
	};
}

function graph(parts: Partial<GridGraph>): GridGraph {
	return {
		edges: parts.edges ?? [],
		input_grants: parts.input_grants ?? [],
		correspondences: parts.correspondences ?? [],
	};
}

describe("grantClasses", () => {
	it("takes the transitive closure across correspondences", () => {
		// A.x ~ B.x ~ C.y all unify into one class (name-heterogeneous),
		// D.z ~ E.z is a separate class.
		const classes = grantClasses([
			corr("A", "x", "B", "x"),
			corr("B", "x", "C", "y"),
			corr("D", "z", "E", "z"),
		]);
		expect(classes.length).toBe(2);
		const big = classes.find((c) => c.grids.includes("A"));
		expect(big).toBeDefined();
		expect(big?.grids).toEqual(["A", "B", "C"]);
		expect(big?.axisNames).toEqual(["x", "y"]); // heterogeneous names, both shown
		const other = classes.find((c) => c.grids.includes("D"));
		expect(other?.grids).toEqual(["D", "E"]);
		expect(other?.axisNames).toEqual(["z"]);
	});

	it("is empty when there are no correspondences", () => {
		expect(grantClasses([])).toEqual([]);
	});

	it("merges two inputs joined through a common task axis", () => {
		// D1.m and D2.m both consumed into T.m ⇒ one class over all three.
		const classes = grantClasses([corr("T", "m", "D1", "m"), corr("T", "m", "D2", "m")]);
		expect(classes.length).toBe(1);
		expect(classes[0]?.grids).toEqual(["D1", "D2", "T"]);
	});
});

describe("classifyRelations", () => {
	it("marks a grant used when a matching read exists, held otherwise", () => {
		const g = graph({
			input_grants: [grant("T", "I", "inp"), grant("T", "H", "", { isTaskFn: true })],
			edges: [edge("T", "I", "read")], // T read I ⇒ the I grant is used
		});
		const { grants } = classifyRelations(g);
		const byTarget = new Map(grants.map((x) => [x.target_path, x.port]));
		expect(byTarget.get("I")).toBe("used");
		expect(byTarget.get("H")).toBe("held"); // task_fn grant, never read here
	});

	it("emits an unmatched read as a bare dependency (no grant)", () => {
		// A subtree granted, but a child grid inside it is what is read.
		const g = graph({
			input_grants: [grant("T", "sub", "s")],
			edges: [edge("T", "sub/child", "read")],
		});
		const { grants, reads } = classifyRelations(g);
		expect(grants[0]?.port).toBe("held"); // the subtree grant itself is unread
		expect(reads.map((e) => e.subject_path)).toEqual(["sub/child"]);
	});

	it("splits writes and creations out (they never dedup against grants)", () => {
		const g = graph({
			edges: [
				edge("T", "out", "wrote_cell"),
				edge("T", "made", "grid_assertion"),
				edge("T", "in", "read"),
			],
		});
		const { writes, creates, reads } = classifyRelations(g);
		expect(writes.map((e) => e.subject_path)).toEqual(["out"]);
		expect(creates.map((e) => e.subject_path)).toEqual(["made"]);
		expect(reads.map((e) => e.subject_path)).toEqual(["in"]);
	});
});

// -- buildPlateForest (laminar promotion) --

const P = (...keys: string[]): string => encodeKeyPath(keys);

function summary(path: string, kind: GridKind): GridSummary {
	return {
		path,
		kind,
		axes: [],
		// biome-ignore lint/suspicious/noExplicitAny: tests never touch the stub
		config_id: null as any,
		grounding: "grounded",
		grounding_reason: null,
		sourcing: null,
		populated: 0,
		n_pending: 0,
		n_running: 0,
		n_complete: 0,
		n_errored: 0,
		n_interrupted: 0,
		n_stale: 0,
		n_unsettled: 0,
		targets_attached: 0,
		targets_dangling: 0,
	};
}

function summaryAx(path: string, kind: GridKind, ...axisNames: string[]): GridSummary {
	return { ...summary(path, kind), axes: axisNames.map((name) => ({ name, kind: "str" })) };
}

function klass(key: string, grids: string[], axisNames: string[]): GrantClass {
	return { key, grids, axisNames, members: [] };
}

/** All plate nodes with the given classKey, anywhere in the forest. */
function platesOf(nodes: readonly PlateForestNode[], classKey: string): PlateForestNode[] {
	const out: PlateForestNode[] = [];
	const walk = (n: PlateForestNode): void => {
		if (n.kind === "plate" && n.classKey === classKey) out.push(n);
		for (const c of n.children) walk(c);
	};
	for (const n of nodes) walk(n);
	return out;
}

function gridChildren(n: PlateForestNode): string[] {
	return n.children.filter((c) => c.kind === "grid").map((c) => (c as { path: string }).path);
}

describe("buildPlateForest", () => {
	it("boxes a class's members and leaves non-members outside", () => {
		const tree = buildGridTree(
			[P("inputs"), P("shout"), P("combined"), P("fns")].map((p) => summary(p, "data")),
		);
		const forest = buildPlateForest(tree, [
			klass("x", [P("inputs"), P("shout"), P("combined")], ["x"]),
		]);
		const plates = platesOf(forest.roots, "x");
		expect(plates).toHaveLength(1); // one laminar box
		expect(gridChildren(plates[0] as PlateForestNode).sort()).toEqual(
			[P("combined"), P("inputs"), P("shout")].sort(),
		);
		// fns is a top-level grid, NOT inside the x plate.
		expect(forest.roots.some((n) => n.kind === "grid" && n.path === P("fns"))).toBe(true);
		expect(forest.railsByClass.get("x")).toHaveLength(1); // single box ⇒ no rail
	});

	it("splits a class across subtrees into two boxes tied by a rail", () => {
		// two subtrees, each with a member of class `model`.
		const tree = buildGridTree([
			summary(P("A"), "subtree"),
			summary(P("A", "d"), "data"),
			summary(P("B"), "subtree"),
			summary(P("B", "d"), "data"),
		]);
		const forest = buildPlateForest(tree, [klass("model", [P("A", "d"), P("B", "d")], ["model"])]);
		const plates = platesOf(forest.roots, "model");
		expect(plates).toHaveLength(2); // one per subtree
		expect(forest.railsByClass.get("model")).toHaveLength(2); // ⇒ a rail ties them
	});

	it("nests a smaller overlapping class inside the larger one", () => {
		const tree = buildGridTree([P("A"), P("B"), P("C")].map((p) => summary(p, "data")));
		// model ⊃ {A,B,C}; user ⊂ {B,C}. Larger (model) is the outer box;
		// user's members are now under the model plate, so user nests inside.
		const forest = buildPlateForest(tree, [
			klass("model", [P("A"), P("B"), P("C")], ["model"]),
			klass("user", [P("B"), P("C")], ["user"]),
		]);
		const modelPlates = platesOf(forest.roots, "model");
		expect(modelPlates).toHaveLength(1);
		const userPlates = platesOf(forest.roots, "user");
		expect(userPlates).toHaveLength(1);
		// the user plate is a descendant of the model plate.
		const model = modelPlates[0] as PlateForestNode;
		expect(platesOf(model.children, "user")).toHaveLength(1);
		expect(gridChildren(userPlates[0] as PlateForestNode).sort()).toEqual([P("B"), P("C")].sort());
	});

	it("gives an unshared (solo) own-axis its own singleton plate", () => {
		const tree = buildGridTree([summaryAx(P("d"), "data", "x")]);
		const forest = buildPlateForest(tree, []); // no grant-classes at all
		expect(forest.roots).toHaveLength(1);
		const plate = forest.roots[0] as PlateForestNode;
		expect(plate.kind).toBe("plate");
		if (plate.kind !== "plate") throw new Error("expected a plate root");
		expect(plate.label).toBe("x");
		expect(gridChildren(plate)).toEqual([P("d")]);
	});

	it("nests a grid's own-axis plates in frame order (outer axis outermost)", () => {
		// own axes (a, b): a is frame-position 0 (outer), b is 1 (inner).
		const tree = buildGridTree([summaryAx(P("g"), "data", "a", "b")]);
		const forest = buildPlateForest(tree, []);
		const outer = forest.roots[0] as PlateForestNode;
		if (outer.kind !== "plate") throw new Error("expected outer plate");
		expect(outer.label).toBe("a");
		const inner = outer.children[0] as PlateForestNode;
		expect(inner.kind).toBe("plate");
		if (inner.kind !== "plate") throw new Error("expected inner plate");
		expect(inner.label).toBe("b");
		expect(gridChildren(inner)).toEqual([P("g")]);
	});
});
