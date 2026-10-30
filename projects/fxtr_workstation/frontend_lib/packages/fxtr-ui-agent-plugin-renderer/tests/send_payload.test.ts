/**
 * Send-box payload-shape tests.
 *
 * Bypasses the React + CodeMirror rendering layer and exercises the
 * same flow ``<SendBox>``'s ``onSend`` runs: build the payload via
 * ``buildSendPayload``, then call the mock session atom's setter
 * with a function that calls ``ctx.allocateInvoke(serviceStub,
 * "send", [payload])``. ``makeMockSessionAtom`` captures the invoke
 * in ``invokeCalls`` so we can assert the payload shape end to end.
 */

import type { EntityRefStub, ServiceRefStub } from "@fxtr-ui/core";
import type { RefStubResolver } from "@fxtr-ui/workstation-dispatch";
import { createStore } from "jotai";
import { describe, expect, it, vi } from "vitest";

import { buildSendPayload } from "../src/send_box/build_payload.js";
import {
	type MockInvokeCall,
	TestEntityRefStub,
	TestOpResultStub,
	TestServiceRefStub,
	makeMockSessionAtom,
} from "./_mock_session.js";

/**
 * Hand-built resolver mirroring ``ShellRefStubResolver``'s shape:
 *
 * - ``entities`` / ``services`` are sets of shortIds the resolver
 *   "knows" about.
 * - ``entityRef`` / ``serviceRef`` intern stubs the first time
 *   they're asked for and return the same instance on later calls,
 *   matching the connector's per-(kind, shortId) interning.
 */
function makeFakeResolver(opts: {
	readonly entities?: ReadonlyArray<string>;
	readonly services?: ReadonlyArray<string>;
}): RefStubResolver {
	const entitySet = new Set(opts.entities ?? []);
	const serviceSet = new Set(opts.services ?? []);
	const entityCache = new Map<string, EntityRefStub>();
	const serviceCache = new Map<string, ServiceRefStub>();
	return {
		entityRef(shortId) {
			let cached = entityCache.get(shortId);
			if (cached === undefined) {
				cached = new TestEntityRefStub(shortId);
				entityCache.set(shortId, cached);
			}
			return cached;
		},
		serviceRef(shortId) {
			let cached = serviceCache.get(shortId);
			if (cached === undefined) {
				cached = new TestServiceRefStub(shortId);
				serviceCache.set(shortId, cached);
			}
			return cached;
		},
		classifyShortId(shortId) {
			if (entitySet.has(shortId)) return "entity";
			if (serviceSet.has(shortId)) return "service";
			return null;
		},
	};
}

interface PanelStateForTest {
	readonly draftMessage: string;
}

/**
 * Simulate the SendBox's ``onSend`` callback against a mock session
 * atom + a controllable resolver. Returns the captured invoke call
 * (always exactly one for non-empty drafts).
 *
 * Mirrors ``<SendBox>``'s actual updater body verbatim — the only
 * difference is that the panel state shape is the test-local
 * minimum (just ``draftMessage``) rather than the full ``PanelState``.
 */
function simulateSend(
	draft: string,
	resolver: RefStubResolver | null,
): { call: MockInvokeCall; newState: PanelStateForTest } {
	const invokeCalls: MockInvokeCall[] = [];
	const serviceStub = new TestServiceRefStub("svc-agent");
	const { atom: stateAtom } = makeMockSessionAtom<PanelStateForTest>({
		initial: { draftMessage: draft },
		invokeQueue: [new TestOpResultStub<null>()],
		invokeCalls,
	});
	const store = createStore();
	store.set(stateAtom, (prev, ctx) => {
		const payload = buildSendPayload(prev.draftMessage, resolver);
		ctx.allocateInvoke(serviceStub, "send", [payload]);
		return { ...prev, draftMessage: "" };
	});
	expect(invokeCalls).toHaveLength(1);
	const call = invokeCalls[0];
	if (call === undefined) throw new Error("no invoke recorded");
	return { call, newState: store.get(stateAtom) };
}

describe("send payload shape", () => {
	it("no chips → bare string", () => {
		const r = makeFakeResolver({ entities: ["e-a"] });
		const { call, newState } = simulateSend("hello world", r);
		expect(call.method).toBe("send");
		expect(call.args).toEqual(["hello world"]);
		expect(newState.draftMessage).toBe("");
	});

	it("one entity chip → {message, attached: [entity]}", () => {
		const r = makeFakeResolver({ entities: ["e-abc"] });
		const { call } = simulateSend("look at @[e-abc]", r);
		const payload = call.args[0] as {
			message: string;
			attached: ReadonlyArray<EntityRefStub | ServiceRefStub>;
		};
		expect(payload.message).toBe("look at @[e-abc]");
		expect(payload.attached).toHaveLength(1);
		expect(payload.attached[0]?.shortId).toBe("e-abc");
		expect(payload.attached[0]).toBeInstanceOf(TestEntityRefStub);
	});

	it("mixed entity + service chips → ordered attached list", () => {
		const r = makeFakeResolver({ entities: ["e-abc"], services: ["s-13"] });
		const { call } = simulateSend("hi @[e-abc] and @[s-13]", r);
		const payload = call.args[0] as {
			message: string;
			attached: ReadonlyArray<EntityRefStub | ServiceRefStub>;
		};
		expect(payload.attached).toHaveLength(2);
		expect(payload.attached[0]?.shortId).toBe("e-abc");
		expect(payload.attached[0]).toBeInstanceOf(TestEntityRefStub);
		expect(payload.attached[1]?.shortId).toBe("s-13");
		expect(payload.attached[1]).toBeInstanceOf(TestServiceRefStub);
	});

	it("interned stubs: repeated shortIds yield same instance", () => {
		const r = makeFakeResolver({ entities: ["e-abc"] });
		const { call } = simulateSend("@[e-abc] @[e-abc]", r);
		const payload = call.args[0] as {
			message: string;
			attached: ReadonlyArray<EntityRefStub | ServiceRefStub>;
		};
		expect(payload.attached).toHaveLength(2);
		expect(payload.attached[0]).toBe(payload.attached[1]);
	});

	it("unresolved shortId is dropped from attached + warns", () => {
		const r = makeFakeResolver({ entities: ["e-known"] });
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		try {
			const { call } = simulateSend("hi @[e-known] @[unknown]", r);
			const payload = call.args[0] as {
				message: string;
				attached: ReadonlyArray<EntityRefStub | ServiceRefStub>;
			};
			expect(payload.attached).toHaveLength(1);
			expect(payload.attached[0]?.shortId).toBe("e-known");
			expect(warn).toHaveBeenCalledWith(expect.stringContaining("unresolved shortId"), "unknown");
		} finally {
			warn.mockRestore();
		}
	});

	it("all-unresolved chips fall back to bare string", () => {
		const r = makeFakeResolver({});
		const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
		try {
			const { call } = simulateSend("hi @[mystery]", r);
			// All chips unresolved → attached.length === 0 → bare-string fallback.
			expect(call.args).toEqual(["hi @[mystery]"]);
		} finally {
			warn.mockRestore();
		}
	});

	it("null resolver (outside a shell) always returns bare string", () => {
		const { call } = simulateSend("hi @[e-abc]", null);
		expect(call.args).toEqual(["hi @[e-abc]"]);
	});
});
