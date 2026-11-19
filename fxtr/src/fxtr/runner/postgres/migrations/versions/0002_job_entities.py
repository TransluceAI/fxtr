"""The entities each imported job's record reaches, for a viewer scoped to some jobs.

Revision ID: 0002
Revises: 0001

Adds the ``job_entities`` table. Nothing of the previous release reads or writes a table it does
not declare, so its tables stay usable by that release, as the package docstring requires.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_entities",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("cid", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["cid"], ["entities.cid"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("job_id", "cid"),
        comment="Which entities a job's record reaches: its inputs, results, and everything they"
        " refer to. Filled when a job record is imported, so a viewer that may show only some jobs"
        " can tell which entities belong to them; a job run in this database has no rows here.",
    )
    op.create_index("job_entities_cid", "job_entities", ["cid"], unique=False)


def downgrade() -> None:
    # What it held an upgrade records again (``record_job_entities``), and an import refills.
    op.drop_index("job_entities_cid", table_name="job_entities")
    op.drop_table("job_entities")
