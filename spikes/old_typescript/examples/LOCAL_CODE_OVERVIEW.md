# @fxtr/examples — Code Overview

Minimal example demonstrating the core fxtr workflow: define entities, store them on a server, discover and query them from a client.

## Entities

Three simple bicodec entities:

- **Author**: `{ name, email }`
- **Tag**: `{ label }`
- **Article**: `{ title, body, author: EntityRef<Author>, tags: EntityRef<Tag>[] }`

## Server

Creates example data (2 authors, 3 tags, 2 articles with relationships), creates named pins, serves via Hono + fxtr HTTP on port 3000.

## CLI Client

Command-line example that lists pins, retrieves articles by pin name, loads linked authors and tags (automatic deep fetch), and demonstrates related entity traversal.

## Web Client

React app that connects to the server and provides:

- **PinBrowser**: Lists all pins, handles selection.
- **ArticleView**: Rich typed view for known entity types (uses `useFxtrLoad` + Suspense).
- **UnknownEntityView**: Generic fallback with collapsible CBOR tree viewer.
- View mode toggle (rich vs raw).

Uses `unionLoadCodec` to dispatch between Article, Author, and Tag codecs.

## Files

```
src/
├── entities.ts           # Article, Author, Tag definitions
├── server.ts             # Server with linked entities
├── client.ts             # CLI client: connect, query, print
└── web/
    ├── main.tsx          # React entry point
    ├── App.tsx           # HTTP namespace setup, view mode toggle
    ├── PinBrowser.tsx    # Pin listing and selection
    ├── ArticleView.tsx   # Rich typed entity view
    ├── UnknownEntityView.tsx  # Collapsible CBOR tree viewer
    └── ErrorBoundary.tsx
vite.config.ts
index.html
```

## Running

```bash
pnpm server              # starts server on port 3000
pnpm client -- http://localhost:3000   # CLI client
pnpm web                 # web client on port 5173
```

## Dependencies

- `@fxtr/core`, `@fxtr/http-server`, `@fxtr/http-client`, `@fxtr/react`
