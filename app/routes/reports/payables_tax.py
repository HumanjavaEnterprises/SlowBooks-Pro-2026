from datetime import date
from datetime import date as dt_date
from decimal import Decimal
from typing import Optional

from fastapi import Depends, HTTPException, Query, Request
from app.schemas.common import StrictModel
from sqlalchemy.orm import Session
from sqlalchemy import func as sqlfunc

from app.database import get_db
from app.services.accounting import _q
from app.models.accounts import Account
from app.models.contacts import Vendor
from app.routes.reports._router import router
from app.routes.invoices.helpers import _due_date_from_terms


class SalesTaxPaymentRequest(StrictModel):
    # `dt_date`, not `date`: inside the class body the field named `date`
    # shadows the type, so `Optional[date]` became `Optional[None]` and every
    # real date was refused with "date: Input should be None" (2.17.3).
    date: Optional[dt_date] = None
    amount: Decimal
    pay_from_account_id: int
    # The tax account paid down: Sales Tax Payable when none is named, else
    # one a tax-code component posts to (a county, a GST/HST account).
    tax_account_id: Optional[int] = None
    # Input tax credits kept in their own account (not netted on the tax
    # account) are settled by the same remittance: the tax account is paid
    # down by the tax collected, the credits come off their account, and the
    # bank pays the difference, which is `amount`.
    recoverable_account_id: Optional[int] = None
    recoverable_amount: Optional[Decimal] = None
    # A refund received (a return whose credits exceed the tax collected):
    # `amount` comes into the bank account instead of leaving it.
    refund: bool = False
    check_number: Optional[str] = ""
    reference: Optional[str] = None


@router.get("/sales-tax")
def sales_tax_report(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    db: Session = Depends(get_db),
):
    """Sales Tax report: the tax charged on sales less the tax given back on
    credit memos, in the company's currency, checked against Sales Tax
    Payable (2200).

    Credit memos were left out (2.17.3, macbase1 F16), so the report read
    higher than 2200 by the tax on every return. A document with nothing
    taxable carries no rate: an all-labour invoice is not "8.25%, $0.00".
    """
    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()

    from app.services import tax_codes

    rows = tax_codes.sales_documents(db, start_date, end_date)

    items = []
    for kind, doc, number, subtotal, taxable, tax in rows:
        items.append(
            {
                "type": kind,
                "date": doc.date.isoformat(),
                "number": number,
                "invoice_number": number if kind == "invoice" else None,
                "memo_number": number if kind == "credit_memo" else None,
                "customer_name": doc.customer.name if doc.customer else "",
                "subtotal": float(subtotal),
                "taxable": float(taxable),
                "tax_rate": float(doc.tax_rate or 0) if taxable else None,
                "tax_amount": float(tax),
            }
        )

    total_sales = sum((r[3] for r in rows), Decimal(0))
    total_taxable = sum((r[4] for r in rows), Decimal(0))
    tax_on_sales = sum((r[5] for r in rows if r[0] == "invoice"), Decimal(0))
    tax_credited = -sum((r[5] for r in rows if r[0] == "credit_memo"), Decimal(0))
    total_tax = tax_on_sales - tax_credited

    components = [
        {
            **row,
            "rate": float(row["rate"]),
            "collected": float(row["collected"]),
            "recoverable": float(row["recoverable"]),
            "net": float(row["net"]),
        }
        for row in tax_codes.component_totals(db, start_date, end_date)
    ]
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "items": items,
        "components": components,
        "total_sales": float(total_sales),
        "total_taxable": float(total_taxable),
        "total_non_taxable": float(total_sales - total_taxable),
        "tax_on_sales": float(tax_on_sales),
        "tax_credited": float(tax_credited),
        "total_tax": float(total_tax),
        "ledger": _sales_tax_ledger(db, start_date, end_date, total_tax),
    }


