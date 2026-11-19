"""The debug viewer's GraphQL backend (see ``CODE_OVERVIEW.md``).

An isolated, removable read-only server over the Postgres runner's state. Nothing else in the
package imports it; deleting this folder (and the ``debug-web`` extra) removes the viewer. Needs the
``fxtr[debug-web]`` extra.
"""

from __future__ import annotations

from fxtr.runner.postgres.debug_web.app import build_schema, create_app, create_reader_app
from fxtr.runner.postgres.debug_web.reader import ViewerReader
from fxtr.runner.postgres.debug_web.scope import JobScope

__all__ = ["JobScope", "ViewerReader", "build_schema", "create_app", "create_reader_app"]
