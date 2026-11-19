import { describe, expect, it } from "vitest";
import { EntityDataError, EntityID, isDataMap, isEntityData, isPartial, markPartial, typeNameOf, type EntityData } from "../src/index";

describe("entity data", () => {
  it("reads the type name off the tag", () => {
    const data: EntityData = { $type: "org.example.Note", body: "hi", ref: new EntityID("bafy1") };
    expect(typeNameOf(data)).toBe("org.example.Note");
  });
  it("faults on a missing or empty tag", () => {
    expect(() => typeNameOf({ body: "hi" } as unknown as EntityData)).toThrow(EntityDataError);
    expect(() => typeNameOf({ $type: "" } as EntityData)).toThrow(EntityDataError);
  });
  it("tells maps and entity data from other values", () => {
    expect(isDataMap({})).toBe(true);
    expect(isDataMap([])).toBe(false);
    expect(isDataMap(new Uint8Array())).toBe(false);
    expect(isDataMap(new EntityID("bafy1"))).toBe(false);
    expect(isEntityData({ $type: "t" })).toBe(true);
    expect(isEntityData({ type: "t" })).toBe(false);
  });
});

describe("markPartial / isPartial", () => {
  it("tells the marked maps of a value from the finished ones, without changing the data", () => {
    const finished = { $type: "t.Turn", text: "done" };
    const inFlight = markPartial({ $type: "t.Turn", text: "hal" });
    const value = { $type: "t.Rollout", turns: [finished, inFlight] };
    expect(isPartial(inFlight)).toBe(true);
    expect(isPartial(finished)).toBe(false);
    expect(isPartial(value)).toBe(false);
    expect(Object.keys(inFlight)).toEqual(["$type", "text"]);
    expect(JSON.stringify(inFlight)).toBe('{"$type":"t.Turn","text":"hal"}');
    // A copy is a new map, unmarked.
    expect(isPartial({ ...inFlight })).toBe(false);
    for (const leaf of [null, undefined, "s", 1, [inFlight]]) expect(isPartial(leaf as never)).toBe(false);
  });
});
