"""Launch the rollout experiment with two samples per model and scenario.

``uv run python launch_rollout_experiment.py`` runs it as a job on this project's Postgres database.
"""

from basic_project.rollout_experiment import rollout_experiment
from fxtr.entity_defns.array import Array
from fxtr.project.running import launch_cli

if __name__ == "__main__":
    launch_cli(rollout_experiment, {"n_samples": Array.scalar(2, int)}, anchor=__file__)
