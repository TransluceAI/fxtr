import { type EntityRefStub, memoryStorage } from "@fxtr-ui/core";
import { type FxtrCbor, FxtrID, RawWeakFxtrID, fxtrCborDumps } from "@fxtr/core";
import { beforeEach, describe, expect, it } from "vitest";

import { HttpConnector } from "../src/connector";
import type { FetchFn } from "../src/entity_data";

const BASE = "http://host:8000";

function cborResponse(envelope: FxtrCbor): Response {
	const bytes = fxtrCborDumps(envelope);
	return {
		status: 200,
		ok: true,
		arrayBuffer: async () => bytes.slice().buffer,
	} as unknown as Response;
}
function jsonResponse(body: unknown): Response {
	return { status: 200, ok: true, json: async () => body } as unknown as Response;
}

// A layered parent (data, prov) whose prov form references the child's
// prov node; the child is the self-`u` form (childData, u(childData)).
const parentData = FxtrID.fromTagAndPayload("d", new Uint8Array(32).fill(1));
const parentProv = FxtrID.fromTagAndPayload("n", new Uint8Array(32).fill(3));
const childData = FxtrID.fromTagAndPayload("d", new Uint8Array(32).fill(2));
const childProv = FxtrID.fromTagAndPayload("u", childData.rawDataId); // u(childData)
// The child's own data content references a (bare) grandchild.
const grandchildData = FxtrID.fromTagAndPayload("d", new Uint8Array(32).fill(4));

// A standalone prov node (a u-node) for the unwraps_to metadata path.
const baseData = FxtrID.fromTagAndPayload("d", new Uint8Array(32).fill(7));
const uNode = FxtrID.fromTagAndPayload("u", baseData.rawDataId); // u(baseData)

const DISCOVERY = {
	// Layered entity value: [data, prov].
	entities: { "demo/parent": [parentData.idString(), parentProv.idString()] },
	services: {},
};

function makeFetch(): FetchFn {
	return async (url) => {
		if (url === `${BASE}/discovery`) return jsonResponse(DISCOVERY);
		// The parent's PROV form: full content with a provenance-level child
		// ref, plus the child prov node's unwraps_to in the precomp closure.
		if (url === `${BASE}/data/${parentProv.idString()}`) {
			return cborResponse({
				bytes: fxtrCborDumps({ child: new RawWeakFxtrID(childProv) }),
				precomp: { [childProv.idString()]: { unwraps_to: childData.idString() } },
			});
		}
		// The child's DATA node (a bare grandchild ref). Loading the child's
		// (childData, u(childData)) ref must come here — never to /data/u(child).
		if (url === `${BASE}/data/${childData.idString()}`) {
			return cborResponse({
				bytes: fxtrCborDumps({ grandchild: new RawWeakFxtrID(grandchildData) }),
				precomp: {},
			});
		}
		// The standalone u-node, for the unwraps_to metadata path.
		if (url === `${BASE}/data/${uNode.idString()}`) {
			return cborResponse({
				bytes: fxtrCborDumps({}),
				precomp: { [uNode.idString()]: { unwraps_to: baseData.idString() } },
			});
		}
		throw new Error(`unexpected fetch ${url}`);
	};
}

let storage: ReturnType<typeof memoryStorage>;
beforeEach(() => {
	storage = memoryStorage();
});

describe("HttpConnector: provenance", () => {
	it("decodes a layered discovery entity (data half shown; ref carries provenance)", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const rec = c.store.get(c.listEntities().records)[0];
		expect(rec?.idString).toBe(parentData.idString()); // record shows the data half
		const ref = rec?.ref as EntityRefStub;
		expect(c.entityKeyFor(ref.shortId)).toEqual({
			dataIdString: parentData.idString(),
			provIdString: parentProv.idString(),
		});
	});

	it("loads a layered entity by its prov form and its children come back LAYERED", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const parentRef = c.store.get(c.listEntities().records)[0]?.ref as EntityRefStub;

		const decoded = (await c.loadEntityData(parentRef)) as { child: EntityRefStub };
		// The child ref decoded from the prov form is layered: (childData, childProv).
		expect(c.entityKeyFor(decoded.child.shortId)).toEqual({
			dataIdString: childData.idString(),
			provIdString: childProv.idString(),
		});
	});

	it("loads a (data, u-node) ref via the data node — never fetches the synthetic u-node", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const parentRef = c.store.get(c.listEntities().records)[0]?.ref as EntityRefStub;
		const decoded = (await c.loadEntityData(parentRef)) as { child: EntityRefStub };

		// decoded.child is (childData, u(childData)). Loading it must hit
		// /data/{childData} (served above), NOT /data/{u(childData)} (absent →
		// makeFetch throws "unexpected fetch" if the synthetic node is fetched).
		const childLoaded = (await c.loadEntityData(decoded.child)) as { grandchild: EntityRefStub };

		// The grandchild is re-wrapped with its own trivial u-node provenance.
		const grandchildProv = FxtrID.fromTagAndPayload("u", grandchildData.rawDataId);
		expect(c.entityKeyFor(childLoaded.grandchild.shortId)).toEqual({
			dataIdString: grandchildData.idString(),
			provIdString: grandchildProv.idString(),
		});
	});

	it("surfaces provenance_parent in getEntityMetadata for a layered ref", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const parentRef = c.store.get(c.listEntities().records)[0]?.ref as EntityRefStub;

		const meta = await c.getEntityMetadata(parentRef);
		expect(meta.id_string).toBe(parentData.idString());
		expect(meta.unprovenanced).toBeDefined();
		expect(meta.provenance_parent).toBeDefined();
		expect(meta.provenance_parent_id_string).toBe(parentProv.idString());
		// unprovenanced is the bare data half; provenance_parent the bare prov half.
		expect(c.entityKeyFor((meta.unprovenanced as EntityRefStub).shortId)).toEqual({
			dataIdString: parentData.idString(),
			provIdString: null,
		});
		expect(c.entityKeyFor((meta.provenance_parent as EntityRefStub).shortId)).toEqual({
			dataIdString: parentProv.idString(),
			provIdString: null,
		});
	});

	it("a bare ref has no provenance_parent (unprovenanced is itself)", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const bareRef = c.internEntity(parentData.idString()); // bare (data, null)
		const meta = await c.getEntityMetadata(bareRef);
		expect(meta.provenance_parent).toBeUndefined();
		expect(meta.unprovenanced).toBeDefined();
	});

	it("layers unwraps_to for a provenance-node entity (data tag c/n/p/u)", async () => {
		const c = new HttpConnector({ baseUrl: BASE, storage, fetchFn: makeFetch() });
		await c.refresh();
		const uRef = c.internEntity(uNode.idString()); // bare entity whose data half is a u-node

		const meta = await c.getEntityMetadata(uRef);
		expect(meta.unwraps_to_id_string).toBe(baseData.idString());
		// unwraps_to is the LAYERED (unwrapped, data) form: (baseData, uNode).
		expect(c.entityKeyFor((meta.unwraps_to as EntityRefStub).shortId)).toEqual({
			dataIdString: baseData.idString(),
			provIdString: uNode.idString(),
		});
	});
});
