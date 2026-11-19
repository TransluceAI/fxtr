"""Which job attempt facts arrived only as the producers of another job's step results.

Revision ID: 0003
Revises: 0002

Adds ``job_attempt_provenance.from_other_job``, false by default, so the previous release's inserts
(a job attempt it runs, a fact it imports) still work and get the meaning they had there. A fact
whose job this database does not hold can only have come with another job's record, so the
upgrade marks those; a fact of a job the database holds is taken to be that job's.
"""

import sqlalchemy as sa
from alembic import op

_EARLIER_COMMENT = (
    "That a job attempt existed and which code its process ran: the immutable"
    " fact about a job attempt, written with it and never changed. Results point here for"
    " their provenance. How the job attempt went (its lease, its state) is execution state, in"
    " job_attempts, which exists only for job attempts this database ran."
)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_attempt_provenance",
        sa.Column(
            "from_other_job",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
            comment="Whether this database has the fact only because another job's record named"
            " it, as the producer of a step result that job reused. Such a fact describes that"
            " result but is not one of its job's own attempts: that job's record, its import's"
            " check for attempts the record lacks, and its attempts' positions leave it out. The"
            " import of the job it names sets it false and gives the fact its record's values,"
            " which are the authority on the job's attempts.",
        ),
    )
    op.create_table_comment(
        "job_attempt_provenance",
        "That a job attempt existed and which code its process ran: the fact about a job"
        " attempt, written with it and changed only when its job's own import replaces one another"
        " job's record brought (from_other_job). Results point here for their provenance."
        " How the job attempt went (its lease, its state) is execution state, in job_attempts, which"
        " exists only for job attempts this database ran.",
        existing_comment=_EARLIER_COMMENT,
    )
    op.execute(
        "UPDATE job_attempt_provenance SET from_other_job = true"
        " WHERE job_id NOT IN (SELECT id FROM jobs)"
    )


def downgrade() -> None:
    # The earlier release keeps no record of which facts another job's record brought; it reads
    # every fact as its job's own, as it did before.
    op.create_table_comment("job_attempt_provenance", _EARLIER_COMMENT)
    op.drop_column("job_attempt_provenance", "from_other_job")
