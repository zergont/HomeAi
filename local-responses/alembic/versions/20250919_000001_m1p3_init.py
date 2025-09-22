# alembic/versions/20250919_000001_m1p3_init.py
"""m1p3 initial tables

Revision ID: 20250919_000001_m1p3_init
Revises: 0001_initial
Create Date: 2025-09-19

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20250919_000001_m1p3_init'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'threads',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('summary_updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'messages',
        sa.Column('id', sa.String(length=64), primary_key=True),
        sa.Column('thread_id', sa.String(length=64), sa.ForeignKey('threads.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('total_tokens', sa.Integer(), nullable=True),
    )
    op.create_index('ix_messages_thread_id', 'messages', ['thread_id'])
    op.create_index('ix_messages_created_at', 'messages', ['created_at'])

    op.create_table(
        'responses',
        sa.Column('id', sa.String(length=80), primary_key=True),
        sa.Column('thread_id', sa.String(length=64), sa.ForeignKey('threads.id', ondelete='CASCADE'), nullable=False),
        sa.Column('request_json', sa.Text(), nullable=False),
        sa.Column('response_json', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('model', sa.String(length=255), nullable=False),
        sa.Column('provider_name', sa.String(length=64), nullable=False),
        sa.Column('provider_base_url', sa.String(length=512), nullable=True),
        sa.Column('input_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('output_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost', sa.Numeric(12, 6), nullable=False, server_default='0.000000'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_responses_thread_id', 'responses', ['thread_id'])
    op.create_index('ix_responses_created_at', 'responses', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_responses_created_at', table_name='responses')
    op.drop_index('ix_responses_thread_id', table_name='responses')
    op.drop_table('responses')

    op.drop_index('ix_messages_created_at', table_name='messages')
    op.drop_index('ix_messages_thread_id', table_name='messages')
    op.drop_table('messages')

    op.drop_table('threads')
