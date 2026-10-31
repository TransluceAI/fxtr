import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DataTable, type TableColumn } from "../src/index";

interface Item {
  readonly name: string;
  readonly score: number | null;
}

const ITEMS: readonly Item[] = [
  { name: "item #10", score: 2 },
  { name: "item #9", score: null },
  { name: "Other", score: 7 },
];

const COLUMNS: readonly TableColumn<Item>[] = [
  { key: "name", label: "name", value: (item) => item.name },
  { key: "score", label: "score", value: (item) => item.score, numeric: true, cell: (item) => item.score ?? "—" },
  { key: "note", label: "note", cell: () => "x" },
];

/** The first cell of each body row. */
const names = () =>
  within(screen.getAllByRole("rowgroup")[1]!)
    .getAllByRole("row")
    .map((row) => within(row).getAllByRole("cell")[0]!.textContent);

const renderTable = () => render(<DataTable rows={ITEMS} columns={COLUMNS} rowKey={(item) => item.name} />);

describe("DataTable", () => {
  it("keeps the rows' order until a header sorts them, cycling up, down, and back", () => {
    renderTable();
    expect(names()).toEqual(["item #10", "item #9", "Other"]);
    const sortByName = screen.getByTitle("Sort by name");
    fireEvent.click(sortByName);
    expect(names()).toEqual(["item #9", "item #10", "Other"]);
    fireEvent.click(sortByName);
    expect(names()).toEqual(["Other", "item #10", "item #9"]);
    fireEvent.click(sortByName);
    expect(names()).toEqual(["item #10", "item #9", "Other"]);
  });

  it("sorts numbers by size with empty values last either way", () => {
    renderTable();
    const sortByScore = screen.getByTitle("Sort by score");
    fireEvent.click(sortByScore);
    expect(names()).toEqual(["item #10", "Other", "item #9"]);
    fireEvent.click(sortByScore);
    expect(names()).toEqual(["Other", "item #10", "item #9"]);
  });

  it("gives a filter to text columns with a value only, and counts what it keeps", () => {
    renderTable();
    expect(screen.queryByLabelText("Filter score")).toBeNull();
    expect(screen.queryByLabelText("Filter note")).toBeNull();
    fireEvent.click(screen.getByLabelText("Filter name"));
    const input = screen.getByLabelText("name filter");
    fireEvent.change(input, { target: { value: "ITEM" } });
    expect(names()).toEqual(["item #10", "item #9"]);
    expect(screen.getByText("2 of 3 rows")).toBeTruthy();
    fireEvent.change(input, { target: { value: "nothing" } });
    expect(screen.getByText("no rows match the filters")).toBeTruthy();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(names()).toEqual(["item #10", "item #9", "Other"]);
    expect(screen.queryByLabelText("name filter")).toBeNull();
  });

  it("hands each row's props and selection to `renderRow`", () => {
    const onClick = vi.fn();
    render(
      <DataTable
        rows={ITEMS}
        columns={COLUMNS}
        rowKey={(item) => item.name}
        renderRow={(item, tr) => tr({ onClick: () => onClick(item.name) }, item.name === "Other")}
      />,
    );
    const other = screen.getByText("Other").closest("tr")!;
    expect(other.hasAttribute("data-selected")).toBe(true);
    expect(other.hasAttribute("data-clickable")).toBe(true);
    fireEvent.click(other);
    expect(onClick).toHaveBeenCalledWith("Other");
  });

  it("refuses repeated row keys", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      expect(() => render(<DataTable rows={ITEMS} columns={COLUMNS} rowKey={() => "same"} />)).toThrow(/row key "same"/);
    } finally {
      errors.mockRestore();
    }
  });
});
