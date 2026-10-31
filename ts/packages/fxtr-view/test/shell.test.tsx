import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  EntityID,
  EntityLink,
  ViewShellProvider,
  useEntityLinks,
  useInvocation,
  type EntityLinks,
  type InvocationRef,
  type ViewShell,
} from "../src/index";

/** A shell whose `EntityLinkImpl` names what it was asked for. */
function testShell(): ViewShell {
  return {
    useEntity: () => ({ status: "error", error: new Error("no entities") }),
    EntityLinkImpl: ({ id }) => <i>{`link to ${id.cid}`}</i>,
  };
}

describe("EntityLink", () => {
  it("renders the shell's EntityLinkImpl", () => {
    render(
      <ViewShellProvider shell={testShell()}>
        <EntityLink id={new EntityID("note1")} />
      </ViewShellProvider>,
    );
    expect(screen.getByText("link to note1")).toBeTruthy();
  });
  it("throws without a provider", () => {
    const quiet = console.error;
    console.error = () => {};
    try {
      expect(() => render(<EntityLink id={new EntityID("note1")} />)).toThrow(/EntityLinkImpl used outside a ViewShellProvider/);
    } finally {
      console.error = quiet;
    }
  });
});

describe("useInvocation", () => {
  const ref: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/rollouts" };
  function State() {
    const result = useInvocation(ref);
    return <span>{result.status === "error" ? String((result.error as Error).message) : result.status}</span>;
  }
  it("yields an error result under a shell that serves no invocations", () => {
    render(
      <ViewShellProvider shell={testShell()}>
        <State />
      </ViewShellProvider>,
    );
    expect(screen.getByText(/serves no invocations/)).toBeTruthy();
  });
  it("goes through the shell's useInvocation when it has one", () => {
    const shell: ViewShell = { ...testShell(), useInvocation: () => ({ status: "loading", promise: new Promise(() => {}) }) };
    render(
      <ViewShellProvider shell={shell}>
        <State />
      </ViewShellProvider>,
    );
    expect(screen.getByText("loading")).toBeTruthy();
  });
});

describe("useEntityLinks", () => {
  const id = new EntityID("note1");
  function Mark() {
    const links = useEntityLinks();
    return (
      <button onClick={() => links.open(id)} aria-pressed={links.isOpen(id)}>
        mark
      </button>
    );
  }
  it("opens nothing under a shell that does not follow links", () => {
    render(
      <ViewShellProvider shell={testShell()}>
        <Mark />
      </ViewShellProvider>,
    );
    const mark = screen.getByText("mark");
    expect(mark.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(mark);
  });
  it("goes through the shell's useEntityLinks when it has one", () => {
    const opened: string[] = [];
    const links: EntityLinks = { open: (target) => opened.push(target.cid), isOpen: (target) => target.cid === "note1" };
    render(
      <ViewShellProvider shell={{ ...testShell(), useEntityLinks: () => links }}>
        <Mark />
      </ViewShellProvider>,
    );
    const mark = screen.getByText("mark");
    expect(mark.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(mark);
    expect(opened).toEqual(["note1"]);
  });
});
