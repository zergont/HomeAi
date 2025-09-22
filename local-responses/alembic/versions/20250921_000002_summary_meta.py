from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '20250921_000002_summary_meta'
down_revision = '20250919_000001_m1p3_init'
branch_labels = None
depends_on = None

def upgrade() -> None:
    with op.batch_alter_table('threads') as b:
        b.add_column(sa.Column('summary_lang', sa.String(length=10), nullable=True))
        b.add_column(sa.Column('summary_quality', sa.String(length=10), nullable=True))
        b.add_column(sa.Column('is_summarizing', sa.Boolean(), nullable=True))
        b.add_column(sa.Column('summary_source_hash', sa.String(length=64), nullable=True))
        b.add_column(sa.Column('last_summary_run_at', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('threads') as b:
        b.drop_column('last_summary_run_at')
        b.drop_column('summary_source_hash')
        b.drop_column('is_summarizing')
        b.drop_column('summary_quality')
        b.drop_column('summary_lang')