def _sales_tax_ledger(db: Session, start_date, end_date, total_tax: Decimal):
    """What Sales Tax Payable (2200) says for the same period: the tax posted
    to it (sales and credit memos, and anything else that touched it), the
    payments made, the balance owed at the end date, and how far the posted
    tax is from the report's total. A difference names a posting the report
    cannot see: tax on a bill, a journal entry, a void of an earlier sale.

    With tax codes, every account a component posts tax owed to is read
    with 2200 (`accounts` lists them), and tax a purchase claims back
    through a component is no part of the tax posted on sales: it is in
    `recoverable`, and nets in the balance when it shares the account."""
    from sqlalchemy import and_, or_

    from app.models.transactions import Transaction, TransactionLine
    from app.services import control_accounts, tax_codes

    tax_id = control_accounts.find(db, "2200")
    if tax_id is None:
        return None
    account = db.get(Account, tax_id)
    tax_ids = tax_codes.tax_accounts(db)

    def owed(*conds) -> Decimal:
        dr, cr = (
            db.query(
                sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
                sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
            )
            .join(Transaction, TransactionLine.transaction_id == Transaction.id)
            .filter(TransactionLine.account_id.in_(tax_ids), *conds)
            .one()
        )
        return _q(Decimal(str(cr)) - Decimal(str(dr)))

    in_period = (Transaction.date >= start_date, Transaction.date <= end_date)
    payment = Transaction.source_type == "sales_tax_payment"
    purchase_doc = Transaction.source_type.in_(tax_codes.PURCHASE_SOURCES)
    claimed = and_(purchase_doc, TransactionLine.tax_component_id.isnot(None))
    posted = owed(
        *in_period, or_(Transaction.source_type.is_(None), ~payment), ~claimed
    )
    # Tax paid to suppliers that bills and vendor credits posted here before
    # 2.18 (it is part of a purchase's cost now, never Sales Tax Payable).
    # Named so the difference it causes is explained, with the correction.
    purchase = and_(purchase_doc, TransactionLine.tax_component_id.is_(None))
    accounts = [db.get(Account, i) for i in tax_ids]
    return {
        "account_number": account.account_number,
        "account_name": account.name,
        "accounts": [
            {"id": a.id, "account_number": a.account_number, "name": a.name}
            for a in accounts
        ],
        "recoverable": float(-owed(*in_period, claimed)),
        "tax_posted": float(posted),
        "payments": float(-owed(*in_period, payment)),
        "balance": float(owed(Transaction.date <= end_date)),
        "difference": float(_q(total_tax - posted)),
        "purchase_tax": float(-owed(*in_period, purchase)),
        "purchase_tax_to_date": float(-owed(Transaction.date <= end_date, purchase)),
    }


@router.post("/sales-tax/pay")
def pay_sales_tax(data: SalesTaxPaymentRequest, db: Session = Depends(get_db)):
    """Record a sales tax payment — DR Sales Tax Payable, CR Bank Account"""
    from app.services.accounting import create_journal_entry, get_sales_tax_account_id
    from app.services.closing_date import check_closing_date

    pay_date = data.date or date.today()
    check_closing_date(db, pay_date)

    amount = _q(data.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")

    bank_account = (
        db.query(Account).filter(Account.id == data.pay_from_account_id).first()
    )
    if not bank_account:
        raise HTTPException(status_code=404, detail="Bank account not found")
    # Tax is paid out of a bank or card account. The picker used to offer
    # every asset (Accounts Receivable, Inventory, Undeposited Funds ...)
    # and the credit landed wherever the user pointed it.
    if not bank_account.bank_kind:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{bank_account.name} is not a bank or credit card account. "
                "Pick the bank or credit card account the tax was paid from."
            ),
        )

    if data.tax_account_id is None:
        tax_account_id = get_sales_tax_account_id(db)
    else:
        from app.services import tax_codes

        if data.tax_account_id not in tax_codes.tax_accounts(db):
            raise HTTPException(
                status_code=400,
                detail=(
                    "That account holds no sales tax. Pay Sales Tax Payable, "
                    "or an account a tax code's component posts its tax to."
                ),
            )
        tax_account_id = data.tax_account_id
    if not tax_account_id:
        raise HTTPException(
            status_code=400, detail="Sales Tax Payable account (2200) not found"
        )

    credits = _q(data.recoverable_amount or 0)
    if data.recoverable_account_id is not None or credits:
        from app.services import tax_codes

        if credits <= 0 or data.recoverable_account_id is None:
            raise HTTPException(
                status_code=400,
                detail="Input tax credits settled with a payment need both their "
                "account and an amount above zero",
            )
        if data.recoverable_account_id not in tax_codes.recoverable_accounts(db):
            raise HTTPException(
                status_code=400,
                detail="That account holds no tax claimed back. Name the account a "
                "tax code's recoverable component posts to.",
            )
        if data.recoverable_account_id == tax_account_id:
            credits = Decimal("0")  # netted on the tax account already

    if data.refund:
        # The bank receives the refund. Credits kept in their own account
        # come off it; the tax collected they were netted against comes off
        # the tax account; with none, the refund is the tax account's own
        # debit balance coming back.
        if credits and credits < amount:
            raise HTTPException(
                status_code=400,
                detail="A refund cannot be more than the input tax credits it "
                "comes from",
            )
        journal_lines = [
            {
                "account_id": bank_account.id,
                "debit": amount,
                "credit": Decimal("0"),
                "description": "Sales tax refund received",
            }
        ]
        if credits:
            if credits > amount:
                journal_lines.append(
                    {
                        "account_id": tax_account_id,
                        "debit": credits - amount,
                        "credit": Decimal("0"),
                        "description": "Sales tax refund received",
                    }
                )
            journal_lines.append(
                {
                    "account_id": data.recoverable_account_id,
                    "debit": Decimal("0"),
                    "credit": credits,
                    "description": "Input tax credits claimed",
                }
            )
        else:
            journal_lines.append(
                {
                    "account_id": tax_account_id,
                    "debit": Decimal("0"),
                    "credit": amount,
                    "description": "Sales tax refund received",
                }
            )
        txn = create_journal_entry(
            db,
            pay_date,
            "Sales Tax Refund",
            journal_lines,
            source_type="sales_tax_payment",
            reference=(
                data.reference if data.reference is not None else data.check_number
            ),
        )
        db.commit()
        return {"status": "ok", "transaction_id": txn.id, "amount": float(amount)}

    journal_lines = [
        {
            "account_id": tax_account_id,
            "debit": amount + credits,
            "credit": Decimal("0"),
            "description": "Sales tax payment",
        },
        {
            "account_id": bank_account.id,
            "debit": Decimal("0"),
            "credit": amount,
            "description": "Sales tax payment",
        },
    ]
    if credits:
        journal_lines.append(
            {
                "account_id": data.recoverable_account_id,
                "debit": Decimal("0"),
                "credit": credits,
                "description": "Input tax credits claimed",
            }
        )

    reference = data.reference if data.reference is not None else data.check_number

    txn = create_journal_entry(
        db,
        pay_date,
        "Sales Tax Payment",
        journal_lines,
        source_type="sales_tax_payment",
        reference=reference,
    )
    db.commit()
    return {"status": "ok", "transaction_id": txn.id, "amount": float(amount)}


