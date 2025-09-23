"""
profile init

Revision ID: 20250922_000003_profile_init
Revises: 20250921_000002_summary_meta
Create Date: 2025-09-22 00:00:03.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20250922_000003_profile_init'
down_revision = '20250921_000002_summary_meta'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'profile',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('display_name', sa.String(length=128), nullable=True),
        sa.Column('preferred_language', sa.String(length=32), nullable=True),
        sa.Column('tone', sa.String(length=32), nullable=True),
        sa.Column('timezone', sa.String(length=64), nullable=True),
        sa.Column('region_coarse', sa.String(length=64), nullable=True),
        sa.Column('work_hours', sa.String(length=256), nullable=True),
        sa.Column('ui_format_prefs', sa.Text(), nullable=True),
        sa.Column('goals_mood', sa.Text(), nullable=True),
        sa.Column('decisions_tasks', sa.Text(), nullable=True),
        sa.Column('brevity', sa.String(length=32), nullable=True),
        sa.Column('format_defaults', sa.Text(), nullable=True),
        sa.Column('interests_topics', sa.Text(), nullable=True),
        sa.Column('workflow_tools', sa.Text(), nullable=True),
        sa.Column('os', sa.String(length=64), nullable=True),
        sa.Column('runtime', sa.String(length=64), nullable=True),
        sa.Column('hardware_hint', sa.String(length=128), nullable=True),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column('source', sa.String(length=64), nullable=True),
        sa.Column('confidence', sa.Integer(), nullable=True),
    )
    # ensure single row id=1 exists
    op.execute("INSERT INTO profile (id) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM profile WHERE id=1)")


def downgrade() -> None:
    op.drop_table('profile')
