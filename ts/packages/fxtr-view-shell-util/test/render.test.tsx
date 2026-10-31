import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  ANY_SUBJECT_TYPE,
  EntityID,
  declareView,
  EntityPanel,
  EntityHoverPreview,
  EntityInlineLink,
  InvocationPanel,
  invocationRefKey,
  renderer,
  unwrap,
  useEntity,
  useInvocation,
  type EntityData,
  type EntityPanelProps,
  type EntityViewProps,
  type InvocationPanelProps,
  type InvocationRecord,
  type InvocationRef,
  type JsonValue,
  type Spawn,
} from "fxtr-view";
import { RenderEntity, RenderInvocation, dispatchRegistry, useRenderers } from "../src/index";
import { RenderSlot } from "../src/render";
import { TableShellProvider } from "../testing";
import { PendingShellProvider } from "./pending_shell";
import { atom } from "jotai";

interface NoteData extends EntityData {
  readonly $type: "org.example.Note";
  readonly body: string;
}

const NOTE: NoteData = { $type: "org.example.Note", body: "hello" };
const entities = new Map<string, EntityData>([["note1", NOTE], ["odd1", { $type: "org.example.Odd" }]]);

function NotePanel({ id }: EntityPanelProps) {
  const result = useEntity(id as EntityID<NoteData>);
  return <div>panel:{result.status === "loaded" ? result.value.body : result.status}</div>;
}
function NoteRawPanel() {
  return <div>raw panel</div>;
}
function NoteInline({ id }: EntityViewProps) {
  return <span>note {id.cid}</span>;
}
function Throwing(): never {
  throw new Error("renderer exploded");
}
function AnyPreview({ id }: EntityViewProps) {
  return <i>any preview ({id.cid})</i>;
}

const registry = dispatchRegistry([
  renderer(EntityPanel, "org.example.Note", "default", NotePanel),
  renderer(EntityPanel, "org.example.Note", "raw", NoteRawPanel),
  renderer(EntityPanel, "org.example.Odd", "default", Throwing),
  renderer(EntityInlineLink, "org.example.Note", "default", NoteInline),
  renderer(EntityHoverPreview, "org.example.Note", "default", ({ id }) => <b>preview {id.cid}</b>),
  renderer(EntityHoverPreview, ANY_SUBJECT_TYPE, "any", AnyPreview),
]);

const storage = atom(null as JsonValue);

describe("RenderSlot", () => {
  it("renders the first renderer registered for the subject type with the props", () => {
    render(
      <TableShellProvider registry={registry}>
        <RenderSlot slot={EntityHoverPreview} subjectType="org.example.Note" id={new EntityID("x")} />
      </TableShellProvider>,
    );
    expect(screen.getByText("preview x")).toBeTruthy();
  });
  it("renders the renderer with the given id", () => {
    render(
      <TableShellProvider registry={registry}>
        <RenderSlot slot={EntityPanel} subjectType="org.example.Note" renderer="raw" id={new EntityID("note1")} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("raw panel")).toBeTruthy();
  });
  it("renders a wildcard renderer when asked for by id, even with a specific one registered", () => {
    render(
      <TableShellProvider registry={registry}>
        <RenderSlot slot={EntityHoverPreview} subjectType="org.example.Note" renderer="any" id={new EntityID("f")} />
      </TableShellProvider>,
    );
    expect(screen.getByText("any preview (f)")).toBeTruthy();
  });
  it("renders the slot's wildcard for a subject type with no renderer of its own", () => {
    render(
      <TableShellProvider registry={registry}>
        <RenderSlot slot={EntityHoverPreview} subjectType="org.example.Other" id={new EntityID("y")} />
      </TableShellProvider>,
    );
    expect(screen.getByText("any preview (y)")).toBeTruthy();
  });
  it("renders a placeholder for a subject type with no renderer at all", () => {
    render(
      <TableShellProvider registry={registry}>
        <RenderSlot slot={EntityInlineLink} subjectType="t" id={new EntityID("x")} />
      </TableShellProvider>,
    );
    expect(screen.getByText("no view for t")).toBeTruthy();
  });
  it("shows Error, with the props, for a renderer id nothing has", () => {
    render(
      <TableShellProvider registry={registry}>
        <RenderSlot slot={EntityHoverPreview} subjectType="org.example.Note" renderer="gone" id={new EntityID("z")} />
      </TableShellProvider>,
    );
    expect(screen.getByRole("alert").textContent).toMatch(/no renderer "gone" for org.example.Note in EntityHoverPreview/);
  });
  it("remounts with a new registry, so a renderer that threw shows again", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      const { rerender } = render(
        <TableShellProvider registry={dispatchRegistry([renderer(EntityHoverPreview, "t", "v", Throwing)])}>
          <RenderSlot slot={EntityHoverPreview} subjectType="t" id={new EntityID("x")} />
        </TableShellProvider>,
      );
      expect(screen.getByRole("alert").textContent).toBe("renderer exploded");
      const fixed = dispatchRegistry([renderer(EntityHoverPreview, "t", "v", ({ id }) => <b>fixed {id.cid}</b>)]);
      rerender(
        <TableShellProvider registry={fixed}>
          <RenderSlot slot={EntityHoverPreview} subjectType="t" id={new EntityID("x")} />
        </TableShellProvider>,
      );
      expect(screen.getByText("fixed x")).toBeTruthy();
    } finally {
      console.error = quiet;
    }
  });
  it("throws without a provider", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      expect(() => render(<RenderSlot slot={EntityHoverPreview} subjectType="t" id={new EntityID("z")} />)).toThrow(/dispatch used outside a RegistryProvider/);
    } finally {
      console.error = quiet;
    }
  });
});

