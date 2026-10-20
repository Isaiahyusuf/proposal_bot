"""telegram health, suppression sources, cleanup log

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 13:34:36.393856

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_STATUSES = "'draft','approved','sending','sent','failed','skipped'"
NEW_STATUSES = ("'draft','approved','held','sending','sent','failed','delivery_failed',"
                "'possible_block','skipped'")


def _swap_status_check(statuses: str) -> None:
    """Replace the unnamed status CHECK from 0001 with the named ck_outreach_status."""
    cond = f"status IN ({statuses})"
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE outreach_records DROP CONSTRAINT IF EXISTS outreach_records_status_check")
        op.execute("ALTER TABLE outreach_records DROP CONSTRAINT IF EXISTS ck_outreach_status")
        op.create_check_constraint("ck_outreach_status", "outreach_records", cond)
    else:  # SQLite rebuilds the table; the unnamed check is not reflected, so it is dropped
        with op.batch_alter_table("outreach_records") as batch:
            batch.create_check_constraint("ck_outreach_status", cond)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('cleanup_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('outreach_id', sa.Integer(), nullable=False),
    sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
    sa.Column('revoke', sa.Boolean(), nullable=False),
    sa.Column('result', sa.String(length=20), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['outreach_id', 'user_id'], ['outreach_records.id', 'outreach_records.user_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_cleanup_actions_user_id'), 'cleanup_actions', ['user_id'], unique=False)
    op.add_column('do_not_contact', sa.Column('source', sa.String(length=20), server_default='manual', nullable=False))
    op.add_column('outreach_records', sa.Column('failure_kind', sa.String(length=30), nullable=True))
    op.add_column('outreach_records', sa.Column('telegram_message_id', sa.BigInteger(), nullable=True))
    op.add_column('outreach_records', sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True))
    _swap_status_check(NEW_STATUSES)
    with op.batch_alter_table('outreach_records') as batch:
        batch.create_unique_constraint('uq_outreach_id_user', ['id', 'user_id'])
    op.add_column('telegram_accounts', sa.Column('health_state', sa.String(length=20), server_default='unknown', nullable=False))
    op.add_column('telegram_accounts', sa.Column('health_detail', sa.Text(), nullable=True))
    op.add_column('telegram_accounts', sa.Column('health_source', sa.String(length=20), nullable=True))
    op.add_column('telegram_accounts', sa.Column('health_checked_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('telegram_accounts', sa.Column('last_spambot_check_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('telegram_accounts', sa.Column('restricted_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('telegram_accounts', 'restricted_until')
    op.drop_column('telegram_accounts', 'last_spambot_check_at')
    op.drop_column('telegram_accounts', 'health_checked_at')
    op.drop_column('telegram_accounts', 'health_source')
    op.drop_column('telegram_accounts', 'health_detail')
    op.drop_column('telegram_accounts', 'health_state')
    op.drop_table('cleanup_actions')
    with op.batch_alter_table('outreach_records') as batch:
        batch.drop_constraint('uq_outreach_id_user', type_='unique')
    op.execute("UPDATE outreach_records SET status='failed' WHERE status IN ('held','delivery_failed','possible_block')")
    _swap_status_check(OLD_STATUSES)
    op.drop_column('outreach_records', 'deleted_at')
    op.drop_column('outreach_records', 'telegram_message_id')
    op.drop_column('outreach_records', 'failure_kind')
    op.drop_column('do_not_contact', 'source')
