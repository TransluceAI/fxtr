// @vitest-environment node
// This package builds into a view bundle a shell can load: run its build script into a temporary
// directory, import the module, and call the factory with this process's shared
// modules. The renderers come back as the source declares them, and they use the shell's instances
// (a renderer's slot is the very object the shell's fxtr-view declares).

import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import * as fxtrView from "fxtr-view";
import { VIEW_API_VERSION, VIEW_BUNDLE_MANIFEST_FILENAME, type ViewBundleFactory, type ViewBundleManifest } from "fxtr-view";
import * as jotai from "jotai";
import * as react from "react";
import * as jsxRuntime from "react/jsx-runtime";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { demoRenderers } from "../src/index";

const PACKAGE = path.resolve(__dirname, "..");

describe("the view bundle", () => {
  let outDir: string;
  beforeAll(async () => {
    outDir = await mkdtemp(path.join(tmpdir(), "basic-project-views-"));
    // Exercise the same script as fxtr views build. Native TS loading must not mask a missing
    // config loader flag on newer Node; older Node versions have no type stripping to disable.
    const nodeOptions = [
      process.env.NODE_OPTIONS,
      process.allowedNodeEnvironmentFlags.has("--no-experimental-strip-types") ? "--no-experimental-strip-types" : "",
    ].filter(Boolean).join(" ");
    await promisify(execFile)("npm", ["run", "build"], {
      cwd: PACKAGE,
      env: { ...process.env, NODE_OPTIONS: nodeOptions, FXTR_VIEW_BUNDLE_OUT_DIR: outDir },
    });
  }, 60_000);
  afterAll(async () => {
    await rm(outDir, { recursive: true, force: true });
  });

  it("has a manifest at the contract's version", async () => {
    const manifest = JSON.parse(await readFile(path.join(outDir, VIEW_BUNDLE_MANIFEST_FILENAME), "utf8")) as ViewBundleManifest;
    expect(manifest.apiVersion).toBe(VIEW_API_VERSION);
    expect(manifest.module).toBe("bundle.js");
  });

  it("loads with the shared modules and returns the package's renderers", async () => {
    const module = (await import(pathToFileURL(path.join(outDir, "bundle.js")).href)) as { default: ViewBundleFactory };
    const bundle = module.default({ react, "react/jsx-runtime": jsxRuntime, jotai, "fxtr-view": fxtrView });
    expect(bundle.renderers.map((r) => [r.slot.debugName, r.subjectType, r.id])).toEqual(
      demoRenderers.map((r) => [r.slot.debugName, r.subjectType, r.id]),
    );
    const slots = new Set([fxtrView.EntityPanel, fxtrView.EntityInlineLink, fxtrView.EntityHoverPreview, fxtrView.InvocationPanel]);
    expect(bundle.renderers.every((r) => slots.has(r.slot))).toBe(true);
  });
});
