// `DataTable`: a table of rows a renderer has in memory, with columns the reader can sort, filter,
// and resize, and a header that stays in view while the rows scroll under it.
//
// Columns are laid out by the browser (`table-layout: fixed`, the table's full width shared among
// the columns without a `width`) until the reader resizes one. Then every column's width is frozen
// at what it was, so dragging one edge moves no other column, and an empty column at the right end
// takes whatever room the columns leave. Double-clicking an edge puts that column back at its
// frozen width.
//
// Sorting and filtering are in the component's own state and run over every row on each render.
// A column sorts and filters by its `value`: numbers by size, text in natural order (so "#9" before
// "#10"), and empty values last whichever the direction; a filter keeps the rows whose value, as
// text, contains it, ignoring case. The filters sit in a second header row, shown while one is
// being edited or set.

import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type HTMLAttributes,
  type PointerEvent,
  type ReactElement,
  type ReactNode,
  type RefObject,
} from "react";
import styles from "./table.module.css";

export interface TableColumn<Row> {
  /** Unique among the table's columns. */
  readonly key: string;
  readonly label: string;
  /** What the column sorts by and, as text, what its filter matches; a column without one neither
   *  sorts nor filters. */
  readonly value?: (row: Row) => string | number | null;
  /** What a cell shows; the value when left out. */
  readonly cell?: (row: Row) => ReactNode;
  /** Right-aligned, in tabular figures; not filtered unless `filterable` says so. */
  readonly numeric?: boolean;
  /** Whether the column has a filter: by default, when it has a value and is not numeric. */
  readonly filterable?: boolean;
  /** The column's starting width in pixels; the columns without one share the rest. */
  readonly width?: number;
}

export interface TableSort {
  readonly key: string;
  readonly direction: "asc" | "desc";
}

/** What a row element can be given by `renderRow`: a click, and a view link's hover and drag. */
export type TableRowProps = Omit<HTMLAttributes<HTMLTableRowElement>, "children" | "className">;

export interface DataTableProps<Row> {
  readonly rows: readonly Row[];
  readonly columns: readonly TableColumn<Row>[];
  /** Unique among the rows. */
  readonly rowKey: (row: Row) => string;
  /** The order the rows start in; the order given when left out. */
  readonly initialSort?: TableSort;
  /** The height past which the rows scroll under the header, in pixels. */
  readonly maxHeight?: number;
  /** Draws a row through `tr`, with the props to spread on the row element and whether it is
   *  selected (tinted, with a bar at its left edge). A row with a click shows as clickable. For a
   *  row whose props come from a hook (a view link's), `renderRow` returns the component calling
   *  it. Rows are plain when left out. */
  readonly renderRow?: (row: Row, tr: (props: TableRowProps, selected?: boolean) => ReactElement) => ReactElement;
  /** What the table says when it has no rows at all. */
  readonly emptyText?: string;
}

const DEFAULT_MAX_HEIGHT = 420;
const MIN_WIDTH = 40;

