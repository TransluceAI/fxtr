import { fireEvent, render, screen } from "@testing-library/react";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";
import { EntityID, EntityLink, EntityHoverPreview, EntityPanel, PartialEntityInline, PartialEntityPanel, type EntityData, type JsonValue } from "fxtr-view";
import { RenderEntity, RenderValue, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import { cborTreeRenderers, shortName } from "../src/index";

const entities = new Map<string, EntityData>([
  ["note1", { $type: "org.example.Note", body: "hi", n: 3n, flag: true, blob: new Uint8Array([1, 2]), tags: ["a", "b"], child: new EntityID("cfg1"), nested: { deep: null } }],
  ["cfg1", { $type: "org.example.Config", model: "m" }],
]);
const registry = dispatchRegistry(cborTreeRenderers);

describe("CborTreePanel", () => {
  it("renders the type, the cid, every kind of leaf, and links for ids", () => {
    const storage = atom<JsonValue>(null);
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("note1")} storage={storage} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".type")?.textContent).toBe("org.example.Note");
    expect(screen.getByText('"hi"')).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("true")).toBeTruthy();
    expect(screen.getByText(/bytes\[2\] 0102/)).toBeTruthy();
    expect(screen.getByText("null")).toBeTruthy();
    // The child id is an inline EntityLink rendered through the EntityInlineLink key.
    const link = document.querySelector(".fxtr-view-link .link")!;
    expect(link.textContent).toBe("Config cfg1");
  });
  it("lists the root's fields with no toggle of their own", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("note1")} storage={atom<JsonValue>(null)} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".panel > .branch")).toBeNull();
    expect(document.querySelector(".panel > ul.root")?.children.length).toBe(8);
  });
  it("collapses and expands subtrees, keeping the state in storage", () => {
    const storage = atom<JsonValue>(null);
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("note1")} storage={storage} />
      </TableShellProvider>,
    );
    const tagsToggle = screen.getByText("▾ [2]");
    fireEvent.click(tagsToggle);
    expect(screen.getByText("▸ [2]")).toBeTruthy();
    expect(screen.queryByText('"a"')).toBeNull();
    fireEvent.click(screen.getByText("▸ [2]"));
    expect(screen.getByText('"a"')).toBeTruthy();
  });
});

describe("CborTreeHoverPreview", () => {
  it("shows the type, cid, and the first fields compactly, with a count of the rest", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityHoverPreview} id={new EntityID("note1")} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".preview .type")?.textContent).toBe("org.example.Note");
    const keys = Array.from(document.querySelectorAll(".preview dt")).map((dt) => dt.textContent);
    expect(keys).toEqual(["body", "n", "flag", "blob", "tags", "child"]);
    expect(screen.getByText("[2]")).toBeTruthy(); // tags, as a size
    expect(document.querySelector(".preview .fxtr-view-link")?.textContent).toBe("Config cfg1");
    expect(document.querySelector(".previewMore")?.textContent).toBe("and 1 more");
  });
});

describe("links", () => {
  it("renders an inline link for any type", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <EntityLink id={new EntityID("cfg1")} />
      </TableShellProvider>,
    );
    expect(document.querySelector("span.fxtr-view-link")?.textContent).toBe("Config cfg1");
  });
  it("labels an unloadable entity generically", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <EntityLink id={new EntityID("missing")} />
      </TableShellProvider>,
    );
    // RenderEntity shows the error fallback for a failed load; the link itself never renders.
    expect(screen.getByRole("alert").textContent).toBe("unknown missing");
  });
  it("shortens dotted names", () => {
    expect(shortName("org.transluce.fxtr.core.Array")).toBe("Array");
    expect(shortName("Plain")).toBe("Plain");
    expect(shortName("org.example.Report.v2")).toBe("Report.v2");
    expect(shortName("v2")).toBe("v2");
  });
});

describe("CborTreePartialPanel", () => {
  it("shows a value still being produced as the same tree as the finished entity, marked partial", () => {
    const value = { ...entities.get("note1")!, body: null } as EntityData;
    const { container } = render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderValue slot={PartialEntityPanel} value={value} partial live storage={atom<JsonValue>(null)} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".type")?.textContent).toBe("org.example.Note");
    expect(screen.getByText("partial")).toBeTruthy();
    // Every field the finished entity's tree shows, the one not yet known as null.
    expect(container.querySelector(".panel > ul.root")?.children.length).toBe(8);
    expect(screen.getAllByText("null").length).toBe(2);
    expect(document.querySelector(".fxtr-view-link .link")?.textContent).toBe("Config cfg1");
  });

  it("says a partial value will not be finished once its step has stopped", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderValue slot={PartialEntityPanel} value={entities.get("cfg1")!} partial live={false} storage={atom<JsonValue>(null)} />
      </TableShellProvider>,
    );
    expect(screen.getByText("partial: stopped before finishing")).toBeTruthy();
  });

  it("says nothing of partiality for a finished value", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderValue slot={PartialEntityPanel} value={entities.get("cfg1")!} partial={false} live storage={atom<JsonValue>(null)} />
      </TableShellProvider>,
    );
    expect(screen.queryByText("partial")).toBeNull();
    expect(screen.getByText('"m"')).toBeTruthy();
  });

  it("says in one line what type a value being produced is, and whether its step still runs", () => {
    const lineOf = (partial: boolean, live: boolean) => render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderValue slot={PartialEntityInline} value={entities.get("cfg1")!} partial={partial} live={live} />
      </TableShellProvider>,
    ).container.textContent;
    expect(lineOf(true, true)).toBe("Config partial");
    expect(lineOf(true, false)).toBe("Config partial: stopped");
    expect(lineOf(false, true)).toBe("Config");
  });
});
