"""team agents: limited-use workspace invites, the local installation row, nullable users.email,
agent seats and file claims

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
import bridge_control.db.tables

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('workspace_invites',
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('expires_at', bridge_control.db.tables.UTCDateTime(timezone=True), nullable=False),
    # Limited-use links; a set project_id also grants editor on that project.
    sa.Column('project_id', sa.Uuid(), nullable=True),
    sa.Column('max_uses', sa.Integer(), server_default='10', nullable=False),
    sa.Column('uses', sa.Integer(), server_default='0', nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_workspace_invites_created_by_users')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_workspace_invites_project_id_projects')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_workspace_invites_workspace_id_workspaces')),
    sa.PrimaryKeyConstraint('token_hash', name=op.f('pk_workspace_invites'))
    )
    # The local install owner has no email. SQLite recreates the table (batch mode).
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('email', existing_type=sa.String(length=320), nullable=True)
    op.create_table('installation',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('feed_conversation_id', sa.Uuid(), nullable=False),
    sa.Column('created_at', bridge_control.db.tables.UTCDateTime(timezone=True), nullable=False),
    sa.CheckConstraint('id = 1', name=op.f('ck_installation_single_row')),
    sa.ForeignKeyConstraint(['feed_conversation_id'], ['conversations.id'], name=op.f('fk_installation_feed_conversation_id_conversations')),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_installation_owner_user_id_users')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_installation_project_id_projects')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_installation_workspace_id_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_installation'))
    )
    op.create_table('agent_seats',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('seat_key', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('status_text', sa.String(length=200), nullable=True),
    sa.Column('task_id', sa.Uuid(), nullable=True),
    sa.Column('last_seen_at', bridge_control.db.tables.UTCDateTime(timezone=True), nullable=False),
    sa.Column('created_at', bridge_control.db.tables.UTCDateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agent_profiles.id'], name=op.f('fk_agent_seats_agent_id_agent_profiles')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_agent_seats_project_id_projects')),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name=op.f('fk_agent_seats_task_id_tasks')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_seats')),
    sa.UniqueConstraint('agent_id', 'seat_key', name='uq_agent_seats_agent_key')
    )
    op.create_table('file_claims',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('project_id', sa.Uuid(), nullable=False),
    sa.Column('agent_id', sa.Uuid(), nullable=False),
    sa.Column('seat_key', sa.String(length=64), nullable=False),
    sa.Column('path_key', sa.String(length=300), nullable=False),
    sa.Column('path', sa.String(length=400), nullable=False),
    sa.Column('intent', sa.String(length=200), nullable=False),
    sa.Column('created_at', bridge_control.db.tables.UTCDateTime(timezone=True), nullable=False),
    sa.Column('expires_at', bridge_control.db.tables.UTCDateTime(timezone=True), nullable=False),
    sa.Column('released_at', bridge_control.db.tables.UTCDateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['agent_id'], ['agent_profiles.id'], name=op.f('fk_file_claims_agent_id_agent_profiles')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_file_claims_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_file_claims'))
    )
    op.create_index('ix_file_claims_project_released', 'file_claims', ['project_id', 'released_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_file_claims_project_released', table_name='file_claims')
    op.drop_table('file_claims')
    op.drop_table('agent_seats')
    op.drop_table('installation')
    # Fails if email-less users exist (e.g. a local install owner): drop those first.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('email', existing_type=sa.String(length=320), nullable=False)
    op.drop_table('workspace_invites')
