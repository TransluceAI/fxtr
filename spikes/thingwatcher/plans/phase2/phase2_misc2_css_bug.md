# Phase 2 Misc: CSS Loading Bug on First Page Load

## Problem

When loading the thingwatcher UI for the first time (fresh server start), the page appears unstyled—just text arranged awkwardly on the left side of the screen. Refreshing the page fixes it. The browser's network tab shows that on the first load, the CSS file (`/main.css`) is never requested at all.

## Root Cause

The bug is in `FrontendHandler` in `server.py`. There's a timing issue between HTML serving and frontend compilation.

### Code Flow (Dev Mode or First Load)

In `__init__` (lines 1108-1116):
```python
def __init__(self, dev_mode: bool = False):
    self.compiled_js: str | None = None
    self.compiled_css: str | None = None
    # ...
    if not dev_mode:
        self._compile()  # Only compiles in production mode
```

In dev mode, `_compile()` is skipped, so `compiled_css` remains `None`.

In `serve_html()` (lines 1172-1186):
```python
async def serve_html(self, _request: Request) -> Response:
    html_content = self.html_file.read_text()
    if self.compiled_css:  # <-- None on first request!
        html_content = html_content.replace(
            "</head>",
            '    <link rel="stylesheet" href="/main.css">\n</head>',
        )
    return Response(html_content, media_type="text/html")
```

The CSS `<link>` tag is only inserted if `compiled_css` is truthy—but it's `None`.

In `serve_js()` (lines 1188-1198):
```python
async def serve_js(self, _request: Request) -> Response:
    if self.dev_mode or self.compiled_js is None:
        self._compile()  # Compilation happens HERE
    # ...
```

Only `serve_js()` triggers compilation, not `serve_html()`.

### Timeline of First Load

1. Browser requests `/` (HTML)
2. `serve_html()` returns HTML **without** CSS link (because `compiled_css` is `None`)
3. Browser parses HTML, finds `<script src="/main.js">`, requests it
4. `serve_js()` triggers `_compile()` → `compiled_css` is now populated
5. Page renders without CSS (HTML never told browser to fetch it)

### Timeline of Refresh

1. Browser requests `/` (HTML)
2. `serve_html()` returns HTML **with** CSS link (because `compiled_css` was set by previous JS request)
3. Browser fetches `/main.css`
4. Page renders correctly

## Solution

The cleanest fix: put the CSS `<link>` tag directly in `index.html` so the browser always requests it. The `serve_css()` handler already handles missing CSS gracefully (returns `/* No CSS */` or triggers compilation in dev mode).

This approach:
1. Makes the HTML structure static and predictable
2. Ensures the browser always fetches CSS
3. Removes the dynamic HTML manipulation from `serve_html()`

Changes:
1. Add `<link rel="stylesheet" href="/main.css">` to `index.html`
2. Remove the dynamic CSS link injection from `serve_html()`

## Implementation Steps

- [x] Add CSS link to `index.html`
- [x] Remove dynamic CSS injection from `serve_html()` in `server.py`
- [x] Update CODE_OVERVIEW.md (no changes needed—this is a bug fix, not a structural change)
