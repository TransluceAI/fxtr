// Tiny collapsible JSON viewer. Each container node (object or array)
// renders a clickable header showing kind + size; primitives render
// inline. Top-level collapse state defaults to ``collapsed`` so a
// large state tree doesn't unfurl on first paint.

import { useState } from "react";

export function JsonTree(props: {
  value: unknown;
  defaultOpen?: boolean;
}): JSX.Element {
  return <Node value={props.value} defaultOpen={props.defaultOpen ?? false} />;
}

function Node(props: { value: unknown; defaultOpen: boolean }): JSX.Element {
  const v = props.value;
  if (v === null) return <span style={{ color: "#888" }}>null</span>;
  if (typeof v === "boolean")
    return <span style={{ color: "#0a7" }}>{String(v)}</span>;
  if (typeof v === "number")
    return <span style={{ color: "#06c" }}>{String(v)}</span>;
  if (typeof v === "string")
    return <span style={{ color: "#a40" }}>{JSON.stringify(v)}</span>;
  if (Array.isArray(v))
    return <Container kind="array" entries={v.map((x, i) => [String(i), x])} defaultOpen={props.defaultOpen} />;
  if (typeof v === "object")
    return (
      <Container
        kind="object"
        entries={Object.entries(v as Record<string, unknown>)}
        defaultOpen={props.defaultOpen}
      />
    );
  return <span>{String(v)}</span>;
}

function Container(props: {
  kind: "array" | "object";
  entries: Array<[string, unknown]>;
  defaultOpen: boolean;
}): JSX.Element {
  const [open, setOpen] = useState(props.defaultOpen);
  const [openL, openR] = props.kind === "array" ? ["[", "]"] : ["{", "}"];
  if (props.entries.length === 0) {
    return (
      <span style={{ color: "#888" }}>
        {openL}
        {openR}
      </span>
    );
  }
  return (
    <span>
      <span
        style={{ cursor: "pointer", userSelect: "none" }}
        onClick={() => setOpen(!open)}
      >
        {open ? "▾" : "▸"} {openL}
        {!open && (
          <span style={{ color: "#888" }}>
            {" "}
            {props.entries.length} {props.kind === "array" ? "items" : "keys"}{" "}
          </span>
        )}
        {!open && openR}
      </span>
      {open && (
        <ul
          style={{
            listStyle: "none",
            margin: 0,
            paddingLeft: 16,
            borderLeft: "1px dotted #ccc",
          }}
        >
          {props.entries.map(([k, child]) => (
            <li key={k}>
              <span style={{ color: "#666" }}>
                {props.kind === "object" ? JSON.stringify(k) : k}:
              </span>{" "}
              <Node value={child} defaultOpen={false} />
            </li>
          ))}
          <li style={{ color: "#888" }}>{openR}</li>
        </ul>
      )}
    </span>
  );
}
