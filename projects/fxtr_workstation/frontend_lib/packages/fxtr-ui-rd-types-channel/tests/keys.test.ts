/**
 * Wire-stable UUID snapshots for the two channel dispatch keys and
 * shape checks on their fallback bundles.
 *
 * Persisted dispatcher state keys `entries[uuid][shortId]` on the
 * key's UUID, so any unintended change is a breaking change for
 * sessions carrying channel-keyed entries. This test catches
 * accidental edits. (The `rd-types-stream` pattern.)
 */

import { describe, expect, it } from "vitest";
import { ChannelContentCard, ChannelStatusPreview, GenericChannelCard } from "../src/index.js";

describe("ChannelContentCard dispatch key", () => {
	it("uuid is wire-stable", () => {
		expect(ChannelContentCard.uuid).toBe("830e2987-08a7-410f-8c23-e01f05878c50");
	});

	it("has a human-readable description", () => {
		expect(ChannelContentCard.description).toBe("ChannelContentCard");
	});

	it("NoMatch is the generic stream card, not an error state", () => {
		expect(ChannelContentCard.fallbacks.NoMatch).toBe(GenericChannelCard);
	});

	it("carries a complete fallback bundle (Loading + NoMatch + Error)", () => {
		expect(typeof ChannelContentCard.fallbacks.Loading).toBe("function");
		expect(typeof ChannelContentCard.fallbacks.NoMatch).toBe("function");
		expect(typeof ChannelContentCard.fallbacks.Error).toBe("function");
	});
});

describe("ChannelStatusPreview dispatch key", () => {
	it("uuid is wire-stable", () => {
		expect(ChannelStatusPreview.uuid).toBe("beb2e1df-699a-4a90-b5fb-490382ba2628");
	});

	it("has a human-readable description", () => {
		expect(ChannelStatusPreview.description).toBe("ChannelStatusPreview");
	});

	it("carries a complete fallback bundle (Loading + NoMatch + Error)", () => {
		expect(typeof ChannelStatusPreview.fallbacks.Loading).toBe("function");
		expect(typeof ChannelStatusPreview.fallbacks.NoMatch).toBe("function");
		expect(typeof ChannelStatusPreview.fallbacks.Error).toBe("function");
	});
});