def _gst_hst_return(db, start_date, end_date, line_104, line_107, line_110, line_111):
    from app.services import gst_hst_return as svc

    # A filing period is a month, a quarter or a year; with no dates, the
    # year to date, as the Sales Tax report runs.
    if not start_date:
        start_date = date(date.today().year, 1, 1)
    if not end_date:
        end_date = date.today()
    entered = {"104": line_104, "107": line_107, "110": line_110, "111": line_111}
    return svc.gst_hst_return(db, start_date, end_date, entered)


# Amounts only the person filing knows (adjustments, instalments, rebates),
# given on the address so a saved report and an export carry them.
# What a filer types is money: bounded like the schemas' amounts, so a
# number no return could hold is a 422 rather than a failed rounding.
_ENTERED = {
    n: Query(
        default=None,
        description=f"Line {n}, entered by the filer",
        ge=Decimal("-999999999999.99"),
        le=Decimal("999999999999.99"),
        max_digits=14,
        decimal_places=2,
        allow_inf_nan=False,
    )
    for n in ("104", "107", "110", "111")
}


@router.get("/gst-hst-return")
def gst_hst_return(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    line_104: Optional[Decimal] = _ENTERED["104"],
    line_107: Optional[Decimal] = _ENTERED["107"],
    line_110: Optional[Decimal] = _ENTERED["110"],
    line_111: Optional[Decimal] = _ENTERED["111"],
    db: Session = Depends(get_db),
):
    """The GST/HST return (GST34) for a Canadian company: its lines from the
    ledger's GST and HST components and the period's sales, the QST summary
    for a Quebec registrant, and the reconciliation against the GST/HST
    accounts (app/services/gst_hst_return.py)."""
    return _gst_hst_return(
        db, start_date, end_date, line_104, line_107, line_110, line_111
    )


def _gst_hst_section(data: dict) -> dict:
    from app.routes.reports.financial import _money

    totals = ("105", "108", "109", "113A")
    rows = [
        {
            "cells": [
                f"{line['line'] or ''}  {line['label']}".strip(),
                _money(line["amount"]),
            ],
            "style": "subtotal" if line["key"] in totals else "",
        }
        for line in data["lines"]
    ]
    qst = data.get("qst")
    if qst:
        rows += [
            {
                "cells": ["QST, for Revenu Québec's return (FPZ-500)", ""],
                "style": "subtotal",
            },
            {"cells": ["QST collected", _money(qst["collected"])]},
            {"cells": ["QST input tax refunds", _money(qst["input_tax_refunds"])]},
            {"cells": ["Net QST", _money(qst["net"])], "style": "subtotal"},
        ]
    rows += [{"cells": [note, ""], "style": "note"} for note in data.get("notes", [])]
    return {
        "title": "GST/HST Return",
        "period": f"{data['start_date']} to {data['end_date']}",
        "columns": ["Line", "Amount"],
        "rows": rows,
    }


