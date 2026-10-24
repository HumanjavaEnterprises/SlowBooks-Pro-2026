"""tax levels: which parts of a tax code a customer pays

A tax-code component gets a level (federal, provincial, or none), and a
customer says whether it pays each: a reseller with a PST exemption
certificate pays GST but not PST. A sales document keeps the levels its
customer was exempt from when it was saved, as it keeps each line's tax
flag, so changing the customer later never re-taxes an old document.

Existing rows: components take no level (charged to every taxable
customer), customers pay both levels, documents record no exemption.
Nothing posts differently after the upgrade.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-10-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DOCUMENTS = ("invoices", "estimates", "credit_memos")


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "tax_code_components" in tables and "level" not in _columns(
        insp, "tax_code_components"
    ):
        op.add_column(
            "tax_code_components",
            sa.Column("level", sa.String(length=20), nullable=True),
        )

    if "customers" in tables:
        present = _columns(insp, "customers")
        for name in ("charges_federal_tax", "charges_provincial_tax"):
            if name not in present:
                op.add_column(
                    "customers",
                    sa.Column(
                        name, sa.Boolean(), nullable=False, server_default=sa.true()
                    ),
                )
        if "tax_exemption_number" not in present:
            op.add_column(
                "customers",
                sa.Column("tax_exemption_number", sa.String(length=50), nullable=True),
            )

    for table in DOCUMENTS:
        if table in tables and "exempt_tax_levels" not in _columns(insp, table):
            op.add_column(
                table,
                sa.Column("exempt_tax_levels", sa.String(length=30), nullable=True),
            )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    drops = {
        "tax_code_components": ("level",),
        "customers": (
            "charges_federal_tax",
            "charges_provincial_tax",
            "tax_exemption_number",
        ),
        **{table: ("exempt_tax_levels",) for table in DOCUMENTS},
    }
    for table, names in drops.items():
        if table not in tables:
            continue
        present = _columns(insp, table)
        gone = [n for n in names if n in present]
        if gone:
            with op.batch_alter_table(table) as batch:
                for name in gone:
                    batch.drop_column(name)
