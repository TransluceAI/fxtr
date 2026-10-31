import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EntityID, EntityLink, EntityInlineLink, renderer, type EntityData, type EntityViewProps } from "fxtr-view";
import { dispatchRegistry } from "../src/index";
import { TableShellProvider } from "../testing";

const entities = new Map<string, EntityData>([["note1", { $type: "org.example.Note" }]]);
function NoteInline({ id }: EntityViewProps) {
  return <em>note {id.cid}</em>;
}
const registry = dispatchRegistry([renderer(EntityInlineLink, "org.example.Note", "default", NoteInline)]);

describe("EntityLink under a TableShellProvider", () => {
  it("renders the dispatched inline link view in the pass-through span", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <EntityLink id={new EntityID("note1")} />
      </TableShellProvider>,
    );
    const wrapper = document.querySelector(".fxtr-view-link")!;
    expect(wrapper.tagName).toBe("SPAN");
    expect(wrapper.textContent).toBe("note note1");
  });
  it("shows the default error fallback for an entity not in the table", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <EntityLink id={new EntityID("missing")} />
      </TableShellProvider>,
    );
    expect(document.querySelector("[role=alert]")?.textContent).toBe("unknown missing");
  });
});
