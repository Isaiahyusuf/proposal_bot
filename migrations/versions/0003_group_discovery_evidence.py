"""candidate evidence/classification and lead scan state

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

CATEGORIES = ("'confirmed_creator','confirmed_developer','possible_owner','administrator','unverified_contact'")


def upgrade() -> None:
    with op.batch_alter_table("leads") as b:
        b.add_column(sa.Column("official_links", sa.Text(), nullable=True))
        b.add_column(sa.Column("scan_status", sa.String(20), nullable=False, server_default="not_scanned"))
        b.add_column(sa.Column("scan_note", sa.Text(), nullable=True))
        b.add_column(sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("needs_manual_review", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.batch_alter_table("candidates") as b:
        b.alter_column("role", existing_type=sa.String(50), type_=sa.String(255), existing_nullable=False)
        b.add_column(sa.Column("category", sa.String(30), nullable=False, server_default="administrator"))
        b.add_column(sa.Column("evidence", sa.Text(), nullable=False, server_default=""))
        b.add_column(sa.Column("confidence", sa.String(10), nullable=False, server_default="low"))
        b.add_column(sa.Column("verification_status", sa.String(20), nullable=False, server_default="unverified"))
        b.add_column(sa.Column("source", sa.String(10), nullable=False, server_default="telegram"))
        b.drop_column("verified")  # replaced by verification_status; old rows were unverified admins
        b.create_check_constraint("ck_candidate_category", f"category IN ({CATEGORIES})")
        b.create_check_constraint("ck_candidate_confidence", "confidence IN ('high','medium','low')")


def downgrade() -> None:
    with op.batch_alter_table("candidates") as b:
        b.drop_constraint("ck_candidate_confidence", type_="check")
        b.drop_constraint("ck_candidate_category", type_="check")
        b.add_column(sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.true()))
        for col in ("source", "verification_status", "confidence", "evidence", "category"):
            b.drop_column(col)
    with op.batch_alter_table("leads") as b:
        for col in ("needs_manual_review", "scanned_at", "scan_note", "scan_status", "official_links"):
            b.drop_column(col)
