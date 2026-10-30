/**
 * Cross-language consistency tests for bicodec entity encoding.
 *
 * These tests verify that TypeScript produces byte-identical content-addressed
 * entity IDs to the Python implementation for equivalent entity definitions.
 *
 * The Python counterpart is at:
 *   v2/tests/test_cross_language/test_bicodec_consistency.py
 *
 * If any of these tests fail, it means the two languages have diverged in their
 * canonical CBOR encoding, which would break cross-language interop.
 */

import { describe, expect, it } from "vitest";
import * as bicodec from "../../src/common/bicodec.js";
import { pyCompatSet } from "../../src/common/pyCompatSet.js";
import { FxtrBasicType, UUID, defineType } from "../../src/common/systemEntities.js";
import { type FxtrScope, GLOBAL_SCOPE, withFxtrScope } from "../../src/scope/scope.js";

const NS = UUID("550e8400-e29b-41d4-a716-446655440000");

// Hardcoded expected IDs — must match Python test expectations
const EXPECTED_DEFINE_TYPE_CROSS_LANG = "dheggkym46dt6g7rxg6ftcpldoly6r4fjcwihuqgaisvei636ja4a";
const EXPECTED_DEFINE_TYPE_ANOTHER = "dhwtdxe3t4jp5djsqgo6upturz74d34eauwcjz3txlasdj6gw43ba";
const EXPECTED_SIMPLE_ENTITY = "df2dsm46vlwfcf7dyqqvid7eud3dazzj6z3h6lkzixahpimntvb7a";
const EXPECTED_SET_ENTITY = "d2igkdhjh4tx2kbn3guz4prs2lzuje7wz6lrw7ypizce2x2szqc4q";
const EXPECTED_NULLABLE_NULL = "dfjtvfjbdlkowhknevyk6umyxjxbgwd3v3a4dvze3plugqab5oqfq";
const EXPECTED_NULLABLE_PRESENT = "dshw3655bvyeegydyiiqxujvsgfv6axca7x5b66ekph5a4hxufglq";

async function withTestScope(fn: (scope: FxtrScope) => Promise<void>): Promise<void> {
	await withFxtrScope({ include: [GLOBAL_SCOPE] }, fn);
}

// Entity definitions matching the Python side
const SimpleEntity = bicodec.entity(defineType(NS, "SimpleEntity"), {
	name: bicodec.string(),
	value: bicodec.number(),
});

const tagsCodec = pyCompatSet(bicodec.string());
const SetEntity = bicodec.entity(defineType(NS, "SetEntity"), {
	tags: tagsCodec,
});

const NullableEntity = bicodec.entity(defineType(NS, "NullableEntity"), {
	label: bicodec.nullable(bicodec.string()),
});

describe("cross-language consistency", () => {
	it("defineType produces same ID for CrossLangTest", async () => {
		const typeId = await defineType(NS, "CrossLangTest");
		expect(typeId.idString()).toBe(EXPECTED_DEFINE_TYPE_CROSS_LANG);
	});

	it("defineType produces same ID for AnotherType", async () => {
		const typeId = await defineType(NS, "AnotherType");
		expect(typeId.idString()).toBe(EXPECTED_DEFINE_TYPE_ANOTHER);
	});

	it("FxtrBasicType round-trip matches Python", async () => {
		await withTestScope(async (scope) => {
			const bt = FxtrBasicType.create({
				namespace_uuid: NS,
				type_name: "CrossLangTest",
			});
			const ref = await bt.store(scope);
			expect(ref.entityId.idString()).toBe(EXPECTED_DEFINE_TYPE_CROSS_LANG);
		});
	});

	it("SimpleEntity matches Python", async () => {
		await withTestScope(async (scope) => {
			const simple = SimpleEntity.create({ name: "hello", value: 42 });
			const ref = await simple.store(scope);
			expect(ref.entityId.idString()).toBe(EXPECTED_SIMPLE_ENTITY);
		});
	});

	it("SetEntity with canonical sort matches Python", async () => {
		await withTestScope(async (scope) => {
			const se = SetEntity.create({
				tags: tagsCodec.createSet(["beta", "alpha", "gamma"]),
			});
			const ref = await se.store(scope);
			expect(ref.entityId.idString()).toBe(EXPECTED_SET_ENTITY);
		});
	});

	it("NullableEntity with null matches Python", async () => {
		await withTestScope(async (scope) => {
			const ne = NullableEntity.create({ label: null });
			const ref = await ne.store(scope);
			expect(ref.entityId.idString()).toBe(EXPECTED_NULLABLE_NULL);
		});
	});

	it("NullableEntity with value matches Python", async () => {
		await withTestScope(async (scope) => {
			const ne = NullableEntity.create({ label: "present" });
			const ref = await ne.store(scope);
			expect(ref.entityId.idString()).toBe(EXPECTED_NULLABLE_PRESENT);
		});
	});
});
