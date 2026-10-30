/**
 * React entry. Two startup steps before any rendering:
 *
 *   1. Install the import-map externals via `installFxtrExternals`. The
 *      dynamic-dispatcher renderer modules (built by nix per the served flake's
 *      `uiPresets`, fetched at runtime as `/data` blob URLs) carry bare-specifier
 *      imports (`react`, `@fxtr-ui/*`, …); this resolves them to the host's
 *      bundled instances. Because this viewer is **flake-agnostic**, it installs
 *      the *union* of every renderer-facing package any served flake might import
 *      — the frontend analogue of `lib.fxtr.workstationExternals` /
 *      `DEFAULT_EXTERNALS`. A new renderer-facing package is added here once.
 *   2. Render `<App />` under StrictMode.
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
	"@fxtr-ui/reports": reports,
	"@fxtr-ui/agent-plugin-renderer": agentPluginRenderer,
	"@fxtr-ui/rd-types-channel": rdTypesChannel,
	"@fxtr-ui/rd-types-stream": rdTypesStream,
});

const rootEl = document.getElementById("root");
if (rootEl === null) {
	throw new Error("#root element missing from index.html");
}
createRoot(rootEl).render(
	<StrictMode>
		<App />
	</StrictMode>,
);
