// The bundle's entry: what this package contributes to the viewer.

import type { ViewBundle } from "fxtr-view";
import { demoRenderers } from "./index";

const bundle: ViewBundle = { renderers: demoRenderers };

export default bundle;
