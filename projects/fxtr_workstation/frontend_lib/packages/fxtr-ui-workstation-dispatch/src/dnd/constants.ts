/**
 * Drag-and-drop MIME constants shared between the workstation shell
 * (which produces ref drags) and renderer modules (which may want to
 * accept ref drops onto their own surfaces — see ``parseRefDragData``
 * and ``useRefDropRegion``).
 *
 * Owned by ``@fxtr-ui/workstation-dispatch`` so externalised renderer
 * modules can import the same constant the shell uses without
 * depending on the shell package itself.
 */

/**
 * MIME type carried by all drags of an entity or service ref out of
 * the workstation shell. The payload value is a ``JSON.stringify``ed
 * shell-internal drag-payload shape; for renderers that only need to
 * know "what ref is being dragged", use ``parseRefDragData`` instead
 * of decoding the payload directly.
 */
export const DRAG_MIME_FXTR_REF = "application/x-fxtr-ref";
