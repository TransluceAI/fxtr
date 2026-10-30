import { describe, expect, it } from "vitest";

import { type ServiceDescriptor, canonicalizeDescriptor } from "../src/descriptor";
import { ShortIdTable } from "../src/shortid_table";

const BASE = "http://host:8000";

// Two long `d`-tag id_strings sharing a 7-char prefix, to exercise
// prefix allocation + collision extension (mirrors `short_ids.py`).
const D_ID_A = "d3f9a1c7k2m8q4r6";
const D_ID_B = "d3f9a1c9z5w1t0p2"; // shares "d3f9a1c" with D_ID_A

describe("ShortIdTable: entity allocation", () => {
	it("mints a shortId that is a prefix of the id_string and starts with the real tag", () => {
		const t = new ShortIdTable(BASE);
		const a = t.shortIdForEntity(D_ID_A);
		// Workstation parity: 7-char prefix of the id_string (tag + 6).
		expect(a).toBe("d3f9a1c");
		expect(D_ID_A.startsWith(a)).toBe(true);
		expect(t.shortIdForEntity(D_ID_A)).toBe(a); // idempotent
	});

	it("preserves a non-`d` tag (a `c`-node renders as `c…`, not `d…`)", () => {
		const t = new ShortIdTable(BASE);
		const c = t.shortIdForEntity("c1234567890abc");
		expect(c).toBe("c123456");
	});

	it("extends the prefix one char at a time on collision", () => {
		const t = new ShortIdTable(BASE);
		const a = t.shortIdForEntity(D_ID_A);
		const b = t.shortIdForEntity(D_ID_B);
		expect(a).toBe("d3f9a1c");
		// "d3f9a1c" is taken, so the second extends to the first
		// distinguishing char.
		expect(b).toBe("d3f9a1c9");
		expect(D_ID_B.startsWith(b)).toBe(true);
		expect(b).not.toBe(a);
	});

	it("uses the full id_string for non-prefixable tags", () => {
		const t = new ShortIdTable(BASE);
		expect(t.shortIdForEntity("!builtin-name")).toBe("!builtin-name");
		expect(t.shortIdForEntity(".")).toBe(".");
		expect(t.shortIdForEntity("p3f9[01]")).toBe("p3f9[01]");
	});

	it("round-trips via lookup + classifyShortId", () => {
		const t = new ShortIdTable(BASE);
		const sid = t.shortIdForEntity(D_ID_A);
		expect(t.lookup(sid)).toEqual({ kind: "entity", idString: D_ID_A });
		expect(t.classifyShortId(sid)).toBe("entity");
	});
});

describe("ShortIdTable: layered (provenanced) entity allocation", () => {
	const N_PROV_1 = "n3f9a1c7k2m8q4"; // a provenance-node (n) id_string
	const N_PROV_2 = "n9z5w1t0p2x3y4";
	const C_PROV = "c3f9a1c7k2m8q4"; // a c-node prov id_string (prefixable)

	it("first key in a bucket claims the bare prefix; new provs take ascending @N", () => {
		const t = new ShortIdTable(BASE);
		expect(t.shortIdForEntity(D_ID_A)).toBe("d3f9a1c"); // bare (@1 implicit)
		expect(t.shortIdForEntity(D_ID_A, N_PROV_1)).toBe("d3f9a1c@2");
		expect(t.shortIdForEntity(D_ID_A, N_PROV_2)).toBe("d3f9a1c@3");
		// Idempotent per (data, prov).
		expect(t.shortIdForEntity(D_ID_A, N_PROV_1)).toBe("d3f9a1c@2");
	});

	it("a layered key arriving first claims the bare prefix; a late (data, null) takes @0", () => {
		const t = new ShortIdTable(BASE);
		expect(t.shortIdForEntity(D_ID_A, N_PROV_1)).toBe("d3f9a1c"); // first → bare
		expect(t.shortIdForEntity(D_ID_A)).toBe("d3f9a1c@0"); // late bare → @0
		expect(t.shortIdForEntity(D_ID_A, N_PROV_2)).toBe("d3f9a1c@2");
	});

	it("the self-`u` form (X, u(X)) renders as @u", () => {
		const t = new ShortIdTable(BASE);
		expect(t.shortIdForEntity(D_ID_A)).toBe("d3f9a1c");
		expect(t.shortIdForEntity(D_ID_A, `u${D_ID_A}`)).toBe("d3f9a1c@u");
	});

	it("non-prefixable data composes the prov's (prov, null) shortId; self-u still @u", () => {
		const t = new ShortIdTable(BASE);
		expect(t.shortIdForEntity(".")).toBe(".");
		// Composite: the c-node prov's own (prov, null) shortId is "c3f9a1c".
		expect(t.shortIdForEntity(".", C_PROV)).toBe(".@c3f9a1c");
		expect(t.shortIdForEntity("c3f9a1c7k2m8q4")).toBe("c3f9a1c"); // same recursion result
		expect(t.shortIdForEntity(".", "u.")).toBe(".@u");
	});

	it("reverse-resolves an entity shortId to its (data, prov) key", () => {
		const t = new ShortIdTable(BASE);
		const bare = t.shortIdForEntity(D_ID_A);
		const layered = t.shortIdForEntity(D_ID_A, N_PROV_1);
		expect(t.entityKeyFor(bare)).toEqual({ dataIdString: D_ID_A, provIdString: null });
		expect(t.entityKeyFor(layered)).toEqual({ dataIdString: D_ID_A, provIdString: N_PROV_1 });
		expect(t.entityKeyFor("nope")).toBeUndefined();
		// A layered entry round-trips through lookup with the prov half.
		expect(t.lookup(layered)).toEqual({
			kind: "entity",
			idString: D_ID_A,
			provIdString: N_PROV_1,
		});
	});

	it("rehydrates layered buckets + continues the @N high-water past reload", () => {
		const t = new ShortIdTable(BASE);
		t.shortIdForEntity(D_ID_A); // d3f9a1c
		t.shortIdForEntity(D_ID_A, N_PROV_1); // @2
		t.shortIdForEntity(D_ID_A, N_PROV_2); // @3

		const t2 = new ShortIdTable(BASE, t.toJSON());
		// Existing (data, prov) keys resolve to the same shortIds.
		expect(t2.shortIdForEntity(D_ID_A)).toBe("d3f9a1c");
		expect(t2.shortIdForEntity(D_ID_A, N_PROV_1)).toBe("d3f9a1c@2");
		expect(t2.entityKeyFor("d3f9a1c@3")).toEqual({ dataIdString: D_ID_A, provIdString: N_PROV_2 });
		// A brand-new prov does not collide with @2/@3 — the counter continues.
		expect(t2.shortIdForEntity(D_ID_A, "nfreshfreshfresh")).toBe("d3f9a1c@4");
	});
});

