frontend
- figure out entity load caching across dispatcher load and session load. should we keep entity alive in dispatcher + share session with renderer?
    - or switch to separate in memory entity data cache
- dispatch entry eviction: LRU?

frontend/backend
- to enable larger sessions: send state updates as JSON patch?
