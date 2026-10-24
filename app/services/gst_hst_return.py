# ============================================================================
# The GST/HST return (GST34) and the QST summary, for Canadian companies
#
# The third seam of docs/design/canada.md. The return is read off what the
# tax-code engine already posts: each GST or HST component's tax collected
# and claimed back comes from the ledger (tax_codes.component_totals), so a
# void, a credit memo or a vendor credit nets the way it posted, and the
# revenue on line 101 comes from the same sales documents the Sales Tax
# report lists. What the books cannot know (adjustments, instalments,
# rebates) is entered by the person filing and is 0 until they do.
#
# The Report Center offers the return only when the company's tax_country
# is CA. The API itself answers for any company, as the nonprofit reports'
# do, and nothing here changes what any other report reads.
#
# Quebec: QST is not on the GST34. It is summed beside the return, without
# line numbers, because Revenu Quebec's own return (FPZ-500) could not be
# read to confirm them; printing a line number nobody checked would send a
# filer to the wrong box.
# ============================================================================

from __future__ import annotations

import io
from decimal import Decimal

from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from app.services.accounting import _q

# What the person filing enters, by the line it goes on.
ENTERED_LINES = ("104", "107", "110", "111")
# The GST34 lines whose numbers Revenu Quebec's return is not known to share.
_NOT_ON_FPZ500 = ("112", "113A", "114", "115")


def is_gst_hst(component_name) -> bool:
    """True for a component that belongs on the GST/HST return: one named
    GST or HST, ignoring case and surrounding spaces. The name is the only
    thing a component carries that says which tax it is, and those two are
    the names the CRA uses; a provincial tax (PST, RST, QST) is filed with
    its own province, never on the GST34."""
    return (component_name or "").strip().upper() in ("GST", "HST")


def is_qst(component_name) -> bool:
    """True for Quebec's sales tax, summed beside the return (same rule)."""
    return (component_name or "").strip().upper() == "QST"


def files_with_revenu_quebec(db: Session) -> bool:
    """A business in Quebec files its GST/HST with Revenu Quebec, on a form
    (FPZ-500) whose lines 114 and 115 mean something other than the GST34's
    refund and amount owing. Only its lines 101 to 111 are known to match
    the GST34's, so the lines after them are shown without numbers. The
    province chosen with the company's Canadian tax setup says where it is;
    a company that has not chosen one falls back on its address."""
    from app.services.settings_service import get_setting_raw

    province = (get_setting_raw(db, "tax_province") or "").strip().upper()
    if province:
        return province == "QC"
    state = (get_setting_raw(db, "company_state") or "").strip().upper()
    return state in ("QC", "QUEBEC", "QUÉBEC")


_LABELS = {
    "101": "Sales and other revenue",
    "103": "GST/HST collected or collectible",
    "104": "Adjustments",
    "105": "Total GST/HST and adjustments for the period",
    "106": "Input tax credits (ITCs) for the current period",
    "107": "Adjustments",
    "108": "Total ITCs and adjustments",
    "109": "Net tax",
    "110": "Instalments and other annual filer payments",
    "111": "Rebates",
    "112": "Total other credits",
    "113A": "Balance",
    "114": "Refund claimed",
    "115": "Amount owing",
}


def _entered(entered: dict | None, line: str) -> Decimal:
    value = (entered or {}).get(line)
    return _q(Decimal(str(value))) if value not in (None, "") else Decimal("0.00")