describe("RenderEntity", () => {
  it("shows Loading, then the dispatched component with the id", () => {
    const { rerender } = render(
      <PendingShellProvider registry={registry}>
        <RenderEntity slot={EntityPanel} id={new EntityID("note1")} storage={storage} />
      </PendingShellProvider>,
    );
    expect(screen.getByText("…")).toBeTruthy();
    rerender(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("note1")} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("panel:hello")).toBeTruthy();
  });
  it("shows the host's loading fallback while the entity loads", () => {
    render(
      <PendingShellProvider registry={registry}>
        <RenderEntity slot={EntityPanel} id={new EntityID("note1")} storage={storage} fallbacks={{ loading: <i>panel loading</i> }} />
      </PendingShellProvider>,
    );
    expect(screen.getByText("panel loading")).toBeTruthy();
  });
  it("shows the host's error fallback for a failed load", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity
          slot={EntityPanel}
          id={new EntityID("missing")}
          storage={storage}
          fallbacks={{ error: (e) => <b>failed: {(e as Error).message}</b> }}
        />
      </TableShellProvider>,
    );
    expect(screen.getByText("failed: unknown missing")).toBeTruthy();
  });
  it("threads a renderer id through", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} renderer="raw" id={new EntityID("note1")} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("raw panel")).toBeTruthy();
  });
  it("shows Error for a failed load", () => {
    render(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityPanel} id={new EntityID("missing")} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByRole("alert").textContent).toBe("unknown missing");
  });
  it("catches a throwing renderer in Error", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      render(
        <TableShellProvider registry={registry} entities={entities}>
          <RenderEntity slot={EntityPanel} id={new EntityID("odd1")} storage={storage} />
        </TableShellProvider>,
      );
      expect(screen.getByRole("alert").textContent).toBe("renderer exploded");
    } finally {
      console.error = quiet;
    }
  });
  it("shows Loading, then the inline link dispatched on the loaded type", () => {
    const { rerender } = render(
      <PendingShellProvider registry={registry}>
        <RenderEntity slot={EntityInlineLink} id={new EntityID("note1")} />
      </PendingShellProvider>,
    );
    expect(screen.getByText("…")).toBeTruthy();
    rerender(
      <TableShellProvider registry={registry} entities={entities}>
        <RenderEntity slot={EntityInlineLink} id={new EntityID("note1")} />
      </TableShellProvider>,
    );
    expect(screen.getByText("note note1")).toBeTruthy();
  });
  it("shows Error for data without a type tag", () => {
    const bad = new Map<string, EntityData>([["bad1", { body: "x" } as unknown as EntityData]]);
    render(
      <TableShellProvider registry={registry} entities={bad}>
        <RenderEntity slot={EntityInlineLink} id={new EntityID("bad1")} />
      </TableShellProvider>,
    );
    expect(screen.getByRole("alert").textContent).toMatch(/no \$type tag/);
  });
});

// ── invocations ──

