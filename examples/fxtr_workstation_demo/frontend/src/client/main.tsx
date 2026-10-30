/**
 * React entry. Three startup steps before any rendering:
 *
 *   1. Bundle externals into ``window.__FXTR_INJECTED__`` via
 *      :func:`installFxtrExternals`. The frontend's dynamic
 *      dispatcher loads renderer modules at runtime (via blob
 *      URL imports); the modules carry bare-specifier imports
 *      for `react`, `react/jsx-runtime`, the externalised
 *      `@fxtr-ui/*` packages, plus `@fxtr-ui/agent-plugin-renderer`,
 *      `@fxtr-ui/rd-types-stream`, and `@fxtr-ui/rd-types-channel`
 *      (added for the demo's builtin renderer module, which
 *      re-exports the log-event + agent-MCP-panel + channel-storage
 *      components). The externals list MUST cover every external a
 *      compiled renderer slice declares — the compile-side lists
 *      live in nix (`lib.workstationExternals` plus per-slice
 *      extensions in each `fxtr_renderer.nix`, e.g.
 *      `agent_workdir/renderers/builtin/fxtr_renderer.nix`).
 *   2. Import dockview + workstation styles.
 *   3. Render ``<App />`` under React StrictMode.
 */

import "dockview/dist/styles/dockview.css";
import "@fxtr-ui/panel-viewer/styles/all.css";

import * as agentPluginRenderer from "@fxtr-ui/agent-plugin-renderer";
import * as core from "@fxtr-ui/core";
import * as rdTypesChannel from "@fxtr-ui/rd-types-channel";
import * as rdTypesStream from "@fxtr-ui/rd-types-stream";
import * as react from "@fxtr-ui/react";
import * as reactDispatch from "@fxtr-ui/react-dispatch";
import { installFxtrExternals } from "@fxtr-ui/react-dispatch-dynamic-shim";
import * as reports from "@fxtr-ui/reports";
import * as stateUtil from "@fxtr-ui/state-util";
import * as workstation from "@fxtr-ui/workstation";
import * as workstationDispatch from "@fxtr-ui/workstation-dispatch";
import * as jotai from "jotai";
import * as jotaiEffect from "jotai-effect";
import * as React from "react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import * as ReactJsxRuntime from "react/jsx-runtime";
import { App } from "./App";

installFxtrExternals({
	react: React,
	"react/jsx-runtime": ReactJsxRuntime,
	jotai,
	"jotai-effect": jotaiEffect,
	"@fxtr-ui/core": core,
	"@fxtr-ui/state-util": stateUtil,
	"@fxtr-ui/react": react,
	"@fxtr-ui/react-dispatch": reactDispatch,
	"@fxtr-ui/workstation-dispatch": workstationDispatch,
	"@fxtr-ui/workstation": workstation,
	"@fxtr-ui/agent-plugin-renderer": agentPluginRenderer,
	"@fxtr-ui/rd-types-channel": rdTypesChannel,
	"@fxtr-ui/rd-types-stream": rdTypesStream,
	"@fxtr-ui/reports": reports,
});

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
createRoot(root).render(
	<StrictMode>
		<App />
	</StrictMode>,
);
