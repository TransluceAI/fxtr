/** Apple keyboards use Command; Windows and Linux keyboards use Control. */
function isApplePlatform(): boolean {
  return /^(Mac|iPhone|iPad|iPod)/.test(navigator.platform)
}

/** The primary shortcut modifier shown in help and key bindings. */
export function primaryModifierLabel(): '⌘' | 'Ctrl' {
  return isApplePlatform() ? '⌘' : 'Ctrl'
}

/** Require the platform's primary modifier without the other platform's modifier. */
export function hasPrimaryModifier(event: { metaKey: boolean; ctrlKey: boolean }): boolean {
  return isApplePlatform() ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey
}

/** Keystrokes belong to whatever the user is typing into, not to a pane's shortcuts. */
export function isEditableTarget(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName) || target.isContentEditable)
  )
}
