"""Launch the model/harness sweep.

``uv run python launch_model_harness_sweep.py`` runs a job on this project's Postgres database.
"""

from basic_project.model_harness_sweep import model_harness_experiment
from fxtr.project.running import launch_cli

if __name__ == "__main__":
    launch_cli(model_harness_experiment, anchor=__file__)
