import type { ViewBundle } from "fxtr-view";
import { behaviorsRenderers } from "fxtr-view-behaviors";

const bundle: ViewBundle = {
  renderers: [...behaviorsRenderers],
};

export default bundle;
