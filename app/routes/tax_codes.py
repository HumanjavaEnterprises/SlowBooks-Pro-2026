# ============================================================================
# Tax codes: CRUD (see app/models/tax_codes.py)
#
# A code's name and description can always change. Its components (rates
# and accounts) can change only while no document uses it, so an edit of
# an old document never re-taxes it at a rate that came later: a new rate
# is a new code, and the old one is archived (hidden from the forms, still
# on the documents that used it). A code in use is never deleted.
# ============================================================================

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.accounts import Account, AccountType
from app.models.tax_codes import TaxCode, TaxCodeComponent
from app.schemas.tax_codes import (
    CanadaPresetRequest,
    TaxCodeComponentIn,
    TaxCodeCreate,
    TaxCodeResponse,
    TaxCodeUpdate,
)
from app.services import canada_tax, tax_codes
from app.services.settings_service import is_canadian

router = APIRouter(prefix="/api/tax-codes", tags=["tax_codes"])

# Tax collected is owed; tax claimed back is owed back (an asset) or nets
# against what is owed (the same liability).
_PAYABLE_TYPES = (AccountType.LIABILITY,)
_RECOVERABLE_TYPES = (AccountType.ASSET, AccountType.LIABILITY)


def _code_or_404(db: Session, code_id: int) -> TaxCode:
    code = db.get(TaxCode, code_id)
    if code is None:
        raise HTTPException(status_code=404, detail="Tax code not found")
    return code


def _response(db: Session, code: TaxCode) -> TaxCodeResponse:
    resp = TaxCodeResponse.model_validate(code)
    resp.rate = tax_codes.effective_rate(code)
    resp.in_use = tax_codes.in_use(db, code)
    return resp


def _account(db: Session, account_id, types, what: str, component: str) -> None:
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(
            status_code=404, detail=f"{component}: {what} account not found"
        )
    if acct.account_type not in types:
        kinds = " or ".join(t.value for t in types)
        raise HTTPException(
            status_code=400,
            detail=(
                f"{component}: the {what} account must be a {kinds} account; "
                f"'{acct.name}' is {acct.account_type.value}"
            ),
        )


def _components(db: Session, components) -> list[TaxCodeComponent]:
    names = [c.name.lower() for c in components]
    if len(set(names)) != len(names):
        raise HTTPException(
            status_code=400, detail="Each component of a tax code needs its own name"
        )
    out = []
    for i, c in enumerate(components):
        if c.payable_account_id is not None:
            _account(db, c.payable_account_id, _PAYABLE_TYPES, "payable", c.name)
        if c.is_recoverable:
            if c.recoverable_account_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{c.name}: a recoverable component needs the account "
                        "the tax claimed back is posted to"
                    ),
                )
            _account(
                db,
                c.recoverable_account_id,
                _RECOVERABLE_TYPES,
                "recoverable",
                c.name,
            )
        out.append(
            TaxCodeComponent(
                name=c.name,
                rate=c.rate,
                payable_account_id=c.payable_account_id,
                is_recoverable=c.is_recoverable,
                recoverable_account_id=(
                    c.recoverable_account_id if c.is_recoverable else None
                ),
                level=c.level,
                sort_order=i,
            )
        )
    return out


def _refuse_duplicate_name(db: Session, name: str, code_id=None) -> None:
    q = db.query(TaxCode).filter(TaxCode.name == name)
    if code_id is not None:
        q = q.filter(TaxCode.id != code_id)
    if q.first():
        raise HTTPException(
            status_code=409, detail=f"A tax code named '{name}' already exists"
        )


def _account_row(acct: Account) -> dict:
    return {"id": acct.id, "account_number": acct.account_number, "name": acct.name}


