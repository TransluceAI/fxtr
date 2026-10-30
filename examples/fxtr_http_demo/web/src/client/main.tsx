/**
 * React entry. Two startup steps before any rendering:
 *
 *   1. Install the import-map externals via `installFxtrExternals`. The
 *      dynamic-dispatcher renderer modules (fetched at runtime as blob
 *      URLs) carry bare-specifier imports (`react`, `@fxtr-ui/*`, …);
 *      this is how they resolve to the host's bundled instances. The
 *      list MUST agree with `DEFAULT_EXTERNALS` in the Python
 *      `compile.py` (server side).
 *   2. Render `<App />` under StrictMode.
 */

import "dockview/dist/styles/dockview.css";
import "@fxtr-ui/panel-viewer/styles/all.css";

import * as core from "@fxtr-ui/core";
import * as react from "@fxtr-ui/react";
import * as reactDispatch from "@fxtr-ui/react-dispatch";
import { installFxtrExternals } from "@fxtr-ui/react-dispatch-dynamic-shim";
import * as stateUtil from "@fxtr-ui/state-util";
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
