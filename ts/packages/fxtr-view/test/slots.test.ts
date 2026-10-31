import { describe, expect, expectTypeOf, it } from "vitest";
import { declareViewSlot, type ViewSlot } from "../src/slots";

describe("view slots", () => {
  it("has a fresh symbol per declaration", () => {
    const a = declareViewSlot<{ x: number }>("A");
    const b = declareViewSlot<{ x: number }>("A");
    expect(a.symbol).not.toBe(b.symbol);
    expect(a.debugName).toBe("A");
    expect(Object.isFrozen(a)).toBe(true);
  });
  it("is branded invariantly by its props", () => {
    type Wide = ViewSlot<{ id: string }>;
    type Narrow = ViewSlot<{ id: string; extra: number }>;
    expectTypeOf<Narrow>().not.toMatchTypeOf<Wide>();
    expectTypeOf<Wide>().not.toMatchTypeOf<Narrow>();
  });
});