export function DataTable<Row>({
  rows,
  columns,
  rowKey,
  initialSort,
  maxHeight = DEFAULT_MAX_HEIGHT,
  renderRow,
  emptyText = "no rows",
}: DataTableProps<Row>) {
  assertUnique(columns.map((column) => column.key), "column key");
  const [sort, setSort] = useState<TableSort | null>(initialSort ?? null);
  const [filters, setFilters] = useState<Readonly<Record<string, string>>>({});
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [focusFilter, setFocusFilter] = useState<string | null>(null);
  const headRow = useRef<HTMLTableRowElement>(null);
  const filterInputs = useRef(new Map<string, HTMLInputElement>());
  const widths = useFrozenWidths(columns, headRow);

  const shown = useMemo(() => sortRows(filterRows(rows, columns, filters), columns, sort), [rows, columns, filters, sort]);
  assertUnique(shown.map(rowKey), "row key");

  useEffect(() => {
    if (focusFilter === null) return;
    filterInputs.current.get(focusFilter)?.focus();
    setFocusFilter(null);
  }, [focusFilter]);

  const filtering = Object.values(filters).some((text) => text.trim() !== "");
  const showFilters = filtersOpen || filtering;
  const sized = columns.every((column) => widths.of(column) !== undefined);

  const toggleSort = (key: string) =>
    setSort((current) =>
      current?.key !== key ? { key, direction: "asc" } : current.direction === "asc" ? { key, direction: "desc" } : null,
    );
  const openFilter = (key: string) => {
    if (showFilters && !filtering) {
      setFiltersOpen(false);
      return;
    }
    setFiltersOpen(true);
    setFocusFilter(key);
  };
  const setFilter = (key: string, text: string) => setFilters((current) => ({ ...current, [key]: text }));
  const clearFilter = (key: string) => {
    const next = { ...filters, [key]: "" };
    setFilters(next);
    if (Object.values(next).every((text) => text.trim() === "")) setFiltersOpen(false);
  };

  const cells = (row: Row) => (
    <>
      {columns.map((column) => {
        const value = column.value?.(row) ?? null;
        return (
          <td
            key={column.key}
            data-numeric={column.numeric || undefined}
            title={column.cell === undefined && typeof value === "string" ? value : undefined}
          >
            {column.cell ? column.cell(row) : value}
          </td>
        );
      })}
      {sized && <td className={styles.filler} />}
    </>
  );
  const tr = (row: Row) => (props: TableRowProps, selected = false) => (
    <tr {...props} data-selected={selected || undefined} data-clickable={props.onClick ? true : undefined}>
      {cells(row)}
    </tr>
  );

  return (
    <div className={styles.wrap}>
      <div className={styles.scroll} style={{ maxHeight }}>
        <table className={styles.table}>
          <colgroup>
            {columns.map((column) => {
              const width = widths.of(column);
              return <col key={column.key} style={width === undefined ? undefined : { width }} />;
            })}
            {sized && <col />}
          </colgroup>
          <thead>
            <tr ref={headRow}>
              {columns.map((column) => (
                <HeaderCell
                  key={column.key}
                  column={column}
                  sort={sort?.key === column.key ? sort.direction : null}
                  filter={filters[column.key] ?? ""}
                  onSort={() => toggleSort(column.key)}
                  onFilter={() => openFilter(column.key)}
                  onResizeStart={() => widths.freeze()[column.key]!}
                  onResize={(width) => widths.set(column.key, width)}
                  onResizeReset={() => widths.reset(column.key)}
                />
              ))}
              {sized && <th className={styles.filler} />}
            </tr>
            {showFilters && (
              <tr className={styles.filterRow}>
                {columns.map((column) => (
                  <th key={column.key}>
                    {isFilterable(column) && (
                      <input
                        ref={(input) => {
                          if (input) filterInputs.current.set(column.key, input);
                          else filterInputs.current.delete(column.key);
                        }}
                        className={styles.filterInput}
                        value={filters[column.key] ?? ""}
                        placeholder={`filter ${column.label}`}
                        aria-label={`${column.label} filter`}
                        onChange={(e) => setFilter(column.key, e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") clearFilter(column.key);
                        }}
                      />
                    )}
                  </th>
                ))}
                {sized && <th className={styles.filler} />}
              </tr>
            )}
          </thead>
          <tbody>
            {shown.map((row) => (
              <Fragment key={rowKey(row)}>{renderRow ? renderRow(row, tr(row)) : tr(row)({})}</Fragment>
            ))}
            {shown.length === 0 && (
              <tr>
                <td className={styles.empty} colSpan={columns.length + (sized ? 1 : 0)}>
                  {rows.length === 0 ? emptyText : "no rows match the filters"}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {filtering && (
        <p className={styles.count}>
          {shown.length} of {rows.length} rows
        </p>
      )}
    </div>
  );
}

function HeaderCell<Row>({
  column,
  sort,
  filter,
  onSort,
  onFilter,
  onResizeStart,
  onResize,
  onResizeReset,
}: {
  column: TableColumn<Row>;
  sort: TableSort["direction"] | null;
  filter: string;
  onSort: () => void;
  onFilter: () => void;
  /** Returns the column's width as the drag starts. */
  onResizeStart: () => number;
  onResize: (width: number) => void;
  onResizeReset: () => void;
}) {
  const drag = useRef<{ x: number; width: number } | null>(null);
  const sortable = column.value !== undefined;
  const label = <span className={styles.label}>{column.label}</span>;
  // The pointer is captured, so the drag keeps tracking once it leaves the edge.
  const onPointerDown = (e: PointerEvent<HTMLSpanElement>) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { x: e.clientX, width: onResizeStart() };
  };
  const onPointerMove = (e: PointerEvent<HTMLSpanElement>) => {
    if (drag.current) onResize(drag.current.width + e.clientX - drag.current.x);
  };
  const endDrag = () => {
    drag.current = null;
  };
  return (
    <th data-key={column.key} data-numeric={column.numeric || undefined} aria-sort={sort === null ? undefined : sort === "asc" ? "ascending" : "descending"}>
      <div className={styles.head}>
        {sortable ? (
          <button type="button" className={styles.sort} onClick={onSort} title={`Sort by ${column.label}`}>
            {label}
            <SortIcon direction={sort} />
          </button>
        ) : (
          <span className={styles.sort}>{label}</span>
        )}
        {isFilterable(column) && (
          <button
            type="button"
            className={styles.filterButton}
            data-on={filter.trim() !== "" || undefined}
            aria-label={`Filter ${column.label}`}
            title={`Filter ${column.label}`}
            onClick={onFilter}
          >
            <FilterIcon />
          </button>
        )}
        <span
          className={styles.grip}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onDoubleClick={onResizeReset}
        />
      </div>
    </th>
  );
}

/** The columns' widths: `width` where a column has one, else the browser's, until `freeze`
 *  measures every column's header and holds those widths, for `set` to change. */
function useFrozenWidths<Row>(columns: readonly TableColumn<Row>[], headRow: RefObject<HTMLTableRowElement>) {
  const [frozen, setFrozen] = useState<Readonly<Record<string, number>> | null>(null);
  const initial = useRef<Readonly<Record<string, number>>>({});
  return {
    of: (column: TableColumn<Row>): number | undefined => frozen?.[column.key] ?? column.width,
    /** Every column's width, measuring them first if they are not frozen yet. */
    freeze: (): Readonly<Record<string, number>> => {
      if (frozen !== null && columns.every((column) => frozen[column.key] !== undefined)) return frozen;
      const row = headRow.current;
      if (!row) throw new Error("DataTable: no header row to measure");
      const measured: Record<string, number> = { ...frozen };
      for (const th of Array.from(row.cells)) {
        const key = th.dataset["key"];
        if (key !== undefined && measured[key] === undefined) measured[key] = th.getBoundingClientRect().width;
      }
      initial.current = { ...measured, ...initial.current };
      setFrozen(measured);
      return measured;
    },
    set: (key: string, width: number) => setFrozen((current) => ({ ...current, [key]: Math.max(MIN_WIDTH, Math.round(width)) })),
    reset: (key: string) => {
      const width = initial.current[key];
      if (width !== undefined) setFrozen((current) => ({ ...current, [key]: width }));
    },
  };
}

function isFilterable<Row>(column: TableColumn<Row>): boolean {
  return column.value !== undefined && (column.filterable ?? !column.numeric);
}

function filterRows<Row>(
  rows: readonly Row[],
  columns: readonly TableColumn<Row>[],
  filters: Readonly<Record<string, string>>,
): readonly Row[] {
  const active = columns.flatMap((column) => {
    const text = filters[column.key]?.trim().toLowerCase() ?? "";
    return text !== "" && column.value ? [{ value: column.value, text }] : [];
  });
  if (active.length === 0) return rows;
  return rows.filter((row) => active.every(({ value, text }) => String(value(row) ?? "").toLowerCase().includes(text)));
}

function sortRows<Row>(rows: readonly Row[], columns: readonly TableColumn<Row>[], sort: TableSort | null): readonly Row[] {
  if (sort === null) return rows;
  // The reader's sort outlives the columns: one made per data value can go when the rows change.
  // The rows show unsorted until a column with that key is back.
  const value = columns.find((column) => column.key === sort.key)?.value;
  if (!value) return rows;
  const sign = sort.direction === "asc" ? 1 : -1;
  return rows
    .map((row) => ({ row, value: value(row) }))
    .sort((a, b) => {
      if (a.value === null || b.value === null) return a.value === b.value ? 0 : a.value === null ? 1 : -1;
      return sign * compareValues(a.value, b.value);
    })
    .map(({ row }) => row);
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function compareValues(a: string | number, b: string | number): number {
  return typeof a === "number" && typeof b === "number" ? a - b : collator.compare(String(a), String(b));
}

function assertUnique(keys: readonly string[], what: string) {
  const seen = new Set<string>();
  for (const key of keys) {
    if (seen.has(key)) throw new Error(`DataTable: ${what} "${key}" is repeated`);
    seen.add(key);
  }
}

function SortIcon({ direction }: { direction: TableSort["direction"] | null }) {
  return (
    <svg className={styles.sortIcon} data-on={direction !== null || undefined} width={10} height={10} viewBox="0 0 10 10" aria-hidden>
      {direction !== "desc" && <path d="M2 4.5 5 1.5 8 4.5" />}
      {direction !== "asc" && <path d="M2 5.5 5 8.5 8 5.5" />}
    </svg>
  );
}

function FilterIcon() {
  return (
    <svg width={10} height={10} viewBox="0 0 10 10" aria-hidden>
      <path d="M1 1.5h8L6 5.2V9L4 8V5.2z" />
    </svg>
  );
}
