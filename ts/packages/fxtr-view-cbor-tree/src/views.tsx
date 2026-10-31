// The renderers: a panel showing an entity's type name and its data as a tree, an inline link
// naming the type and a shortened cid, and a hover preview with the type, cid, and the first few
// fields. All work for any type, so a shell registers them as the built-in entity slots' wildcard renderers
// (see `cborTreeRenderers` in index.ts).

import {
  CidText,
  EntityLink,
  TYPE_KEY,
  isDataMap,
  isEntityID,
  shortCid,
  subAtom,
  typeNameOf,
  unwrap,
  useEntity,
  validatedAtom,
  type DataValue,
  type HoverPreviewProps,
  type LinkProps,
  type EntityPanelProps,
} from "fxtr-view";
import { useMemo } from "react";
import { shortName } from "./names";
import { DataTree, type CollapsedPaths } from "./tree";
import styles from "./cbor_tree.module.css";

interface TreePanelState {
  readonly kind: "fxtr-view-cbor-tree/panel/v1";
  readonly collapsed: CollapsedPaths;
}
const INITIAL_STATE: TreePanelState = { kind: "fxtr-view-cbor-tree/panel/v1", collapsed: {} };
const isTreePanelState = (v: unknown): v is TreePanelState =>
  typeof v === "object" && v !== null && (v as { kind?: unknown }).kind === INITIAL_STATE.kind;

/** Any entity as a panel: its type name, cid, and decoded data as a collapsible tree. */
export function CborTreePanel({ id, storage }: EntityPanelProps) {
  const collapsed = useMemo(() => subAtom(validatedAtom(storage, INITIAL_STATE, isTreePanelState), "collapsed"), [storage]);
  const data = unwrap(useEntity(id));
  return (
    <div className={styles.panel}>
      <div className={styles.header}>
        <span className={styles.type}>{typeNameOf(data)}</span> <CidText cid={id.cid} />
      </div>
      <DataTree value={data} collapsed={collapsed} linkPaths />
    </div>
  );
}

/** Any entity as an inline link: its short type name and shortened cid. */
export function CborTreeInlineLink({ id }: LinkProps) {
  const result = useEntity(id);
  const label = result.status === "loaded" ? shortName(typeNameOf(result.value)) : "entity";
  return (
    <span className={styles.link} title={id.cid}>
      <span className={styles.linkType}>{label}</span> <span className={styles.linkCid}>{shortCid(id.cid)}</span>
    </span>
  );
}

/** How many top-level fields the hover preview shows. */
export const PREVIEW_FIELD_LIMIT = 6;
const PREVIEW_STRING_LIMIT = 80;

/** One value, compactly: scalars (strings truncated), ids as links, containers as their size. */
export function CompactValue({ value }: { value: DataValue }) {
  if (isEntityID(value)) return <EntityLink id={value} />;
  if (value === null) return <span className={styles.null}>null</span>;
  switch (typeof value) {
    case "boolean":
    case "number":
    case "bigint":
      return <span className={styles.num}>{String(value)}</span>;
    case "string":
      return (
        <span className={styles.str}>
          {JSON.stringify(value.length > PREVIEW_STRING_LIMIT ? `${value.slice(0, PREVIEW_STRING_LIMIT)}…` : value)}
        </span>
      );
  }
  if (value instanceof Uint8Array) return <span className={styles.bytes}>bytes[{value.length}]</span>;
  if (Array.isArray(value)) return <span className={styles.summary}>[{value.length}]</span>;
  if (isDataMap(value)) return <span className={styles.summary}>{`{${Object.keys(value).length}}`}</span>;
  return <span>{String(value)}</span>;
}

/** Any entity as a hover preview: its type name, cid, and its first few fields (the `$type` tag
 *  excluded), each value compact. */
export function CborTreeHoverPreview({ id }: HoverPreviewProps) {
  const data = unwrap(useEntity(id));
  const fields = Object.entries(data).filter(([k]) => k !== TYPE_KEY);
  const shown = fields.slice(0, PREVIEW_FIELD_LIMIT);
  return (
    <div className={styles.preview}>
      <div className={styles.header}>
        <span className={styles.type}>{typeNameOf(data)}</span> <CidText cid={id.cid} />
      </div>
      <dl className={styles.previewFields}>
        {shown.map(([k, v]) => (
          <div key={k} className={styles.previewField}>
            <dt className={styles.key}>{k}</dt>
            <dd>
              <CompactValue value={v} />
            </dd>
          </div>
        ))}
      </dl>
      {fields.length > shown.length && (
        <div className={styles.previewMore}>and {fields.length - shown.length} more</div>
      )}
    </div>
  );
}
