import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { connectViewerEvents } from './api/events'
import { configureViewer, LOCAL_CONFIG, renewViewerHeaders } from './config'
import { hostedHeaders, hostedViewerConfig, isHosted, receiveHostedConfig } from './hosted'
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
// bundle's current state, which the server sends first. A viewer framed by a hub first receives
// which job it shows (hosted.ts); it has no events, since a hub launches no runs and builds no
// bundles: a job pushed again shows up through the periodic refresh (App.tsx's `Jobs`).
async function start() {
  const config = isHosted()
    ? hostedViewerConfig(
        await receiveHostedConfig(window, { onRenewal: (renewal) => renewViewerHeaders(hostedHeaders(renewal)) }),
      )
    : LOCAL_CONFIG
  configureViewer(config)
  const load = await loadViews()
  startLiveViews(load)
  if (config.eventsUrl !== null) connectViewerEvents(config.eventsUrl)
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <LiveApp />
    </StrictMode>,
  )
}

// Nothing is rendered before `start` completes, so a failure (a hub page that never configures
// the frame) is written where the app would have been, rather than leaving the page blank.
start().catch((error: unknown) => {
  const message = document.createElement('p')
  message.textContent = `The viewer could not start: ${error instanceof Error ? error.message : String(error)}`
  document.getElementById('root')!.replaceChildren(message)
})