@router.post("/presets/canada")
def create_canada_preset(data: CanadaPresetRequest, db: Session = Depends(get_db)):
    """The standard sales tax codes of a Canadian province, and the GST/HST,
    QST and PST/RST payable accounts they post to, from the rate table in
    app/services/canada_tax.py. Safe to run again: a code or account that is
    there already is left as it is and listed under `existing`."""
    if not is_canadian(db):
        raise HTTPException(
            status_code=400,
            detail=(
                "Canadian sales tax codes are for a company whose tax country "
                "is Canada. Choose Canada under Settings, Company Information, "
                "first."
            ),
        )
    province = canada_tax.PROVINCES.get(data.province.strip().upper())
    if province is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{data.province}' is not a province or territory code. Use "
                f"one of: {', '.join(canada_tax.PROVINCES)}."
            ),
        )
    before = data.include_before_2025_04_01 and province.code == "NS"

    created = {"accounts": [], "tax_codes": []}
    existing = {"accounts": [], "tax_codes": []}
    account_ids = {}
    for kind, (number, name) in canada_tax.account_plan(province).items():
        acct, made = canada_tax.tax_account(db, number, name)
        account_ids[kind] = acct.id
        (created if made else existing)["accounts"].append(_account_row(acct))

    notes = canada_tax.notes(province, before_2025_04_01=before)
    new_codes = []
    for plan in canada_tax.code_plans(province, before_2025_04_01=before):
        found = db.query(TaxCode).filter(TaxCode.name == plan.name).first()
        if found is not None:
            existing["tax_codes"].append(_response(db, found))
            # Left as it is either way: it may be on documents already.
            have = tax_codes.effective_rate(found)
            want = sum((c.rate for c in plan.components), Decimal("0"))
            if have != want:
                notes.append(
                    f"'{found.name}' was already here at {canada_tax.percent(have)}, "
                    f"not {canada_tax.percent(want)}, and was left as it is."
                )
            continue
        components = []
        for c in plan.components:
            account_id = account_ids[c.kind]
            # GST/HST and QST are claimed back on purchases, each into its
            # own account; PST/RST never is.
            recoverable = plan.recoverable and c.kind != canada_tax.PST
            components.append(
                TaxCodeComponentIn(
                    name=c.name,
                    rate=c.rate,
                    payable_account_id=account_id,
                    is_recoverable=recoverable,
                    recoverable_account_id=account_id if recoverable else None,
                    level=canada_tax.level_of(c.kind),
                )
            )
        code = TaxCode(name=plan.name, description=plan.description)
        code.components = _components(db, components)
        db.add(code)
        db.flush()
        new_codes.append(code)
    # New documents start on the province's own code, so a sale is never
    # taxed on a typed rate the GST/HST return cannot see. One already
    # chosen is left as it is.
    from app.services.settings_service import get_setting_raw, set_setting

    if not (get_setting_raw(db, "default_tax_code_id") or "").strip():
        first = canada_tax.code_plans(province, before_2025_04_01=before)[0]
        main = db.query(TaxCode).filter(TaxCode.name == first.name).first()
        if main is not None:
            set_setting(db, "default_tax_code_id", str(main.id))
            notes.append(f"New documents now start on '{main.name}'.")
    db.commit()
    created["tax_codes"] = [_response(db, c) for c in new_codes]
    return {
        "province": province.code,
        "province_name": province.name,
        "rates_verified": canada_tax.RATES_VERIFIED,
        "created": created,
        "existing": existing,
        "notes": notes,
    }


@router.get("", response_model=list[TaxCodeResponse])
def list_tax_codes(include_archived: bool = False, db: Session = Depends(get_db)):
    q = db.query(TaxCode)
    if not include_archived:
        q = q.filter(TaxCode.is_archived.is_(False))
    return [_response(db, c) for c in q.order_by(TaxCode.name).all()]


@router.get("/{code_id}", response_model=TaxCodeResponse)
def get_tax_code(code_id: int, db: Session = Depends(get_db)):
    return _response(db, _code_or_404(db, code_id))


@router.post("", response_model=TaxCodeResponse, status_code=201)
def create_tax_code(data: TaxCodeCreate, db: Session = Depends(get_db)):
    _refuse_duplicate_name(db, data.name)
    code = TaxCode(name=data.name, description=data.description)
    code.components = _components(db, data.components)
    db.add(code)
    db.commit()
    db.refresh(code)
    return _response(db, code)


@router.put("/{code_id}", response_model=TaxCodeResponse)
def update_tax_code(code_id: int, data: TaxCodeUpdate, db: Session = Depends(get_db)):
    code = _code_or_404(db, code_id)
    if data.name is not None and data.name != code.name:
        _refuse_duplicate_name(db, data.name, code.id)
        code.name = data.name
    if "description" in data.model_fields_set:
        code.description = data.description
    if data.is_archived is not None:
        code.is_archived = data.is_archived
    if data.components is not None:
        if tax_codes.in_use(db, code):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Tax code '{code.name}' is on documents already, so its "
                    "rates and accounts stay as they are. For a new rate, add a "
                    "new tax code and archive this one."
                ),
            )
        code.components = _components(db, data.components)
    db.commit()
    db.refresh(code)
    return _response(db, code)


@router.delete("/{code_id}", status_code=204)
def delete_tax_code(code_id: int, db: Session = Depends(get_db)):
    code = _code_or_404(db, code_id)
    if tax_codes.in_use(db, code):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Tax code '{code.name}' is on documents, so it can't be "
                "deleted. Archive it to take it off the forms."
            ),
        )
    db.delete(code)
    db.commit()
