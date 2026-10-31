import { describe, expect, it } from "vitest";
import { EntityDataError, EntityID, isDataMap, isEntityData, typeNameOf, type EntityData } from "../src/index";

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
