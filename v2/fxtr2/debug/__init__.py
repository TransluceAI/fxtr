"""Debugging facilities for fxtr2.

Deliberately empty: import the concrete modules directly
(``fxtr2.debug.scope_report`` for the user-facing scope-drain report,
``fxtr2.debug.scope_instrumentation`` for the always-on hooks the core
modules feed). ``scope_report`` imports the core, so re-exporting it
here would create an import cycle with the core modules that import
``scope_instrumentation``.
"""
