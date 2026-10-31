import { describe, expect, it } from "vitest";
import {
  ANY_SUBJECT_TYPE,
  EntityHoverPreview,
  EntityInlineLink,
  InvocationPanel,
  renderer,
  type EntityViewProps,
  type InvocationPanelProps,
} from "fxtr-view";
import { DispatchFault, chosenRenderer, dispatchRegistry, renderersFor, shownRenderer } from "../src/index";

const Label = ({ id }: EntityViewProps) => id.cid;
const Alt = ({ id }: EntityViewProps) => `alt ${id.cid}`;
const Any = ({ id }: EntityViewProps) => `any ${id.cid}`;
const Chat = ({ invocations }: InvocationPanelProps) => invocations.length;
const Raw = ({ invocations }: InvocationPanelProps) => invocations[0]?.ref.kind ?? null;

describe("dispatchRegistry", () => {
  it("builds a frozen registry", () => {
    const chat = renderer(InvocationPanel, "rollout", "chat", Chat);
    const registry = dispatchRegistry([chat]);
    expect(Object.isFrozen(registry)).toBe(true);
    expect(Object.isFrozen(registry.renderers)).toBe(true);
    expect(registry.renderers).toEqual([chat]);
  });
  it("allows one id under several subject types and slots, in registration order", () => {
    const registry = dispatchRegistry([
      renderer(InvocationPanel, "rollout", "chat", Chat),
      renderer(InvocationPanel, "rollout", "raw", Raw),
      renderer(InvocationPanel, "grade", "chat", Chat),
      renderer(EntityInlineLink, "rollout", "chat", Label),
    ]);
    expect(registry.renderers.map((r) => `${r.slot.debugName}/${r.subjectType}/${r.id}`)).toEqual([
      "InvocationPanel/rollout/chat",
      "InvocationPanel/rollout/raw",
      "InvocationPanel/grade/chat",
      "EntityInlineLink/rollout/chat",
    ]);
  });
  it("faults on two renderers of one slot and subject type sharing an id", () => {
    expect(() =>
      dispatchRegistry([renderer(InvocationPanel, "rollout", "chat", Chat), renderer(InvocationPanel, "rollout", "chat", Raw)]),
    ).toThrow(DispatchFault);
  });
  it("faults on a wildcard sharing an id with anything of its slot, either way round", () => {
    expect(() => dispatchRegistry([renderer(EntityHoverPreview, "t", "x", Label), renderer(EntityHoverPreview, ANY_SUBJECT_TYPE, "x", Any)])).toThrow(DispatchFault);
    expect(() => dispatchRegistry([renderer(EntityHoverPreview, ANY_SUBJECT_TYPE, "x", Any), renderer(EntityHoverPreview, "t", "x", Label)])).toThrow(DispatchFault);
    expect(() => dispatchRegistry([renderer(EntityHoverPreview, ANY_SUBJECT_TYPE, "x", Any), renderer(EntityHoverPreview, ANY_SUBJECT_TYPE, "x", Label)])).toThrow(
      DispatchFault,
    );
  });
});

describe("renderersFor", () => {
  const registry = dispatchRegistry([
    renderer(EntityHoverPreview, "org.example.Note", "default", Label),
    renderer(EntityHoverPreview, "org.example.Note", "alt", Alt),
    renderer(EntityHoverPreview, ANY_SUBJECT_TYPE, "any", Any),
    renderer(InvocationPanel, "rollout", "chat", Chat),
  ]);

  it("lists a subject type's renderers in registration order, then the slot's wildcards", () => {
    expect(renderersFor(registry, EntityHoverPreview, "org.example.Note").map((r) => r.id)).toEqual(["default", "alt", "any"]);
    expect(renderersFor(registry, EntityHoverPreview, "org.example.Nope").map((r) => r.id)).toEqual(["any"]);
    expect(renderersFor(registry, InvocationPanel, "rollout").map((r) => r.id)).toEqual(["chat"]);
    expect(renderersFor(registry, InvocationPanel, "grade")).toEqual([]);
  });
  it("answers for a slot nothing was registered under", () => {
    expect(renderersFor(registry, EntityInlineLink, "org.example.Note")).toEqual([]);
  });
  it("faults when asked for the wildcard as a subject type", () => {
    expect(() => renderersFor(registry, EntityHoverPreview, ANY_SUBJECT_TYPE)).toThrow(DispatchFault);
  });
});

describe("chosenRenderer", () => {
  const offered = [renderer(InvocationPanel, "rollout", "chat", Chat), renderer(InvocationPanel, "rollout", "raw", Raw)];

  it("keeps a remembered id the renderers still offer", () => {
    expect(chosenRenderer(offered, "raw")).toBe("raw");
  });
  it("falls back to the default (null) for a gone id or no choice", () => {
    expect(chosenRenderer(offered, "gone")).toBeNull();
    expect(chosenRenderer(offered, null)).toBeNull();
    expect(chosenRenderer([], "raw")).toBeNull();
  });
});

describe("shownRenderer", () => {
  const chat = renderer(InvocationPanel, "rollout", "chat", Chat);
  const raw = renderer(InvocationPanel, "rollout", "raw", Raw);

  it("shows the chosen renderer, or the first for no choice", () => {
    expect(shownRenderer([chat, raw], "raw")).toBe(raw);
    expect(shownRenderer([chat, raw], null)).toBe(chat);
  });
  it("shows nothing for an id not offered, or no renderers", () => {
    expect(shownRenderer([chat, raw], "gone")).toBeUndefined();
    expect(shownRenderer([], null)).toBeUndefined();
  });
});