describe("ShortIdTable: service allocation", () => {
	const http: ServiceDescriptor = { kind: "http", url: "/svc/echo", method: "GET" };

	it("mints an ascending `s{N}` shortId and is idempotent per descriptor", () => {
		const t = new ShortIdTable(BASE);
		const a = t.shortIdForService(http);
		expect(a).toBe("s0");
		expect(t.shortIdForService({ ...http })).toBe(a); // structural, not identity
		expect(t.classifyShortId(a)).toBe("service");
	});

	it("numbers distinct services ascending in allocation order", () => {
		const t = new ShortIdTable(BASE);
		const a = t.shortIdForService({ kind: "http", url: "/a", method: "GET" });
		const b = t.shortIdForService({ kind: "http", url: "/b", method: "GET" });
		const c = t.shortIdForService({ kind: "ws", url: "ws://host:8000/ws", name: "sched" });
		expect([a, b, c]).toEqual(["s0", "s1", "s2"]);
	});

	it("two descriptors differing only in service_type_id share one shortId", () => {
		const t = new ShortIdTable(BASE);
		const bare = t.shortIdForService(http);
		const annotated = t.shortIdForService({ ...http, service_type_id: "type-1" });
		expect(annotated).toBe(bare);
		// the richer descriptor is retained in the entry
		const entry = t.lookup(bare);
		expect(entry?.kind).toBe("service");
		if (entry?.kind === "service") expect(entry.descriptor.service_type_id).toBe("type-1");
	});

	it("a relative URL and its absolute form intern to the same shortId", () => {
		const t = new ShortIdTable(BASE);
		const rel = t.shortIdForService({ kind: "http", url: "/svc/echo", method: "GET" });
		const abs = t.shortIdForService({
			kind: "http",
			url: "http://host:8000/svc/echo",
			method: "GET",
		});
		expect(abs).toBe(rel);
	});

	it("distinct services get distinct shortIds", () => {
		const t = new ShortIdTable(BASE);
		const a = t.shortIdForService({ kind: "http", url: "/a", method: "GET" });
		const b = t.shortIdForService({ kind: "http", url: "/b", method: "GET" });
		expect(a).not.toBe(b);
	});

	it("interns a transient descriptor too", () => {
		const t = new ShortIdTable(BASE);
		const sid = t.shortIdForService({ kind: "transient", id: "transient-xyz" });
		expect(sid.startsWith("s")).toBe(true);
		expect(t.classifyShortId(sid)).toBe("service");
	});
});

describe("ShortIdTable: classifyShortId", () => {
	it("returns null for an unknown shortId", () => {
		const t = new ShortIdTable(BASE);
		expect(t.classifyShortId("dunknown")).toBeNull();
		expect(t.classifyShortId("whatever")).toBeNull();
	});
});

describe("ShortIdTable: persistence round-trip", () => {
	it("rehydrates entity + service mappings and keeps allocations stable", () => {
		const t = new ShortIdTable(BASE);
		const e = t.shortIdForEntity(D_ID_A);
		const s = t.shortIdForService({ kind: "ws", url: "ws://host:8000/ws", name: "sched" });

		const t2 = new ShortIdTable(BASE, t.toJSON());
		// Existing identities resolve to the same shortIds.
		expect(t2.shortIdForEntity(D_ID_A)).toBe(e);
		expect(t2.shortIdForService({ kind: "ws", url: "ws://host:8000/ws", name: "sched" })).toBe(s);
		expect(t2.lookup(e)).toEqual({ kind: "entity", idString: D_ID_A });
		expect(t2.classifyShortId(s)).toBe("service");
	});

	it("continues the ascending service counter past rehydrated shortIds", () => {
		const t = new ShortIdTable(BASE);
		t.shortIdForService({ kind: "http", url: "/a", method: "GET" }); // s0
		t.shortIdForService({ kind: "http", url: "/b", method: "GET" }); // s1

		const t2 = new ShortIdTable(BASE, t.toJSON());
		// A brand-new service does not collide with s0/s1.
		expect(t2.shortIdForService({ kind: "http", url: "/c", method: "GET" })).toBe("s2");
	});
});

describe("canonicalizeDescriptor", () => {
	it("drops service_type_id and sorts keys", () => {
		const a = canonicalizeDescriptor(
			{ kind: "http", url: "/x", method: "GET", service_type_id: "t" },
			BASE,
		);
		const b = canonicalizeDescriptor({ kind: "http", url: "/x", method: "GET" }, BASE);
		expect(a).toBe(b);
		// key order is stable regardless of input field order
		expect(a).toBe(canonicalizeDescriptor({ method: "GET", url: "/x", kind: "http" }, BASE));
	});
});
