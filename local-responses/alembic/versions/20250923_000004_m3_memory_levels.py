"""
m3 memory levels

Revision ID: 20250923_000004
Revises: 20250922_000003_profile_init
Create Date: 2025-09-23 00:00:04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20250923_000004'
down_revision = '20250922_000003_profile_init'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        'memory_state',
        sa.Column('thread_id', sa.String(length=64), sa.ForeignKey('threads.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('last_compacted_message_id', sa.String(length=64), nullable=True),
        sa.Column('l1_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('l2_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('l3_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.Integer(), nullable=False),
    )
    op.create_table(
        'l2_summaries',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('thread_id', sa.String(length=64), sa.ForeignKey('threads.id', ondelete='CASCADE'), index=True, nullable=False),
        sa.Column('start_message_id', sa.String(length=64), nullable=False),
        sa.Column('end_message_id', sa.String(length=64), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Integer(), nullable=False),
    )
    op.create_index('ix_l2_summaries_thread_id', 'l2_summaries', ['thread_id'])

    op.create_table(
        'l3_microsummaries',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('thread_id', sa.String(length=64), sa.ForeignKey('threads.id', ondelete='CASCADE'), index=True, nullable=False),
        sa.Column('start_l2_id', sa.Integer(), nullable=False),
        sa.Column('end_l2_id', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('tokens', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Integer(), nullable=False),
    )
    op.create_index('ix_l3_microsummaries_thread_id', 'l3_microsummaries', ['thread_id'])

def downgrade() -> None:
    op.drop_index('ix_l3_microsummaries_thread_id', table_name='l3_microsummaries')
    op.drop_table('l3_microsummaries')
    op.drop_index('ix_l2_summaries_thread_id', table_name='l2_summaries')
    op.drop_table('l2_summaries')
    op.drop_table('memory_state')
