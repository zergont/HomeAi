"""
tool_runs table for tool execution cache
"""
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.create_table(
        'tool_runs',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('thread_id', sa.String, index=True, nullable=False),
        sa.Column('attempt_id', sa.String, nullable=False),
        sa.Column('tool_name', sa.String, index=True, nullable=False),
        sa.Column('args_json', sa.Text, nullable=False),
        sa.Column('args_hash', sa.String, index=True, nullable=False),
        sa.Column('result_text', sa.Text, nullable=True),
        sa.Column('status', sa.String, nullable=False, default='done'),
        sa.Column('created_at', sa.Integer, nullable=False),
    )

def downgrade():
    op.drop_table('tool_runs')
