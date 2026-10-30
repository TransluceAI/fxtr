// Combined Clydra renderers - imports from separate files
// Typecheck with: deno check index.tsx
import type { ThingWatcherRenderConfig } from "thingwatcher-bindings";

import { renderers as ClydraRoot } from "./ClydraRoot.tsx";
import { renderers as FileList } from "./FileListIndicator.tsx";
import { renderers as InterruptButton } from "./InterruptButton.tsx";
import { renderers as PermissionDialog } from "./PermissionDialog.tsx";
import { renderers as PermissionMode } from "./PermissionMode.tsx";
import { renderers as ModelPicker } from "./ModelPicker.tsx";

export default {
  dialect: "0.1",
  renderers: {
    ClydraRoot,
    FileList,
    InterruptButton,
    PermissionDialog,
    PermissionMode,
    ModelPicker,
  },
} as ThingWatcherRenderConfig;
