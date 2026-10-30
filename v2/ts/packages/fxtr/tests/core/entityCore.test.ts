import { createHash } from "node:crypto";
import { describe, expect, it } from "vitest";
import {
	DuplicateEntityMarkerError,
	type FxtrCbor,
	FxtrID,
	InvalidEntityError,
	RawFxtrEntityMarkerID,
	RawStrongFxtrID,
	RawWeakFxtrID,
	b32dec,
	b32enc,
	decodeVarint32,
	deepFreeze,
	encodeVarint32,
	fxtrCborDumps,
	fxtrCborDumpsWithMarkerCheck,
	fxtrCborLoads,
	uint8ArrayEquals,
} from "../../src/core/entityCore.js";

function sha256(data: string): Uint8Array {
	return new Uint8Array(createHash("sha256").update(data).digest());
}

// =============================================================================
// FxtrID Construction and Basic Operations
// =============================================================================

describe("FxtrID basics", () => {
	it("creates from tag and payload", () => {
		const fid = FxtrID.fromTagAndPayload("!", new TextEncoder().encode("test"));
		expect(fid.getTag()).toBe("!");
		const [tag, payload] = fid.splitTagAndPayload();
		expect(tag).toBe("!");
		expect(payload).toEqual(new TextEncoder().encode("test"));
	});

	it("roundtrips tag/payload extraction", () => {
		const original = FxtrID.builtin("example");
		const [tag, payload] = original.splitTagAndPayload();
		const reconstructed = FxtrID.fromTagAndPayload(tag, payload);
		expect(reconstructed.key()).toBe(original.key());
	});

	it("getTag returns correct single-character tag", () => {
		expect(FxtrID.fromTagAndPayload(".", new Uint8Array(0)).getTag()).toBe(".");
		expect(FxtrID.fromTagAndPayload("!", new Uint8Array([0x78])).getTag()).toBe("!");
		expect(FxtrID.fromTagAndPayload("d", new Uint8Array([0x78])).getTag()).toBe("d");
	});

	it("key() is consistent for same ID", () => {
		const fid1 = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		const fid2 = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		expect(fid1.key()).toBe(fid2.key());
	});

	it("key() differs for different IDs", () => {
		const fid1 = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		const fid2 = FxtrID.builtin("test");
		expect(fid1.key()).not.toBe(fid2.key());
	});

	it("works as Map key via key()", () => {
		const fid1 = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		const fid2 = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		const map = new Map<string, string>();
		map.set(fid1.key(), "value");
		expect(map.get(fid2.key())).toBe("value");
	});
});

// =============================================================================
// FxtrID String Serialization
// =============================================================================

