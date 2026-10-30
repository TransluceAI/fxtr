"""Demo server for testing all dynamic rendering features.

This demo tests the following features:
1. ChildNodeView component (recursive rendering) - renders another thing inline
2. NodeRef context menu - right-click to open in panel
3. Compile error display - ErrorBoundary should catch and display
4. Runtime error in custom renderer - ErrorBoundary should catch and display
5. Fallback behavior - things without renderers show JSON
6. FullRenderer and RefRenderer named exports

The renderers are loaded from .tsx files in the renderers/ subdirectory.
These files can be typechecked with Deno: `deno check renderers/*.tsx`
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path
from typing import Optional

import anyio
from anyio import open_signal_receiver

from thingwatcher import ThingLink, ThingRef, ThingState, Watcher, register_snapshotter
from thingwatcher.api import (
    AnchorCapturer,
    StatelessSnapshotter,
    register_anchor_capturer,
    register_renderer,
)
from thingwatcher.registry import configure_rendering

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Directory containing this script (and the .tsx renderer files)
SCRIPT_DIR = Path(__file__).parent


# =============================================================================
# Domain objects
# =============================================================================


class Container:
    """A container that holds items. Used to test ThingView recursive rendering."""

    def __init__(self, name: str):
        self.name = name
        self.items: list["Item"] = []

    def add_item(self, item: "Item"):
        self.items.append(item)


class Item:
    """An item inside a container. Used for recursive rendering tests."""

    def __init__(self, name: str, value: int, container: Container | None = None):
        self.name = name
        self.value = value
        self.container = container
        if container:
            container.add_item(self)


class BrokenCompile:
    """A thing with intentionally broken TSX to test compile error display."""

    def __init__(self, description: str):
        self.description = description


class BrokenRuntime:
    """A thing with a renderer that throws during render."""

    def __init__(self, description: str, crash_mode: str = "always"):
        self.description = description
        self.crash_mode = crash_mode  # 'always', 'conditional'


class NoRenderer:
    """A thing type with no custom renderer - should fall back to JSON view."""

    def __init__(self, name: str, data: dict):
        self.name = name
        self.data = data


class StyledOne:
    """A thing with CSS modules styling (teal). Used to demo shadow DOM scoping."""

    def __init__(self, name: str, description: str, child: "StyledTwo | None" = None):
        self.name = name
        self.description = description
        self.child = child  # Optional child to demonstrate nested shadow DOM


class StyledTwo:
    """A thing with CSS modules styling (red). Same TSX as StyledOne but different CSS."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


class ExternalImport:
    """A thing with a renderer that imports from a full esm.sh URL."""

    def __init__(self, name: str, json_data: dict):
        self.name = name
        self.json_data = json_data


class BareImport:
    """A thing with a renderer that imports by bare module name (gets esm.sh prefix)."""

    def __init__(self, name: str, json_data: dict):
        self.name = name
        self.json_data = json_data


class BrokenImport:
    """A thing with a renderer that has an intentionally bad import."""

    def __init__(self, description: str):
        self.description = description


class GlobalCss:
    """A thing with global (non-module) CSS to test bare classnames."""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


class TwindDemo:
    """A thing that uses Twind (Tailwind-in-JS) for dynamic Tailwind CSS support."""

    def __init__(self, title: str, items: list[str] | None = None):
        self.title = title
        self.items = items or []


class DynamicDateThing:
    """A thing whose renderer is built dynamically with the current date.

    This demonstrates the dynamic renderer builder feature. Each time the
    renderer is refreshed (via the refresh button in the Timeline), the
    builder runs again and generates new TSX with the current timestamp.
    """

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description


# =============================================================================
# Snapshotters
# =============================================================================


class ContainerSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: Container) -> ThingState:
        return ThingState(
            key=("container", obj.name),
            thing_type="Container",
            thing_data={"name": obj.name, "item_count": len(obj.items)},
            links=[
                ThingLink(
                    link_type="item",
                    link_key=("item", item.name),
                    to_thing_key=("item", item.name),
                )
                for item in obj.items
            ],
        )


class ItemSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: Item) -> ThingState:
        links = []
        if obj.container:
            links.append(
                ThingLink(
                    link_type="container",
                    link_key="parent",
                    to_thing_key=("container", obj.container.name),
                )
            )
        return ThingState(
            key=("item", obj.name),
            thing_type="Item",
            thing_data={"name": obj.name, "value": obj.value},
            links=links,
        )


class BrokenCompileSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: BrokenCompile) -> ThingState:
        return ThingState(
            key=("broken_compile", obj.description),
            thing_type="BrokenCompile",
            thing_data={"description": obj.description},
            links=[],
        )


class BrokenRuntimeSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: BrokenRuntime) -> ThingState:
        return ThingState(
            key=("broken_runtime", obj.description),
            thing_type="BrokenRuntime",
            thing_data={"description": obj.description, "crash_mode": obj.crash_mode},
            links=[],
        )


class NoRendererSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: NoRenderer) -> ThingState:
        return ThingState(
            key=("no_renderer", obj.name),
            thing_type="NoRenderer",
            thing_data={"name": obj.name, **obj.data},
            links=[
                ThingLink(link_type="basic_link", link_key=linkkey, to_thing_key=linkkey)
                for linkkey in obj.data.get("links", [])
            ],
        )


class StyledOneSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: StyledOne) -> ThingState:
        links = []
        if obj.child:
            links.append(
                ThingLink(
                    link_type="child",
                    link_key=("styled_two", obj.child.name),
                    to_thing_key=("styled_two", obj.child.name),
                )
            )
        return ThingState(
            key=("styled_one", obj.name),
            thing_type="StyledOne",
            thing_data={"name": obj.name, "description": obj.description},
            links=links,
        )


class StyledTwoSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: StyledTwo) -> ThingState:
        return ThingState(
            key=("styled_two", obj.name),
            thing_type="StyledTwo",
            thing_data={"name": obj.name, "description": obj.description},
            links=[],
        )


class ExternalImportSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: ExternalImport) -> ThingState:
        return ThingState(
            key=("external_import", obj.name),
            thing_type="ExternalImport",
            thing_data={"name": obj.name, "json_data": obj.json_data},
            links=[],
        )


class BareImportSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: BareImport) -> ThingState:
        return ThingState(
            key=("bare_import", obj.name),
            thing_type="BareImport",
            thing_data={"name": obj.name, "json_data": obj.json_data},
            links=[],
        )


class BrokenImportSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: BrokenImport) -> ThingState:
        return ThingState(
            key=("broken_import", obj.description),
            thing_type="BrokenImport",
            thing_data={"description": obj.description},
            links=[],
        )


class GlobalCssSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: GlobalCss) -> ThingState:
        return ThingState(
            key=("global_css", obj.name),
            thing_type="GlobalCss",
            thing_data={"name": obj.name, "description": obj.description},
            links=[],
        )


class TwindDemoSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: TwindDemo) -> ThingState:
        return ThingState(
            key=("twind_demo", obj.title),
            thing_type="TwindDemo",
            thing_data={"title": obj.title, "items": obj.items},
            links=[],
        )


class DynamicDateThingSnapshotter(StatelessSnapshotter):
    def snapshot(self, obj: DynamicDateThing) -> ThingState:
        return ThingState(
            key=("dynamic_date", obj.name),
            thing_type="DynamicDateThing",
            thing_data={"name": obj.name, "description": obj.description},
            links=[],
        )


# =============================================================================
# Anchor capturers
# =============================================================================


class ContainerAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: Container) -> ThingRef:
        return ThingRef(key=("container", anchor.name))


class ItemAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: Item) -> ThingRef:
        return ThingRef(key=("item", anchor.name))


class BrokenCompileAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: BrokenCompile) -> ThingRef:
        return ThingRef(key=("broken_compile", anchor.description))


class BrokenRuntimeAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: BrokenRuntime) -> ThingRef:
        return ThingRef(key=("broken_runtime", anchor.description))


class NoRendererAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: NoRenderer) -> ThingRef:
        return ThingRef(key=("no_renderer", anchor.name))


class StyledOneAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: StyledOne) -> ThingRef:
        return ThingRef(key=("styled_one", anchor.name))


class StyledTwoAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: StyledTwo) -> ThingRef:
        return ThingRef(key=("styled_two", anchor.name))


class ExternalImportAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: ExternalImport) -> ThingRef:
        return ThingRef(key=("external_import", anchor.name))


class BareImportAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: BareImport) -> ThingRef:
        return ThingRef(key=("bare_import", anchor.name))


class BrokenImportAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: BrokenImport) -> ThingRef:
        return ThingRef(key=("broken_import", anchor.description))


class GlobalCssAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: GlobalCss) -> ThingRef:
        return ThingRef(key=("global_css", anchor.name))


class TwindDemoAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: TwindDemo) -> ThingRef:
        return ThingRef(key=("twind_demo", anchor.title))


class DynamicDateThingAnchorCapturer(AnchorCapturer):
    def handle(self, anchor: DynamicDateThing) -> ThingRef:
        return ThingRef(key=("dynamic_date", anchor.name))


# =============================================================================
# Dynamic renderer builder
# =============================================================================


def dynamic_date_builder(thing_type: str):
    """Builder that generates a renderer with the current timestamp.

    This demonstrates dynamic renderer generation. Each time the renderer
    is refreshed, this function runs again with a new timestamp.
    """
    from datetime import datetime

    from thingwatcher import RendererBuilderResult

    if thing_type == "DynamicDateThing":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return RendererBuilderResult(
            tsx_source=f"""
import type {{ FullRendererProps, ThingWatcherRenderConfig }} from 'thingwatcher-bindings';
import {{ usePayload }} from 'thingwatcher-bindings';

function DynamicDateFull({{ handle }}: FullRendererProps) {{
    const payload = usePayload(handle);
    return (
        <div style={{{{ padding: '12px', fontFamily: 'system-ui' }}}}>
            <div style={{{{ fontWeight: 'bold', fontSize: '16px', marginBottom: '8px' }}}}>
                {{payload?.name}}
            </div>
            <div style={{{{ color: '#666', marginBottom: '12px' }}}}>
                {{payload?.description}}
            </div>
            <div style={{{{
                background: '#1a1a2e',
                padding: '12px',
                borderRadius: '6px',
                border: '1px solid #333'
            }}}}>
                <div style={{{{ color: '#888', fontSize: '12px', marginBottom: '4px' }}}}>
                    Renderer built at:
                </div>
                <div style={{{{ color: '#4ecdc4', fontSize: '18px', fontFamily: 'monospace' }}}}>
                    {now}
                </div>
            </div>
            <div style={{{{ color: '#666', fontSize: '12px', marginTop: '8px', fontStyle: 'italic' }}}}>
                Click the refresh button (↻) in the Timeline to regenerate this renderer
            </div>
        </div>
    );
}}

export default {{
    dialect: "0.1",
    renderers: {{
        "DynamicDateThing": {{ full: DynamicDateFull }}
    }}
}} as ThingWatcherRenderConfig;
"""
        )
    return None


# =============================================================================
# Main demo
# =============================================================================


