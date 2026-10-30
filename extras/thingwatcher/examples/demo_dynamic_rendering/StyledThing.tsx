// Demo renderer for CSS modules and shadow DOM scoping
// This single file is used for both StyledOne and StyledTwo - the CSS is modified
// via Python-side string replacement to create different color variants.

// @ts-expect-error: CSS modules are handled by esbuild (local-css loader)
import styles from './StyledThing.module.css';
import {
  ChildNodeView,
  usePayload, useLinks, useDraggableIdMarker, useThingMetadata
} from 'thingwatcher-bindings';
import type { ThingWatcherRenderConfig, FullRendererProps, RefRendererProps, PreviewRendererProps } from 'thingwatcher-bindings';
import { RefRenderer as StyledThingRef } from './StyledThingLib.tsx';

function StyledThingFull({ handle }: FullRendererProps) {
  const payload = usePayload(handle);
  const links = useLinks(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const data = payload as { name?: string; description?: string } | null;
  const childLink = links.find(l => l.linkType === 'child');

  return (
    <div className={styles.container}>
      <DraggableIDMarker>
        <h3 className={styles.title}>{data?.name}</h3>
      </DraggableIDMarker>
      <p className={styles.description}>{data?.description}</p>
      {childLink && (
        <div className={styles.childContainer}>
          <div className={styles.childLabel}>Child:</div>
          <ChildNodeView thingKey={childLink.toThingKey} />
        </div>
      )}
    </div>
  );
}

function StyledThingInline({ handle }: RefRendererProps) {
  const payload = usePayload(handle);
  const data = payload as { name?: string } | null;
  return (
    <span className={styles.inlineRef}>
      <span className={styles.inlineRefDot} />
      {data?.name}
    </span>
  );
}

function StyledThingPreview({ handle }: PreviewRendererProps) {
  const payload = usePayload(handle);
  const { shortId } = useThingMetadata(handle);
  const DraggableIDMarker = useDraggableIdMarker(handle);

  const data = payload as { name?: string; description?: string } | null;

  return (
    <div className={styles.preview}>
      <DraggableIDMarker>
        <div className={styles.previewHeader}>
          <span className={styles.previewDot} />
          <span className={styles.previewName}>{data?.name}</span>
          <span className={styles.previewId}>#{shortId}</span>
        </div>
      </DraggableIDMarker>
      <p className={styles.previewDescription}>{data?.description}</p>
    </div>
  );
}

// Note: This module exports a generic "StyledThing" renderer key.
// The Python demo registers this same module for both StyledOne and StyledTwo
// using the mapping form: {"StyledOne": "StyledThing", "StyledTwo": "StyledThing"}
// with different CSS (color replaced via Python string manipulation).
export default {
  dialect: "0.1",
  renderers: {
    "StyledThing": {
      full: StyledThingFull,
      ref: StyledThingRef,
      inline: StyledThingInline,
      preview: StyledThingPreview,
    },
  }
} as ThingWatcherRenderConfig;
