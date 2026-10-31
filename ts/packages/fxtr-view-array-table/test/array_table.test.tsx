import { render, screen } from "@testing-library/react";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";
import { ARRAY_TYPE, EntityID, EntityLink, EntityPanel, formatSchema, isArrayData, type ArrayData, type EntityData, type JsonValue } from "fxtr-view";
import { RenderEntity, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import { arrayTableRenderers } from "../src/index";

const grades: ArrayData = {
  $type: ARRAY_TYPE,
  dims: [["model", "str"], ["sample", "int"]],
  schema: { struct: { score: "float", transcript: { id: "org.example.Transcript" }, notes: { nullable: "str" } } },
  rows: [
    ["a", 0, { score: 0.5, transcript: new EntityID("t1"), notes: null }],
    ["a", 1, { score: 0.75, transcript: new EntityID("t2"), notes: "ok" }],
  ],
};
const scalar: ArrayData = { $type: ARRAY_TYPE, dims: [], schema: { list: "int" }, rows: [[[1, 2, 3]]] };
const entities = new Map<string, EntityData>([
  ["grades", grades],
  ["scalar", scalar],
  ["t1", { $type: "org.example.Transcript" }],
  ["t2", { $type: "org.example.Transcript" }],
  ["odd", { $type: ARRAY_TYPE, rows: "nope" }],
  ["empty", { $type: ARRAY_TYPE, dims: [["k", "int"]], schema: "str", rows: [] }],
]);
const registry = dispatchRegistry(arrayTableRenderers);
const storage = atom<JsonValue>(null);

describe("ArrayTablePanel", () => {
  it("renders dims, schema, and one column per dim and struct field", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("grades")} storage={storage} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".dims")?.textContent).toBe("[model: str, sample: int]");
    expect(document.querySelector(".schema")?.textContent).toBe(
      "{score: float, transcript: id[org.example.Transcript], notes: str | None}",
    );
    expect(document.querySelector(".count")?.textContent).toBe("2 rows");
    const headers = Array.from(document.querySelectorAll("th")).map((th) => th.textContent);
    expect(headers).toEqual(["model", "sample", "score", "transcript", "notes"]);
    const firstRow = Array.from(document.querySelectorAll("tbody tr")[0]!.querySelectorAll("td")).map((td) => td.textContent);
    expect(firstRow).toEqual(["a", "0", "0.5", "no view for org.example.Transcript", "null"]);
    // Ids in cells are EntityLinks (dispatched through EntityInlineLink; here the placeholder no-match).
    expect(document.querySelectorAll(".fxtr-view-link").length).toBe(2);
  });
  it("renders a scalar array with a single value column", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("scalar")} storage={storage} />
      </TableShellProvider>,
    );
    expect(Array.from(document.querySelectorAll("th")).map((th) => th.textContent)).toEqual(["value"]);
    expect(document.querySelector("tbody td")?.textContent).toBe("[1, 2, 3]");
  });
  it("marks an empty array", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("empty")} storage={storage} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".empty")?.textContent).toBe("empty");
    expect(document.querySelector(".count")?.textContent).toBe("0 rows");
  });
  it("shows an error for Array-tagged data that is not array-shaped", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("odd")} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByRole("alert").textContent).toMatch(/not Array-shaped/);
  });
});

describe("ArrayInlineLink", () => {
  it("summarizes dims and row count", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <EntityLink id={new EntityID("grades")} />
      </TableShellProvider>,
    );
    expect(document.querySelector(".link")?.textContent).toBe("Array[model: str, sample: int] · 2 rows");
  });
});

describe("array data helpers", () => {
  it("formats schemas and checks shape", () => {
    expect(formatSchema("int")).toBe("int");
    expect(formatSchema({ list: { nullable: "id" } })).toBe("list[id | None]");
    expect(isArrayData(grades)).toBe(true);
    expect(isArrayData({ $type: "other" })).toBe(false);
  });
});
