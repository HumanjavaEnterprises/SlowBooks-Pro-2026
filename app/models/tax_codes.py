# ============================================================================
# Tax codes: a document's tax as one or more components
#
# A document used to carry one rate, posted to Sales Tax Payable (2200). A
# tax code is a named set of components, each with its own rate and its own
# payable account: a state + county + city rate on one line, each owed to a
# different authority. A component can be recoverable (tax paid on a
# purchase that is claimed back against tax collected), and then the
# purchase posts it to its recoverable account instead of into what the
# lines cost (docs/design/canada.md, the first seam).
#
# A document with no tax code keeps the single rate it always had, posted
# the way it always was. A code's rates and accounts are fixed once a
# document uses it, so editing an old document never re-taxes it at a rate
# that came later: a new rate is a new code, and the old one is archived.
# ============================================================================

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import relationship

from app.database import Base


class TaxCode(Base):
    __tablename__ = "tax_codes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    is_archived = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    components = relationship(
        "TaxCodeComponent",
        back_populates="tax_code",
        cascade="all, delete-orphan",
        order_by="TaxCodeComponent.sort_order, TaxCodeComponent.id",
    )


class TaxCodeComponent(Base):
    __tablename__ = "tax_code_components"

    id = Column(Integer, primary_key=True, index=True)
    tax_code_id = Column(
        Integer,
        ForeignKey("tax_codes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(50), nullable=False)  # "State", "County", "GST"
    rate = Column(Numeric(7, 6), nullable=False, default=0)  # a fraction
    # Where the tax collected on a sale is owed. None = Sales Tax Payable.
    payable_account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    # Tax paid on a purchase that is claimed back, not part of the cost.
    is_recoverable = Column(Boolean, default=False, nullable=False)
    recoverable_account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    sort_order = Column(Integer, default=0, nullable=False)

    tax_code = relationship("TaxCode", back_populates="components")
    payable_account = relationship("Account", foreign_keys=[payable_account_id])
    recoverable_account = relationship("Account", foreign_keys=[recoverable_account_id])
