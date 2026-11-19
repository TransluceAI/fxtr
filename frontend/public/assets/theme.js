// The theme picked in the rail (src/lib/theme.ts), applied before the first paint: this script is
// loaded in <head> without `defer`, so parsing waits for it. Storage is unavailable in a hub's
// sandboxed frame, where no pick is kept.
try {
  const theme = localStorage.getItem('fxtr-theme')
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme
} catch {}