describe("FxtrID string serialization", () => {
	it("'.' node roundtrips", () => {
		const fid = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		expect(fid.idString()).toBe(".");
		const parsed = FxtrID.fromIdString(".");
		expect(parsed.key()).toBe(fid.key());
	});

	it("'!' node roundtrips", () => {
		const fid = FxtrID.builtin("test_type");
		expect(fid.idString()).toBe("!test_type");
		const parsed = FxtrID.fromIdString(fid.idString());
		expect(parsed.key()).toBe(fid.key());
	});

	it("'d' node roundtrips", () => {
		const hashBytes = sha256("test data");
		const fid = FxtrID.fromTagAndPayload("d", hashBytes);
		const s = fid.idString();
		expect(s.startsWith("d")).toBe(true);
		const parsed = FxtrID.fromIdString(s);
		expect(parsed.key()).toBe(fid.key());
	});

	it("'n' node roundtrips", () => {
		const hashBytes = sha256("test data");
		const fid = FxtrID.fromTagAndPayload("n", hashBytes);
		const s = fid.idString();
		expect(s.startsWith("n")).toBe(true);
		const parsed = FxtrID.fromIdString(s);
		expect(parsed.key()).toBe(fid.key());
	});

	it("'c' node roundtrips", () => {
		const hashBytes = sha256("test data");
		const fid = FxtrID.fromTagAndPayload("c", hashBytes);
		const s = fid.idString();
		expect(s.startsWith("c")).toBe(true);
		const parsed = FxtrID.fromIdString(s);
		expect(parsed.key()).toBe(fid.key());
	});

	it("'u' node wrapping '.' roundtrips", () => {
		const inner = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		const fid = FxtrID.fromTagAndPayload("u", inner.rawDataId);
		expect(fid.idString()).toBe("u.");
		const parsed = FxtrID.fromIdString("u.");
		expect(parsed.key()).toBe(fid.key());
	});

	it("'u' node wrapping 'd' node roundtrips", () => {
		const hashBytes = sha256("test");
		const inner = FxtrID.fromTagAndPayload("d", hashBytes);
		const fid = FxtrID.fromTagAndPayload("u", inner.rawDataId);
		const s = fid.idString();
		expect(s.startsWith("ud")).toBe(true);
		const parsed = FxtrID.fromIdString(s);
		expect(parsed.key()).toBe(fid.key());
	});

	it("'p' node with empty path roundtrips", () => {
		const hashBytes = sha256("test");
		// p-node payload is canonical CBOR: [hash_bytes, ...path_indices]
		const payload = fxtrCborDumps([hashBytes]);
		const fid = FxtrID.fromTagAndPayload("p", payload);
		const s = fid.idString();
		expect(s).toContain("[");
		expect(s).toContain("]");
		const parsed = FxtrID.fromIdString(s);
		expect(parsed.key()).toBe(fid.key());
	});

	it("'p' node with path indices roundtrips", () => {
		const hashBytes = sha256("test");
		const payload = fxtrCborDumps([hashBytes, 0, 1, 2]);
		const fid = FxtrID.fromTagAndPayload("p", payload);
		const s = fid.idString();
		const parsed = FxtrID.fromIdString(s);
		expect(parsed.key()).toBe(fid.key());
	});

	it("empty string raises", () => {
		expect(() => FxtrID.fromIdString("")).toThrow("Empty");
	});

	it("unknown tag raises", () => {
		expect(() => FxtrID.fromIdString("Xinvalid")).toThrow("Unknown tag");
	});

	it("'.' with extra chars raises", () => {
		expect(() => FxtrID.fromIdString(".extra")).toThrow("Expected empty");
	});

	it("toString returns idString", () => {
		const hashBytes = sha256("test data");
		const fid = FxtrID.fromTagAndPayload("d", hashBytes);
		expect(fid.toString()).toBe(fid.idString());
	});
});

// =============================================================================
// FxtrID Summary
// =============================================================================

describe("FxtrID summary", () => {
	it("'.' summary is '.'", () => {
		const fid = FxtrID.fromTagAndPayload(".", new Uint8Array(0));
		expect(fid.summary()).toBe(".");
	});

	it("'d' summary is truncated", () => {
		const hashBytes = sha256("test data");
		const fid = FxtrID.fromTagAndPayload("d", hashBytes);
		const s = fid.summary();
		expect(s.startsWith("d")).toBe(true);
		expect(s.length).toBe(8); // "d" + 7 chars
	});

	it("'!' summary is full name", () => {
		const fid = FxtrID.builtin("ctx");
		expect(fid.summary()).toBe("!ctx");
	});
});

// =============================================================================
// Static constructors
// =============================================================================

describe("FxtrID static constructors", () => {
	it("builtin creates ! node", () => {
		const fid = FxtrID.builtin("my_type");
		expect(fid.getTag()).toBe("!");
		expect(fid.idString()).toBe("!my_type");
	});

	it("here creates . node", () => {
		const fid = FxtrID.here();
		expect(fid.getTag()).toBe(".");
		expect(fid.idString()).toBe(".");
	});

	it("various builtin names roundtrip", () => {
		for (const name of ["ctx", "pathroot", "entity", "my_custom_type"]) {
			const fid = FxtrID.builtin(name);
			const parsed = FxtrID.fromIdString(fid.idString());
			expect(parsed.key()).toBe(fid.key());
		}
	});
});

// =============================================================================
// Varint32
// =============================================================================

