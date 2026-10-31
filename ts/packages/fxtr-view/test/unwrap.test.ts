import { describe, expect, it } from "vitest";
import { unwrap } from "../src/index";

describe("unwrap", () => {
  it("returns the value, throws the promise while loading, throws the error on failure", () => {
    expect(unwrap({ status: "loaded", value: 3 })).toBe(3);
    const promise = new Promise(() => {});
    const thrown = (fn: () => unknown) => {
      try {
        fn();
      } catch (e) {
        return e;
      }
      return undefined;
    };
    expect(thrown(() => unwrap({ status: "loading", promise }))).toBe(promise);
    const error = new Error("nope");
    expect(thrown(() => unwrap({ status: "error", error }))).toBe(error);
  });
});
