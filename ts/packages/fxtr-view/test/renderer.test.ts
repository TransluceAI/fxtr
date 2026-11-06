import { describe, expect, expectTypeOf, it } from "vitest";
import { EntityID, EntityInlineLink, EntityPanel, declareView, renderer, useLink, type EntityLinkProps, type ParamValue, type Renderer, type ViewProps } from "../src/index";
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

describe("params", () => {
  const Run = declareViewSlot<{ readonly label: string }>("Run");
  const BATCH = declareView(Run, "org.example.Step", "batch", { params: ["prompt"] });
  const CONVERSATION = declareView(EntityPanel, "org.example.Transcript", "conversation", { params: ["selector"] });

  it("binds every param of a view of another slot, and leaves an entity view's optional, each any JSON", () => {
    expectTypeOf<ViewProps<typeof BATCH>["params"]>().toEqualTypeOf<{ readonly prompt: ParamValue }>();
    expectTypeOf<ViewProps<typeof CONVERSATION>["params"]["selector"]>().toEqualTypeOf<ParamValue | undefined>();
  });
  it("makes a link to a view bind its required params, and to an entity view none", () => {
    // Type-only: these calls are never run.
    const check = () => {
      useLink(BATCH, { prompt: 3 });
      // @ts-expect-error the link leaves out a required param
      useLink(BATCH, {});
      useLink(CONVERSATION);
      useLink(CONVERSATION, { selector: { message: 2 } });
      useLink({ to: "/judge" });
    };
    void check;
  });
  it("lets an entity link carry params to the entity alone, not at a place", () => {
    const id = new EntityID("cid1");
    const alone: EntityLinkProps = { id, params: { selector: { message: 2 } } };
    // @ts-expect-error a link at a place carries no params
    const placed: EntityLinkProps = { id, at: { path: ["transcript"] }, params: { selector: 2 } };
    void alone;
    void placed;
  });
});