describe("varint32", () => {
	it("encodes 0 to '0'", () => {
		expect(encodeVarint32(0)).toBe("0");
	});

	it("values 0-15 encode to single character", () => {
		for (let i = 0; i < 16; i++) {
			expect(encodeVarint32(i).length).toBe(1);
		}
	});

	it("values >= 16 use continuation", () => {
		expect(encodeVarint32(16).length).toBe(2);
	});

	it("large values encode correctly", () => {
		expect(encodeVarint32(1000000).length).toBeGreaterThan(1);
	});

	it("encode -> decode roundtrips", () => {
		for (const value of [0, 1, 15, 16, 100, 1000, 65535, 1000000]) {
			const encoded = encodeVarint32(value);
			const [decoded, remaining] = decodeVarint32(encoded);
			expect(decoded).toBe(value);
			expect(remaining).toBe("");
		}
	});

	it("negative values raise", () => {
		expect(() => encodeVarint32(-1)).toThrow("non-negative");
	});

	it("decode with trailing data returns remainder", () => {
		const encoded = `${encodeVarint32(42)}xyz`;
		const [decoded, remaining] = decodeVarint32(encoded);
		expect(decoded).toBe(42);
		expect(remaining).toBe("xyz");
	});
});

// =============================================================================
// Base32
// =============================================================================

describe("base32", () => {
	it("roundtrips various data", () => {
		const testCases = [
			new Uint8Array(0),
			new Uint8Array([0x78]),
			new TextEncoder().encode("hello"),
			sha256("test"),
		];
		for (const data of testCases) {
			const encoded = b32enc(data);
			const decoded = b32dec(encoded);
			expect(decoded).toEqual(data);
		}
	});

	it("decoding is case-insensitive", () => {
		const data = new TextEncoder().encode("test");
		const encoded = b32enc(data);
		expect(b32dec(encoded.toUpperCase())).toEqual(data);
		expect(b32dec(encoded.toLowerCase())).toEqual(data);
	});

	it("encoded strings have no padding", () => {
		for (const s of ["x", "xx", "xxx", "xxxx"]) {
			const encoded = b32enc(new TextEncoder().encode(s));
			expect(encoded).not.toContain("=");
		}
	});
});

// =============================================================================
// Tagged ID wrapper classes
// =============================================================================

describe("tagged ID wrappers", () => {
	it("RawWeakFxtrID wraps correctly", () => {
		const fid = FxtrID.builtin("test");
		const raw = new RawWeakFxtrID(fid);
		expect(raw.innerId.key()).toBe(fid.key());
		expect(RawWeakFxtrID.CBOR_TAG).toBe(33001);
	});

	it("RawStrongFxtrID wraps correctly", () => {
		const fid = FxtrID.builtin("test");
		const raw = new RawStrongFxtrID(fid);
		expect(raw.innerId.key()).toBe(fid.key());
		expect(RawStrongFxtrID.CBOR_TAG).toBe(33002);
	});

	it("RawFxtrEntityMarkerID wraps correctly", () => {
		const fid = FxtrID.here();
		const raw = new RawFxtrEntityMarkerID(fid);
		expect(raw.innerId.key()).toBe(fid.key());
		expect(RawFxtrEntityMarkerID.CBOR_TAG).toBe(33000);
	});

	it("toCBOR returns [tag, rawDataId]", () => {
		const fid = FxtrID.builtin("test");
		expect(new RawWeakFxtrID(fid).toCBOR()).toEqual([33001, fid.rawDataId]);
		expect(new RawStrongFxtrID(fid).toCBOR()).toEqual([33002, fid.rawDataId]);
		expect(new RawFxtrEntityMarkerID(fid).toCBOR()).toEqual([33000, fid.rawDataId]);
	});
});

// =============================================================================
// CBOR Encoding/Decoding
// =============================================================================

