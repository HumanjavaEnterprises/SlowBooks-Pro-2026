"""tax codes: a document's tax as one or more components

A tax code is a named set of components, each with its own rate and payable
account, and optionally recoverable (tax paid on a purchase that is claimed
back, posted to its own account instead of into what the lines cost). The
first seam of docs/design/canada.md, built as a general feature: a state +
county + city rate on one line is the US case.

Two new tables; a nullable tax_code_id on every tax-bearing document; a
nullable tax_component_id on journal lines so the reports can say which
component a tax line posted. Existing rows take NULL: a document with no
code keeps its single rate, posted to Sales Tax Payable exactly as before.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-10-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DOCUMENTS = (
    "invoices",
    "estimates",
    "credit_memos",
    "recurring_invoices",
    "bills",
    "vendor_credits",
    "purchase_orders",
)


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "tax_codes" not in tables:
        op.create_table(
            "tax_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=100), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "is_archived", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
        op.create_index("ix_tax_codes_id", "tax_codes", ["id"])

    if "tax_code_components" not in tables:
        op.create_table(
            "tax_code_components",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "tax_code_id",
                sa.Integer(),
                sa.ForeignKey("tax_codes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=50), nullable=False),
            sa.Column(
                "rate", sa.Numeric(7, 6), nullable=False, server_default="0"
            ),
            sa.Column(
                "payable_account_id",
                sa.Integer(),
                sa.ForeignKey("accounts.id"),
                nullable=True,
            ),
            sa.Column(
                "is_recoverable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "recoverable_account_id",
                sa.Integer(),
                sa.ForeignKey("accounts.id"),
                nullable=True,
            ),
            sa.Column(
                "sort_order", sa.Integer(), nullable=False, server_default="0"
            ),
        )
        op.create_index(
            "ix_tax_code_components_id", "tax_code_components", ["id"]
        )
        op.create_index(
            "ix_tax_code_components_tax_code_id",
            "tax_code_components",
            ["tax_code_id"],
        )

    for table in DOCUMENTS:
        if table not in tables or "tax_code_id" in _columns(insp, table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("tax_code_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                f"fk_{table}_tax_code_id", "tax_codes", ["tax_code_id"], ["id"]
            )

    if "transaction_lines" in tables and "tax_component_id" not in _columns(
        insp, "transaction_lines"
    ):
        with op.batch_alter_table("transaction_lines") as batch:
            batch.add_column(
                sa.Column("tax_component_id", sa.Integer(), nullable=True)
            )
            batch.create_foreign_key(
                "fk_transaction_lines_tax_component_id",
                "tax_code_components",
                ["tax_component_id"],
                ["id"],
            )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "transaction_lines" in tables and "tax_component_id" in _columns(
        insp, "transaction_lines"
    ):
        with op.batch_alter_table("transaction_lines") as batch:
            batch.drop_constraint(
                "fk_transaction_lines_tax_component_id", type_="foreignkey"
            )
            batch.drop_column("tax_component_id")

    for table in DOCUMENTS:
        if table not in tables or "tax_code_id" not in _columns(insp, table):
            continue
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_tax_code_id", type_="foreignkey")
            batch.drop_column("tax_code_id")

    if "tax_code_components" in tables:
        op.drop_table("tax_code_components")
    if "tax_codes" in tables:
        op.drop_table("tax_codes")
