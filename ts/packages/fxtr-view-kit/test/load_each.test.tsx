import { act, render, screen } from "@testing-library/react";
import { useSyncExternalStore } from "react";
import { describe, expect, it, vi } from "vitest";
import { EntityID, ViewShellProvider, type EntityData, type ViewShell } from "fxtr-view";
import { LoadEach } from "../src/index";

/** A shell serving the entities put in `store`, loading until then. */
function storeShell() {
  const store = new Map<string, EntityData>();
  const listeners = new Set<() => void>();
  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => listeners.delete(listener);
  };
  const shell: ViewShell = {
    useEntity: (id) => {
      const data = useSyncExternalStore(subscribe, () => store.get(id.cid));
      return data ? { status: "loaded", value: data } : { status: "loading", promise: new Promise(() => {}) };
    },
    EntityLinkImpl: () => null,
  };
  const put = (cid: string, data: EntityData) => {
    store.set(cid, data);
    for (const listener of listeners) listener();
  };
  return { shell, put };
}

const typeOf = (data: EntityData) => data.$type;

describe("LoadEach", () => {
  it("hands over the values in hand and counts the rest, loading a repeated id once", () => {
    const { shell, put } = storeShell();
    put("a", { $type: "A" });
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <ViewShellProvider shell={shell}>
          <LoadEach ids={["a", "b", "a"].map((cid) => new EntityID(cid))} parse={typeOf}>
            {(loaded, pending) => <span>{`${[...loaded.values()].join(",")} / ${pending} pending`}</span>}
          </LoadEach>
        </ViewShellProvider>,
      );
      expect(screen.getByText("A / 1 pending")).toBeTruthy();
      act(() => put("b", { $type: "B" }));
      expect(screen.getByText("A,B / 0 pending")).toBeTruthy();
      expect(errors).not.toHaveBeenCalled();
    } finally {
      errors.mockRestore();
    }
  });
});
