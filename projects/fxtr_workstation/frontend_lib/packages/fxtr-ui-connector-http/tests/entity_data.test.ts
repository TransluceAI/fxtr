import { type FxtrCbor, fxtrCborDumps } from "@fxtr/core";
import { describe, expect, it } from "vitest";

import { EntityFetcher, EntityLoadError, type FetchFn } from "../src/entity_data";

/** A `Response` stand-in carrying a CBOR-encoded `/data` envelope. */
function cborResponse(envelope: FxtrCbor, status = 200): Response {
	const bytes = fxtrCborDumps(envelope);
	return {
		status,
		ok: status >= 200 && status < 300,
		arrayBuffer: async () => bytes.slice().buffer,
	} as unknown as Response;
}

/** A fetch that records urls and replies from a per-url map. */
function fetchFrom(map: Record<string, Response>): { fetchFn: FetchFn; urls: string[] } {
	const urls: string[] = [];
	return {
		urls,
		fetchFn: async (url) => {
			urls.push(url);
			const r = map[url];
			if (!r) throw new Error(`unexpected fetch ${url}`);
			return r;
		},
	};
}

describe("EntityFetcher", () => {
	it("fetches /data/{id}, decodes the entity tree, and ingests precomp", async () => {
		const tree: FxtrCbor = { name: "root", count: 5 };
		const envelope: FxtrCbor = {
			bytes: fxtrCborDumps(tree),
			precomp: { did1: { unwraps_to: "did2" } },
		};
		const { fetchFn, urls } = fetchFrom({
			"http://host:8000/data/did1": cborResponse(envelope),
		});
		const fetcher = new EntityFetcher("http://host:8000", fetchFn);

		const out = await fetcher.load("did1");
		expect(out).toEqual({ name: "root", count: 5 });
		expect(urls).toEqual(["http://host:8000/data/did1"]);
		expect(fetcher.precompFor("did1")).toEqual({ unwraps_to: "did2" });
		expect(fetcher.precompFor("unknown")).toBeUndefined();
	});

	it("joins a trailing-slash base URL without doubling", async () => {
		const envelope: FxtrCbor = { bytes: fxtrCborDumps({ ok: true }) };
		const { fetchFn, urls } = fetchFrom({ "http://host/data/d1": cborResponse(envelope) });
		const fetcher = new EntityFetcher("http://host/", fetchFn);
		await fetcher.load("d1");
		expect(urls).toEqual(["http://host/data/d1"]);
	});

	it("throws EntityLoadError on 404 / 503 / other non-2xx", async () => {
		for (const [status, re] of [
			[404, /not found/],
			[503, /scope is closed/],
			[500, /HTTP 500/],
		] as const) {
			const { fetchFn } = fetchFrom({
				"http://h/data/d": cborResponse({ bytes: fxtrCborDumps(null) }, status),
			});
			const fetcher = new EntityFetcher("http://h", fetchFn);
			await expect(fetcher.load("d")).rejects.toThrow(EntityLoadError);
			await expect(new EntityFetcher("http://h", fetchFn).load("d")).rejects.toThrow(re);
		}
	});
});
