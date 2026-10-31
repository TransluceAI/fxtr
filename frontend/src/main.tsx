import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { connectViewerEvents } from './api/events'
import { loadViews } from './views/registry'
import { startLiveViews, useLiveViews } from './views/liveViews'
import './styles/tokens.css'
import './styles/primitives.css'
import './styles/views.css'

/** The app over the project's renderers as they are now: replaced in place when `fxtr view`
 *  rebuilds them (views/liveViews.ts). */
function LiveApp() {
  return <App views={useLiveViews().views} />
}

// The project's view bundle is loaded before the first render, so the first paint never shows the
// viewer's defaults in its renderers' place; later builds, announced on the viewer's events, replace
// it while the page runs. The page follows the events from before it connects, so it hears the
// bundle's current state, which the server sends first.
void loadViews().then((load) => {
  startLiveViews(load)
  connectViewerEvents()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <LiveApp />
    </StrictMode>,
  )
})
