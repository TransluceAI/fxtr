/**
 * Combined renderer module for Container and Item thing types.
 *
 * Demonstrates the multi-renderer bundle feature where a single module
 * exports renderers for multiple thing types.
 */

import type { ThingWatcherRenderConfig } from 'thingwatcher-bindings';
import {
  ContainerFull,
  ContainerRef,
  ContainerInline,
  ContainerPreview,
  ContainerGraphNode,
} from './Container.tsx';
import {
  ItemFull,
  ItemRef,
  ItemInline,
  ItemPreview,
  ItemGraphNode,
} from './Item.tsx';

export default {
  dialect: "0.1",
  renderers: {
    "Container": {
      full: ContainerFull,
      ref: ContainerRef,
      inline: ContainerInline,
      preview: ContainerPreview,
      graph: ContainerGraphNode,
    },
    "Item": {
      full: ItemFull,
      ref: ItemRef,
      inline: ItemInline,
      preview: ItemPreview,
      graph: ItemGraphNode,
    },
  }
} as ThingWatcherRenderConfig;
