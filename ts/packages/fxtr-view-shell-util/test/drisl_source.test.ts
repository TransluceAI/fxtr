import * as dagCbor from "@ipld/dag-cbor";
import { CID } from "multiformats/cid";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EntityDataError, EntityID, isEntityID } from "fxtr-view";
import { createBatchedDrislEntityLoader, decodeEntityData, toDataValue } from "../src/index";

const SOME_CID = CID.parse("bafyreigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi");

describe("toDataValue", () => {
  it("turns CID leaves into EntityIDs, at any depth", () => {
    const value = toDataValue({ ref: SOME_CID, list: [SOME_CID, 1], nested: { ref: SOME_CID } });
    expect(isEntityID((value as { ref: unknown }).ref)).toBe(true);
    expect((value as { ref: EntityID }).ref.cid).toBe(SOME_CID.toString());
    expect(isEntityID((value as { list: unknown[] }).list[0])).toBe(true);
    expect(isEntityID((value as { nested: { ref: unknown } }).nested.ref)).toBe(true);
  });

  it("keeps scalars, bytes, bigints and null as they are", () => {
    const bytes = new Uint8Array([1, 2, 3]);
    expect(toDataValue({ a: 1, b: "s", c: true, d: null, e: bytes, f: 10n })).toEqual({
      a: 1,
      b: "s",
      c: true,
      d: null,
      e: bytes,
      f: 10n,
    });
    expect(toDataValue(undefined)).toBeNull();
  });
});

describe("decodeEntityData", () => {
  it("decodes tagged DAG-CBOR bytes to entity data with typed references", () => {
    const bytes = dagCbor.encode({ $type: "org.example.Thing", ref: SOME_CID, n: 2 });
    const data = decodeEntityData(bytes, "cid");
    expect(data.$type).toBe("org.example.Thing");
    expect(isEntityID(data["ref"])).toBe(true);
    expect(data["n"]).toBe(2);
  });

  it("rejects bytes that are not a tagged map", () => {
    expect(() => decodeEntityData(dagCbor.encode([1, 2]), "cid")).toThrow(EntityDataError);
    expect(() => decodeEntityData(dagCbor.encode({ n: 1 }), "cid")).toThrow(EntityDataError);
  });
});

describe("createBatchedDrislEntityLoader", () => {
  const URL = "/api/entities";

  /** A fake backend: records each request's cids and answers from `stored`, holding every
   *  response until `release` so the test can see what is in flight. */
  function fakeBackend(stored: Record<string, Uint8Array>) {
    const requests: string[][] = [];
    const held: (() => void)[] = [];
    const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe(URL);
      expect(init.method).toBe("POST");
      const { cids } = JSON.parse(init.body as string) as { cids: string[] };
      requests.push(cids);
      await new Promise<void>((resolve) => held.push(resolve));
      return new Response(dagCbor.encode(cids.map((cid) => stored[cid] ?? null)).slice());
    });
    vi.stubGlobal("fetch", fetchMock);
    const release = async () => {
      for (const resolve of held.splice(0)) resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
    };
    return { requests, release };
  }

  const thing = (n: number) => dagCbor.encode({ $type: "org.example.Thing", n });
  const cid = (n: number) => `cid${n}`;

  afterEach(() => vi.unstubAllGlobals());

  it("asks for everything requested in one task together, in batches, a few at a time", async () => {
    const stored = Object.fromEntries(Array.from({ length: 25 }, (_, n) => [cid(n), thing(n)]));
    const backend = fakeBackend(stored);
    const load = createBatchedDrislEntityLoader(URL, { batchSize: 10, maxInFlight: 2 });

    const loads = Array.from({ length: 25 }, (_, n) => load(new EntityID(cid(n))));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(backend.requests.map((r) => r.length)).toEqual([10, 10]);
    await backend.release();
    expect(backend.requests.map((r) => r.length)).toEqual([10, 10, 5]);
    await backend.release();

    const values = await Promise.all(loads);
    expect(values.map((v) => v["n"])).toEqual(Array.from({ length: 25 }, (_, n) => n));
    expect(backend.requests.flat()).toEqual(Array.from({ length: 25 }, (_, n) => cid(n)));
  });

  it("fails an entity the backend does not have, and only that one", async () => {
    const backend = fakeBackend({ [cid(1)]: thing(1) });
    const load = createBatchedDrislEntityLoader(URL);
    const present = load(new EntityID(cid(1)));
    const absent = expect(load(new EntityID(cid(2)))).rejects.toThrow(`entity ${cid(2)}: not stored`);
    await new Promise((resolve) => setTimeout(resolve, 0));
    await backend.release();
    expect((await present)["n"]).toBe(1);
    await absent;
    expect(backend.requests).toEqual([[cid(1), cid(2)]]);
  });

  it("fails every entity of a batch whose request fails", async () => {
    const fail = async () => new Response("the body is not JSON", { status: 400, statusText: "Bad Request" });
    vi.stubGlobal("fetch", vi.fn(fail));
    const load = createBatchedDrislEntityLoader(URL);
    await Promise.all(
      [cid(1), cid(2)].map((c) => expect(load(new EntityID(c))).rejects.toThrow("400 Bad Request: the body is not JSON")),
    );
  });

  it("rejects options that could never send a request", () => {
    expect(() => createBatchedDrislEntityLoader(URL, { batchSize: 0 })).toThrow(RangeError);
    expect(() => createBatchedDrislEntityLoader(URL, { maxInFlight: 1.5 })).toThrow(RangeError);
  });
});
