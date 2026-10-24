# ============================================================================
# Tax codes: the arithmetic and the postings (see app/models/tax_codes.py)
#
# Each component is taxed on the document's taxable lines and rounded to the
# cent on its own, the way a receipt prints GST and PST as two amounts; the
# document's tax_amount is their sum and its tax_rate their combined rate,
# so every screen and report that reads those two columns keeps working.
#
# A sale credits each component's payable account. A purchase debits each
# recoverable component to its recoverable account and spreads the rest
# over the lines, the way all of its tax was spread before. A document with
# no code posts its tax exactly as it did before codes existed.
# ============================================================================

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tax_codes import TaxCode, TaxCodeComponent
from app.services.accounting import _q, get_sales_tax_account_id
from app.services.purchase_posting import spread

# The postings that claim tax back (and give it back): purchases.
PURCHASE_SOURCES = ("bill", "bill_void", "vendor_credit", "vendor_credit_void")

# Every table that carries tax_code_id, for "is this code in use?"
DOCUMENT_MODELS = (
    ("app.models.invoices", "Invoice"),
    ("app.models.estimates", "Estimate"),
    ("app.models.credit_memos", "CreditMemo"),
    ("app.models.recurring", "RecurringInvoice"),
    ("app.models.bills", "Bill"),
    ("app.models.vendor_credits", "VendorCredit"),
    ("app.models.purchase_orders", "PurchaseOrder"),
)


def resolve(db: Session, tax_code_id, *, for_new: bool = True) -> TaxCode | None:
    """The code a document names, or None for a document with none. A new
    document can't take an archived code; an edit keeps the one it has."""
    if tax_code_id is None:
        return None
    code = db.get(TaxCode, tax_code_id)
    if code is None:
        raise HTTPException(status_code=404, detail="Tax code not found")
    if for_new and code.is_archived:
        raise HTTPException(
            status_code=400,
            detail=f"Tax code '{code.name}' is archived; choose a current one",
        )
    return code


def effective_rate(code: TaxCode | None) -> Decimal:
    """The components' rates together: what the document's tax_rate shows."""
    if code is None:
        return Decimal("0")
    return sum((Decimal(str(c.rate or 0)) for c in code.components), Decimal("0"))


def document_rate(code: TaxCode | None, tax_rate):
    """The rate a document stores: its code's combined rate when it has a
    code, the rate it was given when it has none."""
    return effective_rate(code) if code is not None else tax_rate


def component_amounts(
    code: TaxCode, taxable_base, tax_amount=None
) -> list[tuple[TaxCodeComponent, Decimal]]:
    """Each component's tax on `taxable_base`, rounded on its own. When the
    document's stored tax_amount is given and differs from that sum (a tax
    an import brought across as it was), the stored amount is shared over
    the components by rate instead, so the postings still add up to it."""
    base = Decimal(str(taxable_base or 0))
    comps = list(code.components)
    amounts = [_q(base * Decimal(str(c.rate or 0))) for c in comps]
    if tax_amount is not None and sum(amounts, Decimal("0")) != _q(tax_amount):
        amounts = spread(tax_amount, [Decimal(str(c.rate or 0)) for c in comps])
    return list(zip(comps, amounts))


def code_for_edit(db: Session, doc, update_data: dict) -> TaxCode | None:
    """The code a document has after an edit, with update_data's tax_rate
    set to that code's rate. Sending tax_code_id changes it (null clears it,
    and the document keeps the rate it is sent or the one it had); leaving
    it out keeps the document's code, whose rate wins over a tax_rate sent
    with it, since a form sends every field back as it was shown."""
    if "tax_code_id" in update_data:
        new_id = update_data["tax_code_id"]
        code = resolve(db, new_id, for_new=new_id != doc.tax_code_id)
    else:
        code = resolve(db, doc.tax_code_id, for_new=False)
    if code is not None:
        update_data["tax_rate"] = effective_rate(code)
    return code


def sales_tax_lines(
    db: Session,
    code: TaxCode | None,
    taxable_base,
    tax_amount,
    *,
    reverse: bool = False,
    description: str = "Sales tax",
) -> list[dict]:
    """Journal lines for the tax on a sale: a credit to each component's
    payable account (a debit with `reverse`, for a credit memo giving it
    back). No code: one line to Sales Tax Payable, as always."""
    tax_amount = _q(tax_amount or 0)
    if tax_amount <= 0:
        return []

    def line(account_id, amount, desc, component_id=None):
        out = {
            "account_id": account_id,
            "debit": amount if reverse else Decimal("0"),
            "credit": Decimal("0") if reverse else amount,
            "description": desc,
        }
        if component_id is not None:
            out["tax_component_id"] = component_id
        return out

    if code is None:
        return [line(get_sales_tax_account_id(db), tax_amount, description)]
    default_payable = None
    out = []
    for comp, amount in component_amounts(code, taxable_base, tax_amount):
        if amount == 0:
            continue
        account_id = comp.payable_account_id
        if account_id is None:
            if default_payable is None:
                default_payable = get_sales_tax_account_id(db)
            account_id = default_payable
        out.append(line(account_id, amount, f"{description}: {comp.name}", comp.id))
    return out


