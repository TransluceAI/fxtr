"""Run the project arrays walkthrough (:mod:`basic_project.project_arrays`) against this
project's database.

``uv run python launch_project_arrays.py`` runs it with the local client's defaults.
"""

import anyio

from basic_project.project_arrays import walkthrough
from fxtr.runner.postgres.client import LocalProjectClient


async def main() -> None:
    async with LocalProjectClient.from_project(__file__) as client:
        await walkthrough(client)


if __name__ == "__main__":
    anyio.run(main)
