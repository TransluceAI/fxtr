import { fireEvent, render, screen } from "@testing-library/react";
import { atom } from "jotai";
import { describe, expect, it } from "vitest";
import {
  EntityID,
  InvocationPanel,
  invocationRefKey,
  renderer,
  type InvocationRecord,
  type InvocationRef,
  type JsonValue,
  type Spawn,
} from "fxtr-view";
import { RenderInvocation, dispatchRegistry } from "fxtr-view-shell-util";
import { TableShellProvider } from "fxtr-view-shell-util/testing";
import { eachInvocation, singleInvocation } from "../src/index";

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
const storage = atom(null as JsonValue);

/** A view of one record, wrapped for the panel slot. */
const ChatPanel = singleInvocation(({ invocation }: { invocation: InvocationRecord }) => (
  <div>chat for {invocation.name} with {Object.keys(invocation.inputs).join(",")}</div>
));
const invocationRegistry = dispatchRegistry([renderer(InvocationPanel, "rollout", "chat", ChatPanel)]);

describe("singleInvocation", () => {
  const shell = { registry: invocationRegistry, invocations };
  const card = (spawns: readonly Spawn[]) => (
    <TableShellProvider {...shell}>
      <RenderInvocation slot={InvocationPanel} name="rollout" renderer="chat" invocations={spawns} storage={storage} />
    </TableShellProvider>
  );

  it("loads the one spawn's record and renders the view with it", () => {
    render(card([small]));
    expect(screen.getByText("chat for rollout with model,scenario")).toBeTruthy();
  });
  it("asks for one to be selected for zero or several spawns", () => {
    const { container, rerender } = render(card([]));
    expect(container.textContent).toBe("No invocation is selected: select one to use this view.");
    rerender(card([small, large]));
    expect(container.textContent).toBe("2 invocations are selected: select one to use this view.");
  });
  it("throws a failed load into the slot's boundary", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      render(card([large]));
      expect(screen.getByRole("alert").textContent).toMatch(/unknown invocation/);
    } finally {
      console.error = quiet;
    }
  });
  it("passes the other props through", () => {
    const Panel = singleInvocation(({ invocation, storage }: { invocation: InvocationRecord; storage: object }) => (
      <div>{invocation.name} with {typeof storage}</div>
    ));
    render(
      <TableShellProvider {...shell}>
        <Panel invocations={[small]} storage={storage} />
      </TableShellProvider>,
    );
    expect(screen.getByText("rollout with object")).toBeTruthy();
  });
});

describe("eachInvocation", () => {
  const shell = { registry: invocationRegistry, invocations };
  const Panel = eachInvocation(({ invocation }: { invocation: InvocationRecord }) => <div>view of {invocation.name}</div>, {
    Summary: ({ invocation }) => <span>summary {Object.keys(invocation.inputs).length}</span>,
  });
  const panel = (spawns: readonly Spawn[]) => (
    <TableShellProvider {...shell}>
      <Panel invocations={spawns} />
    </TableShellProvider>
  );

  it("renders the view alone for one spawn, and nothing for none", () => {
    const { container, rerender } = render(panel([small]));
    expect(screen.getByText("view of rollout")).toBeTruthy();
    rerender(panel([]));
    expect(container.textContent).toBe("");
  });
  it("gives several spawns a closed section each, headed by key and summary, opened on demand", () => {
    const otherRef: InvocationRef = { jobId: "j", kind: "step", cacheKey: "other" };
    const other: Spawn = { key: { model: "other" }, ref: otherRef, output: null };
    const both = new Map([...invocations, [invocationRefKey(otherRef), rolloutRecord]]);
    const { container } = render(
      <TableShellProvider registry={invocationRegistry} invocations={both}>
        <Panel invocations={[small, other]} />
      </TableShellProvider>,
    );
    const sections = container.querySelectorAll("details");
    expect(sections).toHaveLength(2);
    expect(sections[1]!.querySelector("summary")!.textContent).toMatch(/^other/);
    expect(screen.getAllByText("summary 2")).toHaveLength(2);
    expect(screen.queryByText("view of rollout")).toBeNull();
    sections[0]!.open = true;
    fireEvent(sections[0]!, new Event("toggle"));
    expect(screen.getByText("view of rollout")).toBeTruthy();
  });
});