describe("CBOR encoding/decoding", () => {
	it("simple entity roundtrips", () => {
		const ground = FxtrID.here();
		const typeId = FxtrID.builtin("test");
		const entity: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			"$fxtr.type": new RawWeakFxtrID(typeId),
			name: "test",
			value: 42,
		};
		const encoded = fxtrCborDumps(entity);
		const decoded = fxtrCborLoads(encoded) as Record<string, FxtrCbor>;
		expect(decoded.name).toBe("test");
		expect(decoded.value).toBe(42);
		expect(decoded["$fxtr.ctx"]).toBeInstanceOf(RawFxtrEntityMarkerID);
		expect(decoded["$fxtr.type"]).toBeInstanceOf(RawWeakFxtrID);
	});

	it("preserves all primitive types", () => {
		const ground = FxtrID.here();
		const entity: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			none_val: null,
			bool_true: true,
			bool_false: false,
			int_val: 42,
			float_val: 3.14,
			str_val: "hello",
			bytes_val: new Uint8Array([0x77, 0x6f, 0x72, 0x6c, 0x64]), // "world"
			list_val: [1, 2, 3],
			dict_val: { a: 1, b: 2 },
		};
		const encoded = fxtrCborDumps(entity);
		const decoded = fxtrCborLoads(encoded) as Record<string, FxtrCbor>;
		expect(decoded.none_val).toBe(null);
		expect(decoded.bool_true).toBe(true);
		expect(decoded.bool_false).toBe(false);
		expect(decoded.int_val).toBe(42);
		expect(decoded.float_val).toBeCloseTo(3.14);
		expect(decoded.str_val).toBe("hello");
		expect(decoded.bytes_val).toEqual(new Uint8Array([0x77, 0x6f, 0x72, 0x6c, 0x64]));
		expect(decoded.list_val).toEqual([1, 2, 3]);
		expect(decoded.dict_val).toEqual({ a: 1, b: 2 });
	});

	it("encoding is deterministic", () => {
		const ground = FxtrID.here();
		const entity: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			z_field: "last",
			a_field: "first",
		};
		const encoded1 = fxtrCborDumps(entity);
		const encoded2 = fxtrCborDumps(entity);
		expect(encoded1).toEqual(encoded2);
	});

	it("canonical encoding sorts keys by length then bytewise", () => {
		const ground = FxtrID.here();
		// Keys of different lengths should be sorted length-first
		const entity: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			zz: "two-char",
			a: "one-char",
		};
		const encoded = fxtrCborDumps(entity);
		const decoded = fxtrCborLoads(encoded) as Record<string, FxtrCbor>;
		const keys = Object.keys(decoded);
		// "a" (length 1) should come before "zz" (length 2) in canonical order
		expect(keys.indexOf("a")).toBeLessThan(keys.indexOf("zz"));
	});

	it("tagged IDs survive roundtrip with correct inner IDs", () => {
		const id1 = FxtrID.builtin("type1");
		const id2 = FxtrID.builtin("type2");
		const ground = FxtrID.here();
		const entity: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(ground),
			weak: new RawWeakFxtrID(id1),
			strong: new RawStrongFxtrID(id2),
		};
		const encoded = fxtrCborDumps(entity);
		const decoded = fxtrCborLoads(encoded) as Record<string, FxtrCbor>;
		const weak = decoded.weak as RawWeakFxtrID;
		const strong = decoded.strong as RawStrongFxtrID;
		expect(weak).toBeInstanceOf(RawWeakFxtrID);
		expect(strong).toBeInstanceOf(RawStrongFxtrID);
		expect(weak.innerId.key()).toBe(id1.key());
		expect(strong.innerId.key()).toBe(id2.key());
	});
});

// =============================================================================
// Marker Check
// =============================================================================

describe("marker check", () => {
	it("valid entity passes", () => {
		const entity: FxtrCbor = {
			"$fxtr.ctx": new RawFxtrEntityMarkerID(FxtrID.here()),
			data: "test",
		};
		const encoded = fxtrCborDumpsWithMarkerCheck(entity);
		expect(encoded).toBeInstanceOf(Uint8Array);
	});

	it("non-object raises InvalidEntityError", () => {
		expect(() => fxtrCborDumpsWithMarkerCheck([1, 2, 3] as unknown as FxtrCbor)).toThrow(
			InvalidEntityError,
		);
	});

	it("missing $fxtr.ctx raises InvalidEntityError", () => {
		expect(() => fxtrCborDumpsWithMarkerCheck({ data: "test" })).toThrow(InvalidEntityError);
	});

	it("wrong type for $fxtr.ctx raises InvalidEntityError", () => {
		expect(() =>
			fxtrCborDumpsWithMarkerCheck({
				"$fxtr.ctx": new RawWeakFxtrID(FxtrID.here()),
				data: "test",
			}),
		).toThrow(InvalidEntityError);
	});

	it("multiple markers raises DuplicateEntityMarkerError", () => {
		expect(() =>
			fxtrCborDumpsWithMarkerCheck({
				"$fxtr.ctx": new RawFxtrEntityMarkerID(FxtrID.here()),
				nested: {
					"$fxtr.ctx": new RawFxtrEntityMarkerID(FxtrID.here()),
				},
			}),
		).toThrow(DuplicateEntityMarkerError);
	});
});