@router.get("/gst-hst-return/pdf")
def gst_hst_return_pdf(
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    line_104: Optional[Decimal] = _ENTERED["104"],
    line_107: Optional[Decimal] = _ENTERED["107"],
    line_110: Optional[Decimal] = _ENTERED["110"],
    line_111: Optional[Decimal] = _ENTERED["111"],
    db: Session = Depends(get_db),
):
    from app.routes.reports.financial import _pdf_response

    data = _gst_hst_return(
        db, start_date, end_date, line_104, line_107, line_110, line_111
    )
    return _pdf_response(
        [_gst_hst_section(data)],
        db,
        f"gst-hst-return_{data['start_date']}_{data['end_date']}.pdf",
    )


@router.get("/gst-hst-return/csv")
def gst_hst_return_csv(
    request: Request,
    start_date: date = Query(default=None),
    end_date: date = Query(default=None),
    line_104: Optional[Decimal] = _ENTERED["104"],
    line_107: Optional[Decimal] = _ENTERED["107"],
    line_110: Optional[Decimal] = _ENTERED["110"],
    line_111: Optional[Decimal] = _ENTERED["111"],
    db: Session = Depends(get_db),
):
    from app.routes.reports.financial import _company_name, _csv_download
    from app.services.gst_hst_return import gst_hst_return_csv as to_csv

    data = _gst_hst_return(
        db, start_date, end_date, line_104, line_107, line_110, line_111
    )
    return _csv_download(
        to_csv(data, _company_name(db)),
        f"gst-hst-return_{data['start_date']}_{data['end_date']}.csv",
        request,
    )


def ap_aging_report(db: Session, as_of_date: date) -> dict:
    """The A/P Aging report's figures, in home currency, netted of the credits
    a vendor holds for us (unapplied vendor credits and the part of a bill
    payment not yet applied to a bill), so the total equals account 2000 and
    each vendor's row equals the vendor balance. The analytics page and the
    assistant read these too, so no two views of payables disagree (found
    integrating the 2.17.3 exploratory fixes: the A/R side had been made to
    tie; this side still summed raw document-currency balances)."""
    from app.models.bills import (
        Bill,
        BillPayment,
        BillPaymentAllocation,
        BillStatus,
    )
    from app.models.vendor_credits import VendorCredit, VendorCreditStatus
    from app.services.contact_balances import home_amount

    vendor_names = {v.id: v.name for v in db.query(Vendor.id, Vendor.name).all()}
    aging: dict = {}

    def row(vid):
        if vid not in aging:
            aging[vid] = {
                "vendor_name": vendor_names.get(vid, "Unknown"),
                "vendor_id": vid,
                "current": Decimal(0),
                "over_30": Decimal(0),
                "over_60": Decimal(0),
                "over_90": Decimal(0),
                "total": Decimal(0),
                "unapplied_credits": Decimal(0),
            }
        return aging[vid]

    # Today's open bills dated on or before the report's date, aged against
    # it, as the A/R side does (#285: a bill dated after the date was in
    # the report). A bill paid since the date is not rebuilt as owed on it:
    # this is the current ledger aged, not a reconstruction.
    bills = (
        db.query(Bill)
        .filter(Bill.status.in_([BillStatus.UNPAID, BillStatus.PARTIAL]))
        .filter(Bill.balance_due > 0)
        .filter(Bill.date <= as_of_date)
        .all()
    )
    for bill in bills:
        r = row(bill.vendor_id)
        # A bill with no due date (bills made from a PO before 2.18
        # never got one) ages from its date and terms, not as current.
        due = bill.due_date or _due_date_from_terms(bill.date, bill.terms)
        days = (as_of_date - due).days
        bal = home_amount(bill.balance_due, bill.exchange_rate)
        if days <= 0:
            r["current"] += bal
        elif days <= 30:
            r["over_30"] += bal
        elif days <= 60:
            r["over_60"] += bal
        else:
            r["over_90"] += bal
        r["total"] += bal

    def credit(vid, amount):
        # A credit has no due date, so it reduces the newest bucket — it is
        # money available now, not money aged — and shows on its own line,
        # because "you owe 700" and "you owe 1,000 and hold a 300 credit" are
        # different facts to a person about to pay a vendor (issue #129).
        r = row(vid)
        r["unapplied_credits"] += amount
        r["current"] -= amount
        r["total"] -= amount

    for vc in (
        db.query(VendorCredit)
        .filter(VendorCredit.status != VendorCreditStatus.VOID)
        .filter(VendorCredit.date <= as_of_date)
        .filter(VendorCredit.balance_remaining > 0)
        .all()
    ):
        credit(vc.vendor_id, Decimal(str(vc.balance_remaining)))

    applied = dict(
        db.query(
            BillPaymentAllocation.bill_payment_id,
            sqlfunc.coalesce(sqlfunc.sum(BillPaymentAllocation.amount), 0),
        )
        .group_by(BillPaymentAllocation.bill_payment_id)
        .all()
    )
    for bp in (
        db.query(BillPayment)
        .filter(BillPayment.date <= as_of_date)
        .filter(BillPayment.is_voided.isnot(True))
        .all()
    ):
        left = _q(Decimal(str(bp.amount or 0)) - Decimal(str(applied.get(bp.id, 0))))
        if left > 0:
            credit(bp.vendor_id, home_amount(left, bp.exchange_rate))

    _COLS = ("current", "over_30", "over_60", "over_90", "total", "unapplied_credits")
    items = sorted(aging.values(), key=lambda i: (i["vendor_name"] or "").lower())
    totals = {"vendor_name": "TOTAL", "vendor_id": 0}
    for k in _COLS:
        totals[k] = float(sum((i[k] for i in items), Decimal(0)))
    for item in items:
        for k in _COLS:
            item[k] = float(item[k])
    return {"as_of_date": as_of_date.isoformat(), "items": items, "totals": totals}


