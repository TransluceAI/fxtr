from __future__ import annotations

import typing


def _exec_fixtures_file():
    import __future__

    import pathlib

    from . import plan_fixtures as _plan_fixtures

    fixtures_file = pathlib.Path(_plan_fixtures.__file__)
    compiled = compile(
        fixtures_file.read_text(),
        filename=fixtures_file.name,
        mode="exec",
        flags=__future__.annotations.compiler_flag,
        dont_inherit=True,
    )
    exec(compiled, globals())


if typing.TYPE_CHECKING:
    from .plan_fixtures import *  # noqa
else:
    _exec_fixtures_file()
