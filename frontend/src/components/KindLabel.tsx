import { KIND_ICONS, KIND_ICON_SIZE, KIND_ICON_STROKE } from '../lib/kindIcons'
import type { NodeKind } from '../types'

/** A node kind as its icon then its word, as plain muted text (no pill). */
export function KindLabel({ kind, iconSize = KIND_ICON_SIZE }: { kind: NodeKind; iconSize?: number }) {
  const Icon = KIND_ICONS[kind]
  return (
    <span className="kind-label">
      <Icon size={iconSize} strokeWidth={KIND_ICON_STROKE} aria-hidden />
      {kind}
    </span>
  )
}
