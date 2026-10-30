# Phase 2 Step 16: Offline Indicator for LIVE Badge

## Goal

When the new-ticks subscription is not active (e.g., server down, websocket disconnected), change the "LIVE" indicators to show "OFFLINE" with red outline styling instead.

## Current State

The LIVE indicator appears in 3 places, all tied to `isLive` (which just checks `currentTick === 'live'`):

1. **Header badge** - `App.tsx:220-223` - green "LIVE" badge
2. **Timeline live button** - `Timeline.tsx:272-277` - green active state
3. **Timeline bottom button** - `Timeline.tsx:291-297` - shows "● LIVE" or "○ Live"

The subscription is in `ThingWatcherProvider.tsx:72-75` but its connection status is currently ignored.

## Implementation

### 1. Track websocket connection state via graphql-ws lifecycle

The urql subscription result state is unreliable for detecting connection status. Instead, use the graphql-ws client's lifecycle callbacks which have direct knowledge of the websocket state.

**Architecture:**

1. **`useGraphQL.ts`** - Add lifecycle callbacks to `createWSClient` (already done):
```typescript
export interface CreateGraphQLClientOptions {
  onWsConnected?: () => void;
  onWsDisconnected?: () => void;
}

const wsClient = createWSClient({
  url: wsUrl,
  on: {
    connected: () => options.onWsConnected?.(),
    closed: () => options.onWsDisconnected?.(),
  },
});
```

2. **`ThingWatcherProvider.tsx`** - Accept `isWsConnected` as a prop:
```typescript
interface ThingWatcherProviderProps {
  children: React.ReactNode;
  includeStreamingUpdates?: boolean;
  isWsConnected?: boolean;  // NEW - passed from entry point
}

// Remove the urql subscription result logic, just use the prop
const isSubscriptionActive = isWsConnected ?? false;
```

3. **Entry points** (`main.tsx`, `embed.tsx`) - Track state and pass callbacks:
```tsx
function Main() {
  const [isWsConnected, setIsWsConnected] = useState(false);
  const [client] = useState(() => createGraphQLClient({
    onWsConnected: () => setIsWsConnected(true),
    onWsDisconnected: () => setIsWsConnected(false),
  }));

  return (
    <Provider value={client}>
      <ThingWatcherProvider isWsConnected={isWsConnected}>
        <App />
      </ThingWatcherProvider>
    </Provider>
  );
}
```

This keeps the existing structure but wires up the reliable connection tracking via props.

### 2. Update LIVE badge in header (App.tsx)

Change from:
```tsx
{isLive && <span className={styles.liveBadge}>LIVE</span>}
```
To:
```tsx
{isLive && (
  <span className={isSubscriptionActive ? styles.liveBadge : styles.offlineBadge}>
    {isSubscriptionActive ? 'LIVE' : 'OFFLINE'}
  </span>
)}
```

Add `.offlineBadge` style in `App.module.css`:
```css
.offlineBadge {
  color: #ef4444;           /* red-500 */
  border: 1px solid #ef4444;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.7rem;
  font-weight: 700;
  background: transparent;
}
```

### 3. Update Timeline live button (Timeline.tsx)

Timeline uses `useThingWatcher()` directly to get `isSubscriptionActive`.

The small LIVE button next to the tick display always shows "LIVE" text, but only highlights green when on live tick AND connected. When disconnected or viewing a past tick, it stays gray (default style):
```tsx
<div className={`${styles.liveButton} ${currentTick === 'live' && isSubscriptionActive ? styles.liveButtonActive : ''}`}>
  LIVE
</div>
```

The tick display just shows "LIVE" or the tick number (no offline indicator here):
```tsx
<span className={styles.tickDisplay}>
  {currentTick === 'live' ? 'LIVE' : currentTickIndex} / {latestTick?.index ?? '?'}
</span>
```

### 4. Update Timeline bottom button (Timeline.tsx)

The rightmost "● LIVE / ○ Live" button shows "○ OFFLINE" with red styling whenever disconnected, regardless of which tick is currently selected:
```tsx
<button className={`${styles.button} ${isSubscriptionActive ? (isLive ? styles.liveActive : '') : styles.liveOffline}`}>
  {isSubscriptionActive ? (isLive ? '● LIVE' : '○ Live') : '○ OFFLINE'}
</button>
```

Add `.liveOffline` style in `Timeline.module.css`:
```css
.liveOffline {
  color: #ef4444 !important;
  border-color: #ef4444 !important;
  background: transparent !important;
  font-weight: 600;
}
```

## Implementation Steps

**Connection tracking (revised approach):**
- [x] Add `CreateGraphQLClientOptions` with `onWsConnected`/`onWsDisconnected` callbacks to `useGraphQL.ts`
- [x] Update `ThingWatcherProvider.tsx` to accept `isWsConnected` prop instead of using urql subscription result
- [x] Update `main.tsx` to track ws state and pass to provider
- [x] Update `embed.tsx` to track ws state and pass to provider

**UI (already done):**
- [x] Update `App.tsx` header badge
- [x] Add `.offlineBadge` style to `App.module.css`
- [x] Update `Timeline.tsx` to receive and use `isSubscriptionActive`
- [x] Add offline styles to `Timeline.module.css`
- [x] Add status bar to `EmbedApp.tsx`

**Verification:**
- [ ] Manual test: stop backend server, verify OFFLINE shows; restart, verify LIVE returns
- [ ] Update CODE_OVERVIEW.md