const rolloutRef: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/rollouts[model:\"small\"]" };
const rolloutRecord: InvocationRecord = {
  ref: rolloutRef,
  name: "rollout",
  state: "completed",
  error: null,
  inputs: { model: new EntityID("m1"), scenario: new EntityID("s1") },
  output: new EntityID("out1"),
  children: [],
};
const small: Spawn = { key: { model: "small" }, ref: rolloutRef, output: new EntityID("out1") };
const large: Spawn = { key: { model: "large" }, ref: { jobId: "j", kind: "step", cacheKey: "root/rollouts[model:\"large\"]" }, output: null };
const invocations = new Map([[invocationRefKey(rolloutRef), rolloutRecord]]);

function KeysCard({ invocations }: InvocationPanelProps) {
  return <div>keys {invocations.map((spawn) => spawn.key["model"]).join(",")}</div>;
}
function ChatCard({ invocations }: InvocationPanelProps) {
  return <div>chat {invocations.length}</div>;
}
function RawCard({ invocations }: InvocationPanelProps) {
  return <div>raw {invocations[0]?.output?.cid}</div>;
}
/** Suspends on the record, so Loading shows while it is pending. */
function SuspendingCard({ invocations }: InvocationPanelProps) {
  const record = unwrap(useInvocation((invocations[0] as Spawn).ref));
  return <div>loaded {record.name}</div>;
}
function ExplodingCard(): never {
  throw new Error("card exploded");
}
function StoragePanel({ invocations, storage }: InvocationPanelProps) {
  return <div>panel {invocations.length} {typeof storage}</div>;
}

const invocationRegistry = dispatchRegistry([
  renderer(InvocationPanel, "rollout", "keys", KeysCard),
  renderer(InvocationPanel, "rollout", "chat", ChatCard),
  renderer(InvocationPanel, "rollout", "raw", RawCard),
  renderer(InvocationPanel, "rollout", "slow", SuspendingCard),
  renderer(InvocationPanel, "rollout", "boom", ExplodingCard),
  renderer(InvocationPanel, "rollout", "p", StoragePanel),
]);

describe("useRenderers", () => {
  function List({ subjectType }: { subjectType: string | null }) {
    const renderers = useRenderers(InvocationPanel, subjectType);
    return <span>{renderers.map((r) => r.id).join(",") || "none"}</span>;
  }
  it("lists the registered renderers for a subject type in order", () => {
    render(
      <TableShellProvider registry={invocationRegistry}>
        <List subjectType="rollout" />
      </TableShellProvider>,
    );
    expect(screen.getByText("keys,chat,raw,slow,boom,p")).toBeTruthy();
  });
  it("ends with the slot's wildcards", () => {
    function PreviewList() {
      const choices = useRenderers(EntityHoverPreview, "org.example.Note");
      return <span>{choices.map((c) => c.id).join(",")}</span>;
    }
    render(
      <TableShellProvider registry={registry}>
        <PreviewList />
      </TableShellProvider>,
    );
    expect(screen.getByText("default,any")).toBeTruthy();
  });
  it("is empty for an unknown subject type, a null subject type, and a registry with nothing under the slot", () => {
    render(
      <TableShellProvider registry={invocationRegistry}>
        <List subjectType="grade" />
      </TableShellProvider>,
    );
    expect(screen.getByText("none")).toBeTruthy();
    render(
      <TableShellProvider registry={registry}>
        <List subjectType="rollout" />
        <List subjectType={null} />
      </TableShellProvider>,
    );
    expect(screen.getAllByText("none")).toHaveLength(3);
  });
});

