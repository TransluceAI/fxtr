/**
 * The colour theme: the system's light or dark preference, or one picked in the rail's
 * ThemeToggle. A pick is `data-theme` on <html>, which tokens.css turns into a `color-scheme`, and
 * is kept in localStorage; the script in index.html applies the kept pick before the first paint.
 */

export type Theme = 'system' | 'light' | 'dark'

/** Also read by the script in index.html. */
const STORAGE_KEY = 'fxtr-theme'

/** The theme in effect. */
export function currentTheme(): Theme {
  const picked = document.documentElement.dataset.theme
  return picked === 'light' || picked === 'dark' ? picked : 'system'
}

/** Apply a theme and keep it for later visits. */
export function setTheme(theme: Theme): void {
  const root = document.documentElement
  if (theme === 'system') delete root.dataset.theme
  else root.dataset.theme = theme
  try {
    if (theme === 'system') localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // Storage can be unavailable (a private window, blocked site data); the pick then lasts for
    // this page only.
  }
}
