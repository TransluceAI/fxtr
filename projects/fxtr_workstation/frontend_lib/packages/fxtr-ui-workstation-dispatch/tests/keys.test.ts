/**
 * Wire-stable UUID snapshot for the workstation's four dispatch
 * keys. Any unintended change to a `DispatchKey.uuid` is a
 * breaking change for persisted dispatch state (sessions key
 * `entries[uuid][shortId]` on it) and for runtime-loaded renderer
 * modules (whose compiled export shape is keyed by these UUIDs).
 * This test catches accidental edits.
 */
import { describe, expect, it } from "vitest";
import { BlockLink, FullPanel, HoverPreview, InlineLink } from "../src/index.js";

describe("workstation dispatch key UUIDs", () => {
	it("FullPanel.uuid is wire-stable", () => {
		expect(FullPanel.uuid).toBe("21b2f3a8-7d54-4a1e-9c63-6a4b8e0f1234");
	});

	it("BlockLink.uuid is wire-stable", () => {
		expect(BlockLink.uuid).toBe("b3c4d5e6-7f89-4a01-9b23-4c5d6e7f8901");
	});

	it("InlineLink.uuid is wire-stable", () => {
		expect(InlineLink.uuid).toBe("c4d5e6f7-8901-4a12-9b34-5d6e7f890123");
	});

	it("HoverPreview.uuid is wire-stable", () => {
		expect(HoverPreview.uuid).toBe("d5e6f7a8-9012-4a23-9b45-6e7f89012345");
	});

	it("all four keys have distinct UUIDs", () => {
		const uuids = new Set([FullPanel.uuid, BlockLink.uuid, InlineLink.uuid, HoverPreview.uuid]);
		expect(uuids.size).toBe(4);
	});
});
