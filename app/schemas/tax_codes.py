from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import StrictModel, TaxRate, TaxRateOut


def _name(v, what):
    v = (v or "").strip()
    if not v:
        raise ValueError(f"{what} is required")
    return v


class TaxCodeComponentIn(StrictModel):
    name: str = Field(..., max_length=50)
    rate: TaxRate
    # None = Sales Tax Payable
    payable_account_id: Optional[int] = None
    is_recoverable: bool = False
    recoverable_account_id: Optional[int] = None
    # federal | provincial | None: a customer can be exempt from one level
    level: Optional[Literal["federal", "provincial"]] = None

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v):
        return _name(v, "A component's name")


class TaxCodeCreate(StrictModel):
    name: str = Field(..., max_length=100)
    description: Optional[str] = None
    components: list[TaxCodeComponentIn] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v):
        return _name(v, "Tax code name")


class TaxCodeUpdate(StrictModel):
    name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    is_archived: Optional[bool] = None
    # The whole set, replacing the code's components: only while no
    # document uses the code (a new rate is a new code).
    components: Optional[list[TaxCodeComponentIn]] = Field(None, min_length=1)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, v):
        if v is None:
            return v
        return _name(v, "Tax code name")


class TaxCodeComponentResponse(BaseModel):
    id: int
    name: str
    rate: TaxRateOut
    payable_account_id: Optional[int] = None
    is_recoverable: bool
    recoverable_account_id: Optional[int] = None
    level: Optional[str] = None

    model_config = {"from_attributes": True}


class TaxCodeResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_archived: bool
    rate: TaxRateOut = Decimal("0")  # the components' rates together
    in_use: bool = False
    components: list[TaxCodeComponentResponse] = []

    model_config = {"from_attributes": True}


class CanadaPresetRequest(StrictModel):
    # checked against the provinces by the route, which says which it takes
    province: str = Field(..., max_length=40)
    # Nova Scotia only: also the 15% HST code for documents dated before
    # 2025-04-01, when the rate fell to 14%.
    include_before_2025_04_01: bool = False
    # Also the codes for sales shipped to other provinces (place of supply)
    include_other_provinces: bool = False
