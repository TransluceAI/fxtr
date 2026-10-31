import { describe, expect, it } from "vitest";
import { invocationRefKey, type InvocationRef } from "../src/index";

describe("invocationRefKey", () => {
  it("tells workflow and step refs apart and survives pathname punctuation", () => {
    const workflow: InvocationRef = { jobId: "j", kind: "workflow", pathname: 'root/sample[prompt:3,split:"a/b"]' };
    const step: InvocationRef = { jobId: "j", kind: "step", cacheKey: 'root/sample[prompt:3,split:"a/b"]' };
    expect(invocationRefKey(workflow)).not.toBe(invocationRefKey(step));
    expect(invocationRefKey(workflow)).toBe(invocationRefKey({ ...workflow }));
    expect(JSON.parse(invocationRefKey(workflow))).toEqual(["j", "workflow", 'root/sample[prompt:3,split:"a/b"]']);
  });
  it("keeps jobs apart", () => {
    const a: InvocationRef = { jobId: "a", kind: "workflow", pathname: "root" };
    expect(invocationRefKey(a)).not.toBe(invocationRefKey({ ...a, jobId: "b" }));
  });
});
