import { act, render, screen } from "@testing-library/react";
import { Provider, createStore } from "jotai";
import { describe, expect, it } from "vitest";
import { EntityID, type EntityData, type InvocationRecord, type InvocationRef } from "fxtr-view";
import { createCachedEntityHook, createCachedInvocationHook } from "../src/index";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("createCachedEntityHook", () => {
  it("shares one load per id key across readers and renders loaded data without a loading tick", async () => {
    const loads: string[] = [];
    const pending = deferred<EntityData>();
    const useEntity = createCachedEntityHook((id) => {
      loads.push(id.cid);
      return pending.promise;
    });
    let pendingPromise: Promise<unknown> | null = null;
    function Show({ id, tag }: { id: EntityID; tag: string }) {
      const r = useEntity(id);
      if (r.status === "loading") pendingPromise = r.promise;
      return <span data-testid={tag}>{r.status === "loaded" ? String(r.value["$type"]) : r.status}</span>;
    }
    const store = createStore();
    const id = new EntityID("c1");
    const { rerender } = render(
      <Provider store={store}>
        <Show id={id} tag="a" />
        <Show id={new EntityID("c1")} tag="b" />
      </Provider>,
    );
    expect(screen.getByTestId("a").textContent).toBe("loading");
    expect(loads).toEqual(["c1"]);
    expect(pendingPromise).toBeInstanceOf(Promise);
    let settled = false;
    void pendingPromise!.then(() => (settled = true));
    await act(async () => {
      pending.resolve({ $type: "t" });
    });
    expect(settled).toBe(true);
    expect(screen.getByTestId("a").textContent).toBe("t");
    expect(screen.getByTestId("b").textContent).toBe("t");
    // A new reader of a loaded id mounts already loaded.
    rerender(
      <Provider store={store}>
        <Show id={id} tag="a" />
        <Show id={new EntityID("c1")} tag="b" />
        <Show id={new EntityID("c1")} tag="c" />
      </Provider>,
    );
    expect(screen.getByTestId("c").textContent).toBe("t");
    expect(loads).toEqual(["c1"]);
  });

  it("loads different ids separately and reports errors", async () => {
    const useEntity = createCachedEntityHook(async (id) => {
      if (id.cid === "bad") throw new Error("nope");
      return { $type: `t:${id.cid}` };
    });
    function Show({ id, tag }: { id: EntityID; tag: string }) {
      const r = useEntity(id);
      return (
        <span data-testid={tag}>
          {r.status === "loaded" ? String(r.value["$type"]) : r.status === "error" ? String((r.error as Error).message) : r.status}
        </span>
      );
    }
    await act(async () => {
      render(
        <>
          <Show id={new EntityID("c1")} tag="a" />
          <Show id={new EntityID("c2")} tag="b" />
          <Show id={new EntityID("bad")} tag="c" />
        </>,
      );
    });
    expect(screen.getByTestId("a").textContent).toBe("t:c1");
    expect(screen.getByTestId("b").textContent).toBe("t:c2");
    expect(screen.getByTestId("c").textContent).toBe("nope");
  });
});

describe("createCachedInvocationHook", () => {
  it("shares one load per ref key and tells refs of the two kinds apart", async () => {
    const loads: string[] = [];
    const useInvocation = createCachedInvocationHook(async (ref) => {
      loads.push(ref.kind);
      return { ref, name: ref.kind, state: "completed", error: null, inputs: {}, output: null, children: [] } satisfies InvocationRecord;
    });
    const workflow: InvocationRef = { jobId: "j", kind: "workflow", pathname: "root/x" };
    const step: InvocationRef = { jobId: "j", kind: "step", cacheKey: "root/x" };
    function Show({ target, tag }: { target: InvocationRef; tag: string }) {
      const r = useInvocation(target);
      return <span data-testid={tag}>{r.status === "loaded" ? r.value.name : r.status}</span>;
    }
    await act(async () => {
      render(
        <>
          <Show target={workflow} tag="a" />
          <Show target={{ ...workflow }} tag="b" />
          <Show target={step} tag="c" />
        </>,
      );
    });
    expect(screen.getByTestId("a").textContent).toBe("workflow");
    expect(screen.getByTestId("b").textContent).toBe("workflow");
    expect(screen.getByTestId("c").textContent).toBe("step");
    expect(loads.sort()).toEqual(["step", "workflow"]);
  });
});
