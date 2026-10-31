import { describe, expect, expectTypeOf, it } from "vitest";
import { EntityDataError, type EntityData } from "../src/index";
import { EntityID, idFromJson, idKey, idToJson, isEntityID, shortCid, splitCid } from "../src/ids";

describe("shortCid / splitCid", () => {
  it("drops the common prefix and keeps the first characters, git style", () => {
    expect(shortCid("bafyreiabcdefghijklmnopqrstuvwxyz")).toBe("abcdefgh");
    expect(shortCid("bafyreiabc")).toBe("abc");
    expect(shortCid("zQmOtherKindOfCid")).toBe("zQmOther");
    expect(shortCid("short")).toBe("short");
  });
  it("splits so that prefix + short + rest is the cid", () => {
    const cid = "bafyreiabcdefghijklmnop";
    const parts = splitCid(cid);
    expect(parts).toEqual({ prefix: "bafyrei", short: "abcdefgh", rest: "ijklmnop" });
    expect(parts.prefix + parts.short + parts.rest).toBe(cid);
    expect(splitCid("plain")).toEqual({ prefix: "", short: "plain", rest: "" });
  });
});

describe("EntityID", () => {
  it("is frozen and rejects an empty cid", () => {
    const id = new EntityID("bafy1");
    expect(Object.isFrozen(id)).toBe(true);
    expect(() => new EntityID("")).toThrow(EntityDataError);
  });
  it("keys and compares by cid", () => {
    expect(idKey(new EntityID("bafy1"))).toBe("bafy1");
    expect(new EntityID("bafy1").equals(new EntityID("bafy1"))).toBe(true);
    expect(new EntityID("bafy1").equals(new EntityID("bafy2"))).toBe(false);
  });
  it("distinguishes ids from look-alike data", () => {
    expect(isEntityID(new EntityID("bafy1"))).toBe(true);
    expect(isEntityID({ cid: "bafy1" })).toBe(false);
  });
  it("round-trips through JSON explicitly and via JSON.stringify", () => {
    const id = new EntityID("bafy1");
    expect(idToJson(id)).toEqual({ cid: "bafy1" });
    const back = idFromJson(JSON.parse(JSON.stringify(id)));
    expect(back).toBeInstanceOf(EntityID);
    expect(back.equals(id)).toBe(true);
    expect(idFromJson(id)).toBe(id);
  });
  it("rejects malformed JSON forms", () => {
    expect(() => idFromJson(null)).toThrow(EntityDataError);
    expect(() => idFromJson({ cid: "" })).toThrow(EntityDataError);
    expect(() => idFromJson({ cid: 5 })).toThrow(EntityDataError);
  });
  it("is covariant in its phantom data type", () => {
    interface NoteData extends EntityData {
      readonly $type: "org.example.Note";
      readonly body: string;
    }
    const typed = new EntityID<NoteData>("bafy1");
    expectTypeOf(typed).toMatchTypeOf<EntityID>();
    expectTypeOf<EntityID>().not.toMatchTypeOf<EntityID<NoteData>>();
  });
});
