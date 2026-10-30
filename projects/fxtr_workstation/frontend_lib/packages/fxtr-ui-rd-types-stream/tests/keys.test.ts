/**
 * Wire-stable UUID snapshot for ``StreamEntry`` and a shape check
 * on its fallbacks bundle.
 *
 * Persisted dispatcher state keys ``entries[uuid][shortId]`` on the
 * key's UUID, so any unintended change is a breaking change for
 * sessions carrying StreamEntry-keyed entries. This test catches
 * accidental edits.
 */

import { describe, expect, it } from "vitest";
import { StreamEntry } from "../src/index.js";

describe("StreamEntry dispatch key", () => {
	it("uuid is wire-stable", () => {
		expect(StreamEntry.uuid).toBe("4b2e1a3c-7d68-4e91-a25f-9c3d8b0e1f7a");
	});

	it("has a human-readable description", () => {
		expect(StreamEntry.description).toBe("StreamEntry");
	});

	it("carries a complete fallback bundle (Loading + NoMatch + Error)", () => {
		expect(typeof StreamEntry.fallbacks.Loading).toBe("function");
		expect(typeof StreamEntry.fallbacks.NoMatch).toBe("function");
		expect(typeof StreamEntry.fallbacks.Error).toBe("function");
	});

	it("does not carry a Builtin fallback (key admits services; no entity-only shortcut)", () => {
		expect(StreamEntry.fallbacks.Builtin).toBeUndefined();
	});
});
