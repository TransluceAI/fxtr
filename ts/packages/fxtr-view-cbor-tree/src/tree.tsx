// A collapsible tree over any decoded data value. Maps show `key: value` rows, lists show indexed
// rows, scalars render by type, bytes as a hex prefix, and every EntityID leaf is an inline
// EntityLink. The root's rows are listed as they are, with no toggle and no indent (a panel is the
// root's container); every subtree below collapses. Subtrees start open, and the ones collapsed
// are kept in a JSON record keyed by path, in the storage atom the caller hands down (a panel's
// storage, or a slot carved off one).

import { createContext, useContext } from "react";
import { useAtom, type PrimitiveAtom } from "jotai";
import { EntityLink, isDataMap, isEntityID, type DataValue } from "fxtr-view";
import styles from "./cbor_tree.module.css";

/** Collapsed subtrees by path (`"a/0/b"`). Absent means open. */
export type CollapsedPaths = { readonly [path: string]: true };

/** Whether the tree's entity links say where they sit (their path from the tree's root, which is
 *  the value the view was handed); set by the tree's root for its leaves. */
const LinkPathsContext = createContext(false);

export function DataTree({
  value,
  collapsed,
  linkPaths = false,
}: {
  value: DataValue;
  collapsed: PrimitiveAtom<CollapsedPaths>;
  /** Give each entity link its path from `value`, for a tree whose root is the value its view was
   *  handed (`EntityLink`'s `at`). */
  linkPaths?: boolean;
}) {
  return (
    <LinkPathsContext.Provider value={linkPaths}>
      <TreeNode value={value} path="" collapsed={collapsed} />
    </LinkPathsContext.Provider>
  );
}

/** A value that is not a list or a map, rendered by its type: an entity link, null, a number or
 *  bool, a quoted string, or bytes as their length and a hex prefix. */
export function ScalarValue({ value, path }: { value: DataValue; path?: string }) {
  const linkPaths = useContext(LinkPathsContext);
  if (isEntityID(value))
    return <EntityLink id={value} {...(linkPaths && path !== undefined ? { at: { path: path === "" ? [] : path.split("/") } } : {})} />;
  if (value === null) return <span className={styles.null}>null</span>;
  switch (typeof value) {
    case "boolean":
    case "number":
    case "bigint":
      return <span className={styles.num}>{String(value)}</span>;
    case "string":
      return <span className={styles.str}>{JSON.stringify(value)}</span>;
  }
  if (value instanceof Uint8Array) {
    return (
      <span className={styles.bytes}>
        bytes[{value.length}] {hexPrefix(value)}
      </span>
    );
  }
  throw new Error(`ScalarValue: not a scalar: ${Object.prototype.toString.call(value)}`);
}

/** The path of the child `key` under `path`. */
export function childPath(path: string, key: string): string {
  return path === "" ? key : `${path}/${key}`;
}

function TreeNode({ value, path, collapsed }: { value: DataValue; path: string; collapsed: PrimitiveAtom<CollapsedPaths> }) {
  if (!Array.isArray(value) && !isDataMap(value)) return <ScalarValue value={value} path={path} />;
  const entries: readonly (readonly [string, DataValue])[] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v] as const)
    : Object.entries(value);
  const isList = Array.isArray(value);
  return (
    <Container path={path} collapsed={collapsed} isList={isList} summary={isList ? `[${entries.length}]` : `{${entries.length}}`}>
      {entries.map(([k, v]) => (
        <li key={k}>
          <span className={styles.key}>{k}</span>
          <TreeNode value={v} path={childPath(path, k)} collapsed={collapsed} />
        </li>
      ))}
    </Container>
  );
}

/** A map's or list's rows: at the root (path `""`) a plain list with the `root` class
 *  (styled without an indent), below it a `Branch` that collapses. */
export function Container({
  path,
  collapsed,
  isList,
  summary,
  children,
}: {
  path: string;
  collapsed: PrimitiveAtom<CollapsedPaths>;
  isList: boolean;
  summary: string;
  children: React.ReactNode;
}) {
  const kind = isList ? styles.list : styles.map;
  if (path === "") return <ul className={`${kind} ${styles.root}`}>{children}</ul>;
  return (
    <Branch path={path} collapsed={collapsed} summary={summary}>
      <ul className={kind}>{children}</ul>
    </Branch>
  );
}

/** A collapsible container: its toggle (showing `summary`) and, while open, its children. It is
 *  collapsed when `path` has an entry in `collapsed`. */
export function Branch({
  path,
  collapsed,
  summary,
  children,
}: {
  path: string;
  collapsed: PrimitiveAtom<CollapsedPaths>;
  summary: string;
  children: React.ReactNode;
}) {
  const [paths, setPaths] = useAtom(collapsed);
  const isCollapsed = paths[path] === true;
  const toggle = () =>
    setPaths((prev) => {
      if (prev[path] === true) {
        const { [path]: _dropped, ...rest } = prev;
        return rest;
      }
      return { ...prev, [path]: true };
    });
  return (
    <span>
      <button type="button" className={styles.toggle} onClick={toggle} aria-expanded={!isCollapsed}>
        {isCollapsed ? "▸" : "▾"} {summary}
      </button>
      {isCollapsed ? null : children}
    </span>
  );
}

function hexPrefix(bytes: Uint8Array, limit = 16): string {
  const shown = Array.from(bytes.slice(0, limit))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return bytes.length > limit ? `${shown}…` : shown;
}