describe("RenderInvocation", () => {
  const shell = { registry: invocationRegistry, invocations };

  it("renders the chosen renderer with the spawns, loading nothing itself", () => {
    render(
      <TableShellProvider registry={invocationRegistry}>
        <RenderInvocation slot={InvocationPanel} name="rollout" renderer="keys" invocations={[small, large]} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("keys small,large")).toBeTruthy();
  });
  it("threads the slot's other props through", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderInvocation slot={InvocationPanel} name="rollout" renderer="p" invocations={[small]} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("panel 1 object")).toBeTruthy();
  });
  it("shows Loading while the renderer suspends, then the renderer", () => {
    const { rerender } = render(
      <PendingShellProvider registry={invocationRegistry}>
        <RenderInvocation slot={InvocationPanel} name="rollout" renderer="slow" invocations={[small]} storage={storage} />
      </PendingShellProvider>,
    );
    expect(screen.getByText("…")).toBeTruthy();
    rerender(
      <TableShellProvider {...shell}>
        <RenderInvocation slot={InvocationPanel} name="rollout" renderer="slow" invocations={[small]} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("loaded rollout")).toBeTruthy();
  });
  it("shows the host's error fallback for a renderer id nothing has", () => {
    const spawns = [small, large];
    const error = (e: unknown) => <span role="alert">{spawns.length} spawns: {(e as Error).message}</span>;
    render(
      <TableShellProvider {...shell}>
        <RenderInvocation slot={InvocationPanel} name="rollout" renderer="gone" invocations={spawns} fallbacks={{ error }} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByRole("alert").textContent).toMatch(/^2 spawns: .*no renderer "gone" for rollout in InvocationPanel/);
  });
  it("shows the host's error fallback for a throwing renderer", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      render(
        <TableShellProvider {...shell}>
          <RenderInvocation
            slot={InvocationPanel}
            name="rollout"
            renderer="boom"
            invocations={[small]}
            fallbacks={{ error: (e) => <b>card failed: {(e as Error).message}</b> }} storage={storage} />
        </TableShellProvider>,
      );
      expect(screen.getByText("card failed: card exploded")).toBeTruthy();
    } finally {
      console.error = quiet;
    }
  });
  it("shows the host's no-renderer fallback", () => {
    render(
      <TableShellProvider {...shell}>
        <RenderInvocation slot={InvocationPanel} name="grade" invocations={[small]} fallbacks={{ noRenderer: (t) => <b>nothing for {t}</b> }} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("nothing for grade")).toBeTruthy();
  });
  it("catches a throwing renderer, and switching renderer clears it", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      const { rerender } = render(
        <TableShellProvider {...shell}>
          <RenderInvocation slot={InvocationPanel} name="rollout" renderer="boom" invocations={[small]} storage={storage} />
        </TableShellProvider>,
      );
      expect(screen.getByRole("alert").textContent).toMatch(/card exploded/);
      rerender(
        <TableShellProvider {...shell}>
          <RenderInvocation slot={InvocationPanel} name="rollout" renderer="raw" invocations={[small]} storage={storage} />
        </TableShellProvider>,
      );
      expect(screen.getByText("raw out1")).toBeTruthy();
    } finally {
      console.error = quiet;
    }
  });
  it("remounts when the spawns change, so a caught error does not outlive the selection", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      /** Throws for two spawns, renders for one: a caught error must not outlive the selection. */
      function Fussy({ invocations }: InvocationPanelProps) {
        if (invocations.length > 1) throw new Error("one at a time");
        return <div>fussy ok</div>;
      }
      const fussyRegistry = dispatchRegistry([renderer(InvocationPanel, "rollout", "fussy", Fussy)]);
      const card = (spawns: Spawn[]) => (
        <TableShellProvider registry={fussyRegistry}>
          <RenderInvocation slot={InvocationPanel} name="rollout" renderer="fussy" invocations={spawns} storage={storage} />
        </TableShellProvider>
      );
      const { rerender } = render(card([small, large]));
      expect(screen.getByRole("alert").textContent).toMatch(/one at a time/);
      rerender(card([small]));
      expect(screen.getByText("fussy ok")).toBeTruthy();
    } finally {
      console.error = quiet;
    }
  });
  it("rejects an entity slot, and RenderEntity rejects an invocation slot", () => {
    // @ts-expect-error EntityInlineLink is not an invocation slot
    void (<RenderInvocation slot={EntityInlineLink} name="rollout" id={new EntityID("x")} />);
    // @ts-expect-error InvocationPanel is not an entity slot
    void (<RenderEntity slot={InvocationPanel} invocations={[small]} />);
  });
  it("rejects a panel slot without its storage", () => {
    // @ts-expect-error the panel slot's props need storage
    void (<RenderInvocation slot={InvocationPanel} name="rollout" renderer="p" invocations={[small]} />);
  });
});

describe("views declared within another", () => {
  function Detail({ invocations, params }: InvocationPanelProps) {
    return <div>detail {invocations.length} {params?.["point"]}</div>;
  }
  const CHART = declareView(InvocationPanel, "rollout", "chart");
  const POINT = declareView(InvocationPanel, "rollout", "point", { within: CHART, params: ["point"] });
  const registry = dispatchRegistry([renderer(CHART, ChatCard), renderer(POINT, Detail)]);

  it("dispatches the nested view by its full id, with the slot's props", () => {
    expect(POINT.id).toBe("chart/point");
    render(
      <TableShellProvider registry={registry}>
        <RenderInvocation slot={InvocationPanel} name="rollout" renderer={POINT.id} invocations={[small]}
          storage={storage} params={{ point: 3 }} />
      </TableShellProvider>,
    );
    expect(screen.getByText("detail 1 3")).toBeTruthy();
  });
});
