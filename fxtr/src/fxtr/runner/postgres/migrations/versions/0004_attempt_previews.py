"""Each step attempt's latest preview, for a viewer that shows a running step's value.

Revision ID: 0004
Revises: 0003

Adds the ``attempt_previews`` table. Nothing of the previous release reads or writes a table it
does not declare, so its tables stay usable by that release, as the package docstring requires.

Some schemas have the table and no revision: a fxtr from before migrations created every table it
declared, and for a time that included this one. Such a schema is adopted at the first revision
(the package docstring says how), so this revision meets the table there and leaves it as it is.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "attempt_previews",
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="How many previews the body had published when this one was written, from 1;"
            " it only grows, so a reader can tell a newer preview by it.",
        ),
        sa.Column(
            "data",
            sa.LargeBinary(),
            nullable=False,
            comment="The preview's canonical DAG-CBOR bytes: a tagged mapping, often the wire form"
            " of a Partial (fxtr.entity_defns.partial). It may name entities that are not stored.",
        ),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["attempts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id"),
        if_not_exists=True,
        comment="The latest preview a step attempt's body published through ctx.preview,"
        " overwritten as newer ones are written, kept after the attempt fails or is abandoned, and"
        " deleted when it completes (its result takes the preview's place). Pure observability"
        " state that participates in no rule, like attempt_logs: nothing here is read by the"
        " ledger or the policy.",
    )


def downgrade() -> None:
    # A preview is shown only while its step runs, and the step publishes the next one.
    op.drop_table("attempt_previews")