def purchase_tax_split(
    code: TaxCode | None, taxable_base, tax_amount
) -> tuple[list[tuple[TaxCodeComponent, Decimal]], Decimal]:
    """Split a purchase's tax into (recoverable components with their
    amounts, the rest). The rest is part of what the lines cost and is
    spread over them by the caller, as all purchase tax was before."""
    tax_amount = _q(tax_amount or 0)
    if code is None or tax_amount == 0:
        return [], tax_amount
    recoverable = []
    rest = Decimal("0")
    for comp, amount in component_amounts(code, taxable_base, tax_amount):
        if comp.is_recoverable and comp.recoverable_account_id:
            if amount:
                recoverable.append((comp, amount))
        else:
            rest += amount
    return recoverable, _q(rest)


def recoverable_tax_lines(
    recoverable, *, reverse: bool = False, description: str = "Recoverable tax"
) -> list[dict]:
    """Journal lines for the recoverable tax on a purchase: a debit to each
    component's recoverable account (a credit with `reverse`, for a vendor
    credit giving it back)."""
    return [
        {
            "account_id": comp.recoverable_account_id,
            "debit": Decimal("0") if reverse else amount,
            "credit": amount if reverse else Decimal("0"),
            "description": f"{description}: {comp.name}",
            "tax_component_id": comp.id,
        }
        for comp, amount in recoverable
    ]


def in_use(db: Session, code: TaxCode) -> bool:
    """True when any document names this code: its rates and accounts are
    then fixed, so an edit of an old document never re-taxes it."""
    import importlib

    for module, name in DOCUMENT_MODELS:
        model = getattr(importlib.import_module(module), name)
        if db.query(model.id).filter(model.tax_code_id == code.id).first():
            return True
    return False


def tax_accounts(db: Session) -> list[int]:
    """The liability accounts that hold tax owed: Sales Tax Payable, every
    component's payable account, and a recoverable account that is a
    liability (tax claimed back netted against what is owed, as one
    GST/HST account does). Sales Tax Payable first; it may be missing."""
    from app.models.accounts import Account, AccountType
    from app.services import control_accounts

    ids: list[int] = []
    main = control_accounts.find(db, "2200")
    if main is not None:
        ids.append(main)
    rows = db.query(
        TaxCodeComponent.payable_account_id, TaxCodeComponent.recoverable_account_id
    ).all()
    for payable_id, recoverable_id in rows:
        for account_id in (payable_id, recoverable_id):
            if account_id is None or account_id in ids:
                continue
            acct = db.get(Account, account_id)
            if acct is not None and acct.account_type == AccountType.LIABILITY:
                ids.append(account_id)
    return ids


def component_totals(db: Session, start_date, end_date) -> list[dict]:
    """Each component's tax for a period, from the ledger: what sales charged
    less what credit memos gave back (collected), what purchases claimed back
    less what vendor credits returned (recoverable), and the two netted.
    Told apart by the posting's source, not its side or account, so one
    account can hold both (a GST/HST account with its input tax credits).
    Voids are postings too and net out on their own."""
    from sqlalchemy import func as sqlfunc

    from app.models.transactions import Transaction, TransactionLine

    rows = (
        db.query(
            TaxCodeComponent,
            Transaction.source_type.in_(PURCHASE_SOURCES),
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
            sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
        )
        .join(TransactionLine, TransactionLine.tax_component_id == TaxCodeComponent.id)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .filter(Transaction.date >= start_date, Transaction.date <= end_date)
        .group_by(TaxCodeComponent.id, Transaction.source_type.in_(PURCHASE_SOURCES))
        .all()
    )
    totals: dict[int, dict] = {}
    for comp, is_purchase, debit, credit in rows:
        row = totals.setdefault(
            comp.id,
            {
                "component_id": comp.id,
                "tax_code_id": comp.tax_code_id,
                "tax_code": comp.tax_code.name,
                "name": comp.name,
                "rate": comp.rate,
                "collected": Decimal("0"),
                "recoverable": Decimal("0"),
            },
        )
        debit, credit = Decimal(str(debit)), Decimal(str(credit))
        if is_purchase:
            row["recoverable"] += debit - credit
        else:
            row["collected"] += credit - debit
    out = []
    for row in sorted(totals.values(), key=lambda r: (r["tax_code"], r["name"])):
        row["collected"] = _q(row["collected"])
        row["recoverable"] = _q(row["recoverable"])
        row["net"] = _q(row["collected"] - row["recoverable"])
        out.append(row)
    return out
