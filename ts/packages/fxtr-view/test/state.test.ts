import { atom, createStore } from "jotai";
import { describe, expect, it } from "vitest";
import { subAtom, validatedAtom, type JsonValue, type StorageAtom } from "../src/index";

interface PanelState {
  readonly kind: "demo/v1";
  readonly open: boolean;
  readonly child: JsonValue;
}
const INITIAL: PanelState = { kind: "demo/v1", open: false, child: null };
const isPanelState = (v: unknown): v is PanelState =>
  typeof v === "object" && v !== null && (v as { kind?: unknown }).kind === "demo/v1";

describe("validatedAtom + subAtom", () => {
  it("reads the initial value over foreign storage and writes through", () => {
    const store = createStore();
    const storage: StorageAtom = atom<JsonValue>({ kind: "other/v3", stale: true });
    const typed = validatedAtom(storage, INITIAL, isPanelState);
    expect(store.get(typed)).toEqual(INITIAL);
    const open = subAtom(typed, "open");
    store.set(open, true);
    expect(store.get(storage)).toEqual({ ...INITIAL, open: true });
    store.set(open, (prev) => !prev);
    expect(store.get(open)).toBe(false);
  });
  it("rejects non-JSON state types", () => {
    const storage: StorageAtom = atom<JsonValue>(null);
    // @ts-expect-error a Date is not JSON
    validatedAtom(storage, { when: new Date() }, (_v): _v is { when: Date } => true);
  });
  it("hands a child slot down as its own storage", () => {
    const store = createStore();
    const storage: StorageAtom = atom<JsonValue>(null);
    const typed = validatedAtom(storage, INITIAL, isPanelState);
    const child = subAtom(typed, "child");
    const childTyped = validatedAtom(child, { n: 1 }, (v): v is { n: number } => typeof v === "object" && v !== null && "n" in v);
    store.set(childTyped, { n: 5 });
    expect(store.get(storage)).toEqual({ ...INITIAL, child: { n: 5 } });
  });
});
