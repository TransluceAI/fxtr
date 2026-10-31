import { describe, expect, it } from "vitest";
import { ARRAY_TYPE, EntityPanel, EntityHoverPreview, EntityInlineLink, InvocationPanel } from "fxtr-view";
import { dispatchRegistry, renderersFor } from "fxtr-view-shell-util";
import { ArrayInlineLink } from "fxtr-view-array-table";
import { CborTreeHoverPreview, CborTreeInlineLink, CborTreePanel, ENTITY_DATA } from "fxtr-view-cbor-tree";
import { defaultRenderers } from "../src/index";

const defaultRegistry = dispatchRegistry(defaultRenderers);

describe("defaultRenderers", () => {
  it("offers an Array its table first, then the data tree", () => {
    expect(renderersFor(defaultRegistry, EntityPanel, ARRAY_TYPE).map((r) => r.id)).toEqual(["table", ENTITY_DATA]);
    expect(renderersFor(defaultRegistry, EntityPanel, ARRAY_TYPE)[1]?.Component).toBe(CborTreePanel);
    expect(renderersFor(defaultRegistry, EntityInlineLink, ARRAY_TYPE).map((r) => r.Component)).toEqual([ArrayInlineLink, CborTreeInlineLink]);
  });

  it("gives any other type the cbor-tree views in every built-in entity slot", () => {
    expect(renderersFor(defaultRegistry, EntityPanel, "org.example.Nothing").map((r) => r.Component)).toEqual([CborTreePanel]);
    expect(renderersFor(defaultRegistry, EntityInlineLink, "org.example.Nothing").map((r) => r.Component)).toEqual([CborTreeInlineLink]);
    expect(renderersFor(defaultRegistry, EntityHoverPreview, "org.example.Nothing").map((r) => r.Component)).toEqual([CborTreeHoverPreview]);
  });

  it("registers nothing under the invocation slot", () => {
    expect(renderersFor(defaultRegistry, InvocationPanel, "rollout")).toEqual([]);
  });
});
