"""The project: what a user's code is part of, and how a launcher talks to it.

A project is a git (or jj) repository of user code that depends on fxtr, marked by a
``pyproject.toml`` with a ``[tool.fxtr]`` table. This package holds what every client of a project
shares and what needs no database extra to import: the project's configuration and location
(:mod:`fxtr.project.config`), the environment that pins its code and checks its venv
(:mod:`fxtr.project.environment`, :mod:`fxtr.project.code_version`), the interface user code
talks to it through (:mod:`fxtr.project.client`), and the ``fxtr`` command line
(:mod:`fxtr.project.cli`). ``CODE_OVERVIEW.md`` beside this file is the map.
"""