async def setup_demo(watcher: Watcher):
    """Set up the demo with all test cases."""
    # Register snapshotters
    register_snapshotter(Container, ContainerSnapshotter())
    register_snapshotter(Item, ItemSnapshotter())
    register_snapshotter(BrokenCompile, BrokenCompileSnapshotter())
    register_snapshotter(BrokenRuntime, BrokenRuntimeSnapshotter())
    register_snapshotter(NoRenderer, NoRendererSnapshotter())
    register_snapshotter(StyledOne, StyledOneSnapshotter())
    register_snapshotter(StyledTwo, StyledTwoSnapshotter())
    register_snapshotter(ExternalImport, ExternalImportSnapshotter())
    register_snapshotter(BareImport, BareImportSnapshotter())
    register_snapshotter(BrokenImport, BrokenImportSnapshotter())
    register_snapshotter(GlobalCss, GlobalCssSnapshotter())
    register_snapshotter(TwindDemo, TwindDemoSnapshotter())
    register_snapshotter(DynamicDateThing, DynamicDateThingSnapshotter())

    # Register anchor capturers
    register_anchor_capturer(Container, ContainerAnchorCapturer())
    register_anchor_capturer(Item, ItemAnchorCapturer())
    register_anchor_capturer(BrokenCompile, BrokenCompileAnchorCapturer())
    register_anchor_capturer(BrokenRuntime, BrokenRuntimeAnchorCapturer())
    register_anchor_capturer(NoRenderer, NoRendererAnchorCapturer())
    register_anchor_capturer(StyledOne, StyledOneAnchorCapturer())
    register_anchor_capturer(StyledTwo, StyledTwoAnchorCapturer())
    register_anchor_capturer(ExternalImport, ExternalImportAnchorCapturer())
    register_anchor_capturer(BareImport, BareImportAnchorCapturer())
    register_anchor_capturer(BrokenImport, BrokenImportAnchorCapturer())
    register_anchor_capturer(GlobalCss, GlobalCssAnchorCapturer())
    register_anchor_capturer(TwindDemo, TwindDemoAnchorCapturer())
    register_anchor_capturer(DynamicDateThing, DynamicDateThingAnchorCapturer())

    # Configure rendering colors
    configure_rendering("Container", "#4ecdc4")
    configure_rendering("Item", "#ff6b6b")
    configure_rendering("BrokenCompile", "#ff4444")
    configure_rendering("BrokenRuntime", "#ff8800")
    configure_rendering("NoRenderer", "#888888")  # No renderer registered for this type
    configure_rendering("StyledOne", "#4ecdc4")  # Teal
    configure_rendering("StyledTwo", "#ff6b6b")  # Red
    configure_rendering("ExternalImport", "#9cdcfe")  # Light blue
    configure_rendering("BareImport", "#dcdcaa")  # Yellow
    configure_rendering("BrokenImport", "#ff4444")  # Red (broken)
    configure_rendering("GlobalCss", "#4a9e3f")  # Green (global CSS demo)
    configure_rendering("TwindDemo", "#60a5fa")  # Blue (Tailwind demo)
    configure_rendering("DynamicDateThing", "#e879f9")  # Purple (dynamic builder demo)

    # Register the dynamic renderer builder
    # NOTE: We don't register a static renderer for DynamicDateThing - the builder handles it
    from thingwatcher import register_renderer_builder

    register_renderer_builder(dynamic_date_builder)

    # Register custom renderers from .tsx files
    # Combined Container + Item bundle using the list form
    # Also does hot reloading by passing paths directly without reading them
    register_renderer(
        ["Container", "Item"],
        (SCRIPT_DIR / "container_item.ts"),
        imports={
            "./Container.tsx": (SCRIPT_DIR / "Container.tsx"),
            "./Item.tsx": (SCRIPT_DIR / "Item.tsx"),
        },
    )

    # Individual test renderers
    register_renderer("BrokenCompile", (SCRIPT_DIR / "BrokenCompile.tsx").read_text())
    register_renderer("BrokenRuntime", (SCRIPT_DIR / "BrokenRuntime.tsx").read_text())
    register_renderer("ExternalImport", (SCRIPT_DIR / "ExternalImport.tsx").read_text())
    register_renderer("BareImport", (SCRIPT_DIR / "BareImport.tsx").read_text())
    register_renderer("BrokenImport", (SCRIPT_DIR / "BrokenImport.tsx").read_text())
    # Intentionally NOT registering a renderer for NoRenderer type

    # Global CSS demo (non-module CSS with bare classnames)
    register_renderer(
        "GlobalCss",
        (SCRIPT_DIR / "GlobalCss.tsx").read_text(),
        imports={
            "./GlobalCss.css": (SCRIPT_DIR / "GlobalCss.css").read_text(),
        },
    )

    # Register CSS module renderers for StyledOne/StyledTwo
    # Both use the same TSX but with different CSS (Python-side string replacement)
    # Using the mapping form to map thing types to renderer keys
    styled_thing_tsx = (SCRIPT_DIR / "StyledThing.tsx").read_text()
    styled_thing_css = (SCRIPT_DIR / "StyledThing.module.css").read_text()
    styled_thing_lib_tsx = (SCRIPT_DIR / "StyledThingLib.tsx").read_text()

    # StyledOne: uses original teal color (#4ecdc4)
    register_renderer(
        {"StyledOne": "StyledThing"},
        styled_thing_tsx,
        imports={
            "./StyledThing.module.css": styled_thing_css,
            "./StyledThingLib.tsx": styled_thing_lib_tsx,
        },
    )

    # StyledTwo: uses modified red color (#ff6b6b) via Python string replace
    styled_thing_css_red = styled_thing_css.replace("#4ecdc4", "#ff6b6b")
    register_renderer(
        {"StyledTwo": "StyledThing"},
        styled_thing_tsx,
        imports={
            "./StyledThing.module.css": styled_thing_css_red,
            "./StyledThingLib.tsx": styled_thing_lib_tsx,
        },
    )

    # Twind demo (Tailwind-in-JS) - demonstrates usePersistentGlobalState
    register_renderer("TwindDemo", (SCRIPT_DIR / "TwindDemo.tsx").read_text())

    print("\n=== Dynamic Rendering Demo on port 8765 ===\n")
    print("This demo tests all dynamic rendering features:\n")
    print("1. ChildNodeView (recursive rendering) - Container shows Items inline")
    print("2. NodeRef context menu - Right-click links to open in panel")
    print("3. Compile error display - BrokenCompile has invalid TSX")
    print("4. Runtime error display - BrokenRuntime throws during render")
    print("5. Fallback to JSON - NoRenderer has no custom renderer")
    print("6. FullRenderer/RefRenderer named exports - new renderer API")
    print("7. CSS modules + shadow DOM - StyledOne (teal) renders StyledTwo (red) as child")
    print("8. Global CSS - GlobalCss uses bare classnames without CSS modules")
    print(
        "9. Twind (Tailwind-in-JS) - TwindDemo uses usePersistentGlobalState for shared twind instance"
    )
    print("10. Dynamic renderer builder - DynamicDateThing uses a builder function to generate TSX")
    print()
    print(f"Renderers loaded from: {SCRIPT_DIR}")
    print("Typecheck with: deno check *.tsx")
    print()

    # Create test data
    print("Creating test data...")

    # Test 1 & 2: Container with items (tests ThingView and ThingLink)
    container = Container("TestBox")
    watcher.track(container)
    watcher.add_root("container", container)

    items = [
        Item("Alpha", 1, container),
        Item("Beta", 2, container),
        Item("Gamma", 3, container),
    ]
    for item in items:
        watcher.track(item)

    watcher.tick("info", "Created container with items")

    # Test 3: Broken compile
    broken_compile = BrokenCompile("This thing has broken TSX")
    watcher.track(broken_compile)
    watcher.add_root("broken_compile", broken_compile)
    watcher.tick("info", "Created broken compile thing")

    # Test 4: Broken runtime
    broken_runtime = BrokenRuntime("This thing crashes during render", "always")
    watcher.track(broken_runtime)
    watcher.add_root("broken_runtime", broken_runtime)
    watcher.tick("info", "Created broken runtime thing")

    # Test 5: No renderer (fallback)
    no_renderer_child = NoRenderer(
        "FallbackTestChild",
        {"field1": "value1", "field2": 42, "nested": {"a": 1, "b": 2}},
    )
    watcher.track(no_renderer_child)
    no_renderer = NoRenderer(
        "FallbackTest",
        {
            "field1": "value1",
            "field2": 42,
            "nested": {"a": 1, "b": 2},
            "links": [("no_renderer", no_renderer_child.name)],
        },
    )
    watcher.track(no_renderer)
    watcher.add_root("no_renderer", no_renderer)
    watcher.tick("info", "Created no-renderer thing")

    # Test 6: CSS modules and shadow DOM scoping
    # StyledOne (teal) renders StyledTwo (red) as a child
    # Each has its own shadow DOM, so styles don't conflict
    styled_two = StyledTwo(
        "ChildWidget",
        "I am a red child rendered inside a teal parent",
    )
    watcher.track(styled_two)

    styled_one = StyledOne(
        "ParentWidget",
        "I am a teal parent with a red child",
        child=styled_two,
    )
    watcher.track(styled_one)
    watcher.add_root("styled_css", styled_one)
    watcher.tick("info", "Created styled things with CSS modules")

    # Test 7: External import from full esm.sh URL
    # Tests that full URLs are passed through unchanged (not double-prefixed)
    external_import = ExternalImport(
        "JsonViewer (full URL)",
        {"example": "data", "nested": {"a": 1, "b": 2}, "list": [1, 2, 3]},
    )
    watcher.track(external_import)
    watcher.add_root("external_import", external_import)
    watcher.tick("info", "Created external import thing")

    # Test 8: Bare import (module name without URL)
    # Tests that bare imports correctly get the esm.sh prefix added
    bare_import = BareImport(
        "JsonViewer (bare import)",
        {"mode": "bare import", "values": [10, 20, 30]},
    )
    watcher.track(bare_import)
    watcher.add_root("bare_import", bare_import)
    watcher.tick("info", "Created bare import thing")

    # Test 9: Broken import (intentionally bad package name)
    # Tests that import failures show meaningful error messages
    broken_import = BrokenImport("This thing has a bad import")
    watcher.track(broken_import)
    watcher.add_root("broken_import", broken_import)
    watcher.tick("info", "Created broken import thing")

    # Test 10: Global CSS (non-module)
    # Tests that .css files (not .module.css) use the css loader for bare classnames
    global_css = GlobalCss(
        "GlobalStyleBox",
        "I use global CSS with bare classnames like className='global-box'",
    )
    watcher.track(global_css)
    watcher.add_root("global_css", global_css)
    watcher.tick("info", "Created global CSS thing")

    # Test 11: Twind (Tailwind-in-JS) demo
    # Tests usePersistentGlobalState for shared module state
    twind_demo = TwindDemo(
        "Shopping List",
        items=["Apples", "Bananas", "Oranges", "Milk", "Bread"],
    )
    watcher.track(twind_demo)
    watcher.add_root("twind_demo", twind_demo)

    # Create a second TwindDemo to verify shared state works
    twind_demo2 = TwindDemo(
        "Todo Items",
        items=["Write code", "Test features", "Update docs"],
    )
    watcher.track(twind_demo2)
    watcher.tick("info", "Created Twind demo things")

    # Test 12: Dynamic renderer builder
    # Tests that renderer builders generate TSX on-demand with dynamic content
    dynamic_date = DynamicDateThing(
        "DynamicRenderer",
        "This renderer was built dynamically by a Python function. "
        "Click the refresh button (↻) in the Timeline to regenerate it with a new timestamp.",
    )
    watcher.track(dynamic_date)
    watcher.add_root("dynamic_date", dynamic_date)
    watcher.tick("info", "Created dynamic date thing")

    print("\nTest things created and added as roots:")
    print("  - container: Container with 3 items (test ChildNodeView/NodeRef)")
    print("  - broken_compile: Has invalid TSX (test compile error display)")
    print("  - broken_runtime: Throws during render (test runtime error display)")
    print("  - no_renderer: No custom renderer (test JSON fallback)")
    print("  - styled_css: StyledOne (teal) with StyledTwo child (red) - CSS module scoping")
    print("  - external_import: Imports from full esm.sh URL (test URL pass-through)")
    print("  - bare_import: Imports by module name (test esm.sh prefix addition)")
    print("  - broken_import: Has bad import (test import error display)")
    print("  - global_css: Global CSS with bare classnames (test non-module CSS)")
    print("  - twind_demo: Twind (Tailwind-in-JS) with usePersistentGlobalState")
    print("  - dynamic_date: Dynamic renderer builder (refresh to see timestamp change)")
    print()
    print("Open http://localhost:8765 to test the features.")
    print("Click on each root in the left panel to see the different behaviors.")
    print()
    print("Press Ctrl+C to stop the server.")


