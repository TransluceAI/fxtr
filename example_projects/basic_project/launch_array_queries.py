"""Launch the array queries experiment (:mod:`basic_project.array_queries`).

``uv run python launch_array_queries.py`` runs it as a job on this project's Postgres database.
"""

from basic_project.array_queries import evaluate_models
from fxtr.project.running import launch_cli

if __name__ == "__main__":
    launch_cli(evaluate_models, {}, anchor=__file__)