def gst_hst_return(db: Session, start_date, end_date, entered=None) -> dict:
    """The GST34's lines for a period, the GST/HST components behind them,
    the QST summary, and the reconciliation against the GST/HST accounts.
    `entered` maps a line in ENTERED_LINES to the amount the filer gives."""
    from app.services import tax_codes

    totals = tax_codes.component_totals(db, start_date, end_date)
    gst = [row for row in totals if is_gst_hst(row["name"])]
    qst = [row for row in totals if is_qst(row["name"])]

    revenue = sum(
        (row[3] for row in tax_codes.sales_documents(db, start_date, end_date)),
        Decimal("0"),
    )
    amounts = {
        "101": _q(revenue),
        "103": _q(sum((r["collected"] for r in gst), Decimal("0"))),
        "106": _q(sum((r["recoverable"] for r in gst), Decimal("0"))),
    }
    for line in ENTERED_LINES:
        amounts[line] = _entered(entered, line)
    amounts["105"] = amounts["103"] + amounts["104"]
    amounts["108"] = amounts["106"] + amounts["107"]
    amounts["109"] = amounts["105"] - amounts["108"]
    amounts["112"] = amounts["110"] + amounts["111"]
    amounts["113A"] = amounts["109"] - amounts["112"]
    # Lines 113B and 113C add self-assessed tax (lines 205 and 405), which
    # the books do not track; with none, 113C is 113A.
    amounts["114"] = -amounts["113A"] if amounts["113A"] < 0 else Decimal("0.00")
    amounts["115"] = amounts["113A"] if amounts["113A"] > 0 else Decimal("0.00")

    quebec = files_with_revenu_quebec(db)
    lines = []
    for key in _LABELS:
        number = None if quebec and key in _NOT_ON_FPZ500 else key
        lines.append(
            {
                "key": key,
                "line": number,
                "label": _LABELS[key],
                "amount": float(amounts[key]),
                "entered": key in ENTERED_LINES,
            }
        )

    def detail(rows):
        return [
            {
                "component_id": r["component_id"],
                "tax_code": r["tax_code"],
                "name": r["name"],
                "rate": float(r["rate"]),
                "collected": float(r["collected"]),
                "recoverable": float(r["recoverable"]),
                "net": float(r["net"]),
            }
            for r in rows
        ]

    qst_summary = None
    if qst or _has_component(db, is_qst):
        collected = _q(sum((r["collected"] for r in qst), Decimal("0")))
        refunds = _q(sum((r["recoverable"] for r in qst), Decimal("0")))
        qst_summary = {
            "components": detail(qst),
            "collected": float(collected),
            "input_tax_refunds": float(refunds),
            "net": float(collected - refunds),
        }

    result = {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "lines": lines,
        "components": detail(gst),
        "qst": qst_summary,
        "files_with_revenu_quebec": quebec,
        "notes": _notes(quebec),
        "ledger": _gst_hst_ledger(
            db, start_date, end_date, amounts["103"] - amounts["106"]
        ),
    }
    ledger = result["ledger"]
    if ledger and ledger.get("uncoded_tax"):
        where = ", ".join(
            f"{a['account_number'] or ''} {a['name']}".strip()
            for a in ledger["uncoded_accounts"]
        )
        result["notes"].insert(
            0,
            f"{ledger['uncoded_tax']:.2f} of sales tax was charged on a typed rate, "
            f"with no tax code, and posted to {where}. It is not on lines 103 or "
            "106: give those documents a tax code before filing.",
        )
    return result


def _notes(quebec: bool) -> list[str]:
    """What the screen says beside the lines, carried onto the printout and
    the CSV too: a filer working from paper needs the same caveats."""
    notes = [
        "Lines 104, 107, 110 and 111 are amounts the books do not hold "
        "(adjustments, instalments paid, rebates claimed); they are 0 unless "
        "entered.",
        "Lines 113B and 113C (self-assessed GST/HST, lines 205 and 405) are not "
        "tracked by these books.",
        "Amounts are shown to the cent; the CRA's line 101 takes whole dollars.",
        "Line 101 counts the period's invoices, sales receipts and credit memos. "
        "Other revenue (deposits straight to income, interest, sales of capital "
        "property) is not in it: add it before filing.",
        "This is the regular method. A Quick Method filer reports differently "
        "on lines 101 and 103 and claims no ITCs on operating expenses, so these "
        "lines are not theirs to copy.",
    ]
    if quebec:
        notes.append(
            "Filed with Revenu Québec on its FPZ-500, whose lines after 111 differ "
            "from the GST34's, so those lines are shown without numbers."
        )
    return notes


def _has_component(db: Session, rule) -> bool:
    from app.models.tax_codes import TaxCodeComponent

    return any(rule(name) for (name,) in db.query(TaxCodeComponent.name).all())


