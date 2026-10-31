"""Launch the bio refinement experiment.

``uv run python launch_bio_refinement.py`` runs it as a job on this project's Postgres database.
"""

from basic_project.bio_refinement import refine_bios
from fxtr.project.running import launch_cli

if __name__ == "__main__":
    launch_cli(refine_bios, anchor=__file__)
