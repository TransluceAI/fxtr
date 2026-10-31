import { describe, expect, expectTypeOf, it } from "vitest";
import { EntityInlineLink, declareView, renderer, type Renderer } from "../src/index";
import { declareViewSlot } from "../src/slots";

const Card = declareViewSlot<{ readonly label: string }>("Card");
const Label = ({ label }: { readonly label: string }) => label;

describe("renderer", () => {
  it("builds a frozen renderer for a slot and subject type", () => {
    const r = renderer(Card, "org.example.Note", "default", Label);
    expect(Object.isFrozen(r)).toBe(true);
    expect(r).toEqual({ slot: Card, subjectType: "org.example.Note", id: "default", Component: Label });
    expectTypeOf(r).toEqualTypeOf<Renderer<{ readonly label: string }>>();
  });
  it("takes Props from the slot, not the component", () => {
    // @ts-expect-error a component wanting props the slot does not promise
    renderer(Card, "t", "x", (p: { other: number }) => p.other);
    const NeedsStorage = (p: { readonly storage: unknown }) => String(p.storage);
    // @ts-expect-error a link renderer wanting a panel's storage
    renderer(EntityInlineLink, "t", "x", NeedsStorage);
  });
});

describe("declareView", () => {
  it("names a view within another below it, reading its params, and a renderer of it carries both", () => {
    const CHART = declareView(Card, "org.example.Run", "org.example.Chart");
    const POINT = declareView(Card, "org.example.Run", "point", { within: CHART, params: ["point"] });
    expect(POINT).toMatchObject({ id: "org.example.Chart/point", within: "org.example.Chart", params: ["point"] });
    expect(renderer(POINT, Label)).toMatchObject({ id: "org.example.Chart/point", within: "org.example.Chart", params: ["point"] });
    expect(renderer(CHART, Label)).toEqual({ slot: Card, subjectType: "org.example.Run", id: "org.example.Chart", Component: Label });
  });
  it("refuses a view within one of another subject", () => {
    const CHART = declareView(Card, "org.example.Run", "chart");
    expect(() => declareView(Card, "org.example.Other", "point", { within: CHART })).toThrow(/another subject/);
  });
});
