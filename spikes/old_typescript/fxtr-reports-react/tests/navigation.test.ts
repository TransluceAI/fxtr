/**
 * Tests for navigation state reducer: push, replace, back.
 */

import { FxtrID } from "@fxtr/core";
import { describe, expect, it } from "vitest";
import { type NavigationState, navigationReducer } from "../src/navigation.js";

// Helper: create a FxtrID from a string (using data node type)
function makeId(s: string): FxtrID {
	const bytes = new TextEncoder().encode(s);
	// Use 'd' (data) node tag byte = 0x64
	const idBytes = new Uint8Array(bytes.length + 1);
	idBytes[0] = 0x64;
	idBytes.set(bytes, 1);
	return new FxtrID(idBytes);
}

const empty: NavigationState = { breadcrumbs: [] };
const idA = makeId("entity-a");
const idB = makeId("entity-b");
const idC = makeId("entity-c");

describe("navigationReducer", () => {
	it("navigate pushes new entity to breadcrumbs", () => {
		const s1 = navigationReducer(empty, {
			type: "navigate",
			entityId: idA,
		});
		expect(s1.breadcrumbs).toHaveLength(1);
		expect(s1.breadcrumbs[0]?.entityId.key()).toBe(idA.key());

		const s2 = navigationReducer(s1, {
			type: "navigate",
			entityId: idB,
		});
		expect(s2.breadcrumbs).toHaveLength(2);
		expect(s2.breadcrumbs[1]?.entityId.key()).toBe(idB.key());
	});

	it("navigate with same entity replaces top entry navState", () => {
		const s1 = navigationReducer(empty, {
			type: "navigate",
			entityId: idA,
			navState: "initial",
		});
		expect(s1.breadcrumbs).toHaveLength(1);
		expect(s1.breadcrumbs[0]?.navState).toBe("initial");

		const s2 = navigationReducer(s1, {
			type: "navigate",
			entityId: idA,
			navState: "updated",
		});
		expect(s2.breadcrumbs).toHaveLength(1);
		expect(s2.breadcrumbs[0]?.navState).toBe("updated");
	});

	it("navigateBack pops to specified index", () => {
		let state = empty;
		state = navigationReducer(state, {
			type: "navigate",
			entityId: idA,
		});
		state = navigationReducer(state, {
			type: "navigate",
			entityId: idB,
		});
		state = navigationReducer(state, {
			type: "navigate",
			entityId: idC,
		});
		expect(state.breadcrumbs).toHaveLength(3);

		const popped = navigationReducer(state, { type: "back", index: 0 });
		expect(popped.breadcrumbs).toHaveLength(1);
		expect(popped.breadcrumbs[0]?.entityId.key()).toBe(idA.key());
	});

	it("navigateBack with index -1 clears all", () => {
		let state = empty;
		state = navigationReducer(state, {
			type: "navigate",
			entityId: idA,
		});
		state = navigationReducer(state, {
			type: "navigate",
			entityId: idB,
		});

		const cleared = navigationReducer(state, { type: "back", index: -1 });
		expect(cleared.breadcrumbs).toHaveLength(0);
	});

	it("navigateBack with out-of-range index is no-op", () => {
		let state = empty;
		state = navigationReducer(state, {
			type: "navigate",
			entityId: idA,
		});

		const same = navigationReducer(state, { type: "back", index: 5 });
		expect(same).toBe(state);
	});
});
