// @vitest-environment node
// This package builds into a view bundle a shell can load: build it with its own Vite config into
// a temporary directory, import the module, and call the factory with this process's shared
// modules. The renderers come back as the source declares them, and they use the shell's instances
// (a renderer's slot is the very object the shell's fxtr-view declares).

import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import * as fxtrView from "fxtr-view";
import { VIEW_API_VERSION, VIEW_BUNDLE_MANIFEST_FILENAME, type ViewBundleFactory, type ViewBundleManifest } from "fxtr-view";
import * as jotai from "jotai";
import * as react from "react";
import * as jsxRuntime from "react/jsx-runtime";
import { build } from "vite";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { rolloutRenderers } from "../src/index";

const PACKAGE = path.resolve(__dirname, "..");

describe("the view bundle", () => {
  let outDir: string;
  beforeAll(async () => {
    outDir = await mkdtemp(path.join(tmpdir(), "behavior_rollouts-views-"));
    await build({ root: PACKAGE, configFile: path.join(PACKAGE, "vite.config.ts"), configLoader: "runner", logLevel: "silent", build: { outDir } });
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
      rolloutRenderers.map((r) => [r.slot.debugName, r.subjectType, r.id]),
    );
    const slots = new Set([fxtrView.EntityPanel, fxtrView.EntityInlineLink, fxtrView.EntityHoverPreview, fxtrView.InvocationPanel]);
    expect(bundle.renderers.every((r) => slots.has(r.slot))).toBe(true);
  });
});