async def main(db_path: Optional[str]):
    if db_path is None:
        import datetime

        db_path = f"/tmp/demo_dynamic_rendering_{datetime.datetime.now().isoformat()}.db"
        print(f"Using auto database file: {db_path}")

    watcher = Watcher(db_path=db_path, port=8765)
    shutdown_event = asyncio.Event()

    async def signal_handler():
        with open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for signum in signals:
                if shutdown_event.is_set():
                    print("Already shutting down, cancelling")
                    outer_cancel_scope.cancel()
                    return
                else:
                    if signum == signal.SIGINT:
                        print("\nShutting down gracefully...")
                    else:
                        print("\nTerminated! Shutting down gracefully...")
                    shutdown_event.set()
                    workflow_cancel_scope.cancel()

    with anyio.CancelScope() as outer_cancel_scope:
        async with anyio.create_task_group() as signals_tg:
            signals_tg.start_soon(signal_handler)
            async with watcher.watching_and_serving():
                try:
                    with anyio.CancelScope() as workflow_cancel_scope:
                        await setup_demo(watcher)
                    print("Demo setup complete, waiting for shutdown signal...")
                    await shutdown_event.wait()
                finally:
                    shutdown_event.set()
                print("Main loop ready to exit, just waiting on watcher")
            signals_tg.cancel_scope.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Demo server for testing dynamic rendering features"
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database file (default: auto-generated temp file)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.db))