// =============================================================================
// Deep Freeze
// =============================================================================

describe("deepFreeze", () => {
	it("freezes plain objects", () => {
		const obj = { a: 1, b: "two" };
		deepFreeze(obj);
		expect(Object.isFrozen(obj)).toBe(true);
	});

	it("freezes nested objects", () => {
		const obj = { a: { b: { c: 1 } } };
		deepFreeze(obj);
		expect(Object.isFrozen(obj)).toBe(true);
		expect(Object.isFrozen(obj.a)).toBe(true);
		expect(Object.isFrozen(obj.a.b)).toBe(true);
	});

	it("freezes arrays", () => {
		const arr = [1, [2, 3]];
		deepFreeze(arr);
		expect(Object.isFrozen(arr)).toBe(true);
		expect(Object.isFrozen(arr[1])).toBe(true);
	});

	it("handles primitives without error", () => {
		expect(deepFreeze(null)).toBe(null);
		expect(deepFreeze(42)).toBe(42);
		expect(deepFreeze("hello")).toBe("hello");
		expect(deepFreeze(true)).toBe(true);
	});

	it("freezes tagged IDs", () => {
		const fid = FxtrID.here();
		const weak = new RawWeakFxtrID(fid);
		deepFreeze(weak);
		expect(Object.isFrozen(weak)).toBe(true);
	});
});

// =============================================================================
// uint8ArrayEquals
// =============================================================================

describe("uint8ArrayEquals", () => {
	it("equal arrays return true", () => {
		expect(uint8ArrayEquals(new Uint8Array([1, 2, 3]), new Uint8Array([1, 2, 3]))).toBe(true);
	});

	it("empty arrays are equal", () => {
		expect(uint8ArrayEquals(new Uint8Array(0), new Uint8Array(0))).toBe(true);
	});

	it("different lengths return false", () => {
		expect(uint8ArrayEquals(new Uint8Array([1, 2]), new Uint8Array([1, 2, 3]))).toBe(false);
	});

	it("different contents return false", () => {
		expect(uint8ArrayEquals(new Uint8Array([1, 2, 3]), new Uint8Array([1, 2, 4]))).toBe(false);
	});
});

// =============================================================================
// FxtrID.equals
// =============================================================================

describe("FxtrID.equals", () => {
	it("same IDs are equal", () => {
		const a = FxtrID.here();
		const b = FxtrID.here();
		expect(a.equals(b)).toBe(true);
	});

	it("different IDs are not equal", () => {
		const a = FxtrID.here();
		const b = FxtrID.builtin("test");
		expect(a.equals(b)).toBe(false);
	});

	it("same builtin names are equal", () => {
		const a = FxtrID.builtin("ctx");
		const b = FxtrID.builtin("ctx");
		expect(a.equals(b)).toBe(true);
	});

	it("different builtin names are not equal", () => {
		const a = FxtrID.builtin("ctx");
		const b = FxtrID.builtin("pathroot");
		expect(a.equals(b)).toBe(false);
	});

	it("same d-node from same hash are equal", () => {
		const hash = sha256("test");
		const a = FxtrID.fromTagAndPayload("d", hash);
		const b = FxtrID.fromTagAndPayload("d", hash);
		expect(a.equals(b)).toBe(true);
	});

	it("consistent with key() comparison", () => {
		const hash = sha256("test");
		const a = FxtrID.fromTagAndPayload("d", hash);
		const b = FxtrID.fromTagAndPayload("d", hash);
		expect(a.equals(b)).toBe(a.key() === b.key());

		const c = FxtrID.fromTagAndPayload("n", hash);
		expect(a.equals(c)).toBe(a.key() === c.key());
	});
});
