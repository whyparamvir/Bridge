"""Assign stable public-facing numbers to agent identities."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_numbers",
        sa.Column("number", sa.Integer(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_profiles.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("agent_id"),
        sqlite_autoincrement=True,
    )
    op.execute(
        "INSERT INTO agent_numbers (agent_id) "
        "SELECT id FROM agent_profiles ORDER BY created_at, id"
    )


def downgrade() -> None:
    op.drop_table("agent_numbers")