def _gst_hst_ledger(db: Session, start_date, end_date, net_tax: Decimal):
    """What the GST/HST accounts say for the same period, as the Sales Tax
    report's reconciliation does for Sales Tax Payable: every account a GST
    or HST component posts its tax owed or its input tax credits to, the
    net tax posted there (less payments), the payments, the balance at the
    end date, and how far the posted tax is from the return's net tax.

    An account shared with another tax (PST owed to Sales Tax Payable beside
    GST) carries that tax too; its postings are told apart by their
    component and set aside, so they are named rather than shown as a
    difference. What is left over is a posting with no component at all: a
    document with no tax code, a journal entry, a void of an earlier sale."""
    from app.models.accounts import Account
    from app.models.tax_codes import TaxCodeComponent
    from app.models.transactions import Transaction, TransactionLine
    from app.services import control_accounts, tax_codes

    comps = db.query(TaxCodeComponent).all()
    gst_ids = {c.id for c in comps if is_gst_hst(c.name)}
    if not gst_ids:
        return None
    main = control_accounts.find(db, "2200")
    account_ids: list[int] = []
    for c in comps:
        if c.id not in gst_ids:
            continue
        candidates = [c.payable_account_id or main]
        if c.is_recoverable and c.recoverable_account_id:
            candidates.append(c.recoverable_account_id)
        for account_id in candidates:
            if account_id is not None and account_id not in account_ids:
                account_ids.append(account_id)
    if not account_ids:
        return None

    def owed(*conds, account_ids=account_ids) -> Decimal:
        dr, cr = (
            db.query(
                sqlfunc.coalesce(sqlfunc.sum(TransactionLine.debit), 0),
                sqlfunc.coalesce(sqlfunc.sum(TransactionLine.credit), 0),
            )
            .join(Transaction, TransactionLine.transaction_id == Transaction.id)
            .filter(TransactionLine.account_id.in_(account_ids), *conds)
            .one()
        )
        return _q(Decimal(str(cr)) - Decimal(str(dr)))

    in_period = (Transaction.date >= start_date, Transaction.date <= end_date)
    payment = Transaction.source_type == "sales_tax_payment"
    not_payment = Transaction.source_type.is_(None) | ~payment
    other_tax = TransactionLine.tax_component_id.isnot(None) & ~(
        TransactionLine.tax_component_id.in_(gst_ids)
    )
    posted = owed(*in_period, not_payment, ~other_tax)
    shared = owed(*in_period, other_tax)
    shared_names = sorted(
        {
            c.name
            for c in comps
            if c.id not in gst_ids
            and (
                (c.payable_account_id or main) in account_ids
                or (c.is_recoverable and c.recoverable_account_id in account_ids)
            )
        }
    )
    payable = set(tax_codes.tax_accounts(db))
    # Sales tax charged with no tax code (a rate typed on the document) on
    # a tax account outside these: it cannot be on lines 103 or 106, and the
    # check above cannot see it, so it is counted and named on its own.
    elsewhere = [i for i in payable if i not in account_ids]
    uncoded = (
        owed(
            *in_period,
            not_payment,
            TransactionLine.tax_component_id.is_(None),
            account_ids=elsewhere,
        )
        if elsewhere
        else Decimal("0.00")
    )
    end = Transaction.date <= end_date
    accounts = []
    for account_id in account_ids:
        acct = db.get(Account, account_id)
        if acct is None:
            continue
        accounts.append(
            {
                "id": acct.id,
                "account_number": acct.account_number,
                "name": acct.name,
                "balance": float(owed(end, account_ids=[acct.id])),
                # the pay endpoint takes only an account tax is owed on
                "payable": acct.id in payable,
            }
        )
    return {
        "accounts": accounts,
        "tax_posted": float(posted),
        "other_tax": float(shared),
        "other_tax_names": shared_names,
        "payments": float(-owed(*in_period, payment)),
        "balance": float(owed(end)),
        "difference": float(_q(net_tax - posted)),
        "uncoded_tax": float(uncoded),
        "uncoded_accounts": (
            [
                {"account_number": a.account_number, "name": a.name}
                for a in (db.get(Account, i) for i in elsewhere)
                if a is not None
            ]
            if uncoded
            else []
        ),
    }


def gst_hst_return_csv(data: dict, company: str) -> str:
    """Line | Description | Amount, the return's lines as the screen shows
    them, then the QST summary when there is one, without line numbers."""
    from app.services.csv_export import _SafeWriter
    from app.services.ledger_exports import _money, _preamble

    out = io.StringIO()
    w = _SafeWriter(out)
    _preamble(
        w, company, "GST/HST Return", f"{data['start_date']} to {data['end_date']}"
    )
    w.writerow(["Line", "Description", "Amount"])
    for line in data["lines"]:
        w.writerow([line["line"] or "", line["label"], _money(line["amount"])])
    qst = data.get("qst")
    if qst:
        w.writerow([])
        w.writerow(["", "QST (Revenu Québec return FPZ-500)", ""])
        w.writerow(["", "QST collected", _money(qst["collected"])])
        w.writerow(["", "QST input tax refunds", _money(qst["input_tax_refunds"])])
        w.writerow(["", "Net QST", _money(qst["net"])])
    w.writerow([])
    for note in data.get("notes", []):
        w.writerow(["", note, ""])
    return out.getvalue()