@router.get("/ap-aging")
def ap_aging(as_of_date: date = Query(default=None), db: Session = Depends(get_db)):
    """AP Aging report — mirrors AR aging but for bills."""
    return ap_aging_report(db, as_of_date or date.today())


@router.get("/1099-summary")
def report_1099_summary(
    year: int = Query(default=None),
    db: Session = Depends(get_db),
):
    """1099 Summary: total payments to 1099 vendors for a year."""
    if not year:
        year = date.today().year

    from app.models.bills import BillPayment, BillPaymentAllocation

    from app.services.form_1099 import is_1099_vendor

    # The same rule as the 1099-NEC / 1096, so a vendor on this summary is
    # on those forms too (NEC type).
    vendors_1099 = db.query(Vendor).filter(is_1099_vendor()).all()
    if not vendors_1099:
        return {"year": year, "items": [], "total": 0, "vendors_above_threshold": 0}

    # Single query: sum bill payment allocations grouped by vendor for the year.
    # Replaces a query-per-vendor loop that made this O(N) in DB round-trips.
    totals_by_vendor = dict(
        db.query(
            BillPayment.vendor_id,
            sqlfunc.coalesce(sqlfunc.sum(BillPaymentAllocation.amount), 0),
        )
        .join(
            BillPaymentAllocation,
            BillPaymentAllocation.bill_payment_id == BillPayment.id,
        )
        .filter(sqlfunc.extract("year", BillPayment.date) == year)
        # a voided payment keeps its allocation rows (the void restores the
        # bills' balances and flags the payment); it is not money paid (#287)
        .filter(BillPayment.is_voided.isnot(True))
        .group_by(BillPayment.vendor_id)
        .all()
    )

    items = []
    total = Decimal(0)
    above_threshold = 0
    missing_w9 = 0  # reportable, and no W-9 on file (#257)

    for vendor in vendors_1099:
        vendor_total = Decimal(str(totals_by_vendor.get(vendor.id, 0) or 0))
        total += vendor_total
        flagged = vendor_total >= 600
        if flagged:
            above_threshold += 1
            if not vendor.w9_on_file:
                missing_w9 += 1

        items.append(
            {
                "vendor_id": vendor.id,
                "vendor_name": vendor.name,
                "tax_id": vendor.tax_id or "",
                "vendor_1099_type": vendor.vendor_1099_type or "NEC",
                "total_paid": float(vendor_total),
                "above_threshold": flagged,
                "w9_on_file": bool(vendor.w9_on_file),
            }
        )

    items.sort(key=lambda x: x["total_paid"], reverse=True)

    return {
        "year": year,
        "items": items,
        "total": float(total),
        "vendors_above_threshold": above_threshold,
        "vendors_missing_w9": missing_w9,
        "threshold": 600.0,
    }
