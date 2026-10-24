from datetime import date as dt_date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel
from app.schemas.common import StrictModel


class CCChargeCreate(StrictModel):
    date: dt_date
    payee: Optional[str] = None
    account_id: int
    # the card (a liability account; default 2100 Credit Card)
    card_account_id: Optional[int] = None
    amount: Decimal
    memo: Optional[str] = None
    reference: Optional[str] = None
    class_id: Optional[int] = None
    job_id: Optional[int] = None
    function: Optional[str] = None  # nonprofit: program | management | fundraising
    # A tax code splits the total (tax included, as a receipt prints it):
    # its recoverable components are claimed back to their own accounts.
    # tax_amount is the receipt's own tax figure, when it differs from the
    # code's rate backed out of the total by a cent.
    tax_code_id: Optional[int] = None
    tax_amount: Optional[Decimal] = None


class CCChargeResponse(BaseModel):
    id: int
    date: dt_date
    payee: str = ""
    account_name: str = ""
    amount: float
    memo: str = ""
    reference: str = ""
