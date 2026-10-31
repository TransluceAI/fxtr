"""The basic example project's code: entity types, steps, and workflows for a few small
experiments, the helpers that print their results, and the bodies of the walkthroughs the
launcher scripts run.

This package is imported in two environments. Its own project's venv runs the launchers (this is a
standalone uv project with fxtr as an editable dependency), and fxtr's own test suite imports it
through pytest's ``pythonpath`` to run the experiments against a test database. It depends on
nothing but fxtr, so the same code works in both.

Every step and workflow here declares an explicit ``name=``: the project's invocation renderers
(the ``views/`` renderer package beside this one) are keyed on these short names.
"""
