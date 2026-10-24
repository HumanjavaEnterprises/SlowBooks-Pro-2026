"""Paid receipts carry tax codes: expenses and credit-card charges (#456).

A receipt's total includes its tax, so a code splits that total: each
recoverable component (GST/HST, QST) is claimed back to its own account
and the rest (cost, and tax not claimed back such as PST) goes to the
expense. Without this, GST/HST paid by card or cheque could never reach
the return's input tax credits (found by the final Canadian tax QA).
"""

from decimal import Decimal

import pytest

from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine

DAY = "2026-07-02"
PERIOD = "start_date=2026-07-01&end_date=2026-09-30"


@pytest.fixture
def accounts(db_session, seed_accounts):
    out = dict(seed_accounts)
    acct = Account(
        account_number="2250",
        name="GST/HST Payable",
        account_type=AccountType.LIABILITY,
        balance=Decimal(0),
    )
    db_session.add(acct)
    db_session.commit()
    out["2250"] = acct
    return out


def _code(client, name, *components):
    r = client.post(
        "/api/tax-codes", json={"name": name, "components": list(components)}
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def hst(client, accounts):
    gst_hst = accounts["2250"].id
    return _code(
        client,
        "HST ON 13%",
        {
            "name": "HST",
            "rate": 0.13,
            "payable_account_id": gst_hst,
            "is_recoverable": True,
            "recoverable_account_id": gst_hst,
        },
    )


@pytest.fixture
def gst_pst(client, accounts):
    gst_hst = accounts["2250"].id
    return _code(
        client,
        "GST + PST BC 12%",
        {
            "name": "GST",
            "rate": 0.05,
            "payable_account_id": gst_hst,
            "is_recoverable": True,
            "recoverable_account_id": gst_hst,
        },
        {"name": "PST", "rate": 0.07},
    )


def _lines(db_session, txn_id):
    db_session.expire_all()
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn_id).all()
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines)
    return lines


def _on(lines, account):
    return [ln for ln in lines if ln.account_id == account.id]


def _expense(client, accounts, amount, **extra):
    r = client.post(
        "/api/expenses",
        json={
            "date": DAY,
            "payee": "Office Depot",
            "expense_account_id": accounts["6000"].id,
            "paid_from_account_id": accounts["1000"].id,
            "amount": amount,
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _charge(client, accounts, amount, **extra):
    r = client.post(
        "/api/cc-charges",
        json={
            "date": DAY,
            "payee": "Laptop shop",
            "account_id": accounts["6000"].id,
            "card_account_id": accounts["2100"].id,
            "amount": amount,
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_an_expense_claims_its_hst_back(client, db_session, accounts, hst):
    exp = _expense(client, accounts, 113.00, tax_code_id=hst["id"])
    assert exp["amount"] == 113.00  # what was paid, tax included
    lines = _lines(db_session, exp["id"])
    assert _on(lines, accounts["6000"])[0].debit == Decimal("100.00")
    (itc,) = _on(lines, accounts["2250"])
    assert itc.debit == Decimal("13.00")
    assert itc.tax_component_id == hst["components"][0]["id"]
    assert _on(lines, accounts["1000"])[0].credit == Decimal("113.00")


def test_a_card_charge_claims_gst_and_keeps_pst_in_the_cost(
    client, db_session, accounts, gst_pst
):
    out = _charge(client, accounts, 1120.00, tax_code_id=gst_pst["id"])
    lines = _lines(db_session, out["transaction_id"])
    # 1120 = 1000 + 50 GST + 70 PST; the PST is part of what it cost
    assert _on(lines, accounts["6000"])[0].debit == Decimal("1070.00")
    assert _on(lines, accounts["2250"])[0].debit == Decimal("50.00")
    listed = client.get("/api/cc-charges").json()[0]
    assert listed["amount"] == 1120.00 and listed["account_name"]


def test_the_receipts_own_tax_figure_wins(client, db_session, accounts, hst):
    # 13% backed out of 10.00 is 1.15; the till printed 1.16
    exp = _expense(client, accounts, 10.00, tax_code_id=hst["id"], tax_amount=1.16)
    lines = _lines(db_session, exp["id"])
    assert _on(lines, accounts["2250"])[0].debit == Decimal("1.16")
    assert _on(lines, accounts["6000"])[0].debit == Decimal("8.84")


def test_a_tax_figure_that_is_not_part_of_the_total_is_refused(client, accounts, hst):
    r = client.post(
        "/api/expenses",
        json={
            "date": DAY,
            "expense_account_id": accounts["6000"].id,
            "paid_from_account_id": accounts["1000"].id,
            "amount": 10,
            "tax_code_id": hst["id"],
            "tax_amount": 10,
        },
    )
    assert r.status_code == 400 and "less than the total" in r.json()["detail"]


def test_without_a_code_an_expense_posts_as_before(client, db_session, accounts):
    exp = _expense(client, accounts, 113.00)
    lines = _lines(db_session, exp["id"])
    assert _on(lines, accounts["6000"])[0].debit == Decimal("113.00")
    assert not _on(lines, accounts["2250"])


def test_receipts_reach_the_returns_itcs_and_voids_take_them_back(
    client, db_session, accounts, hst
):
    from app.services.settings_service import set_setting

    set_setting(db_session, "tax_country", "CA")
    db_session.commit()
    exp = _expense(client, accounts, 113.00, tax_code_id=hst["id"])
    out = _charge(client, accounts, 226.00, tax_code_id=hst["id"])
    lines = {
        line["key"]: line["amount"]
        for line in client.get(f"/api/reports/gst-hst-return?{PERIOD}").json()["lines"]
    }
    assert lines["106"] == 39.00  # 13 + 26

    assert client.post(f"/api/expenses/{exp['id']}/void").status_code == 200
    assert (
        client.post(f"/api/cc-charges/{out['transaction_id']}/void").status_code == 200
    )
    lines = {
        line["key"]: line["amount"]
        for line in client.get(f"/api/reports/gst-hst-return?{PERIOD}").json()["lines"]
    }
    assert lines["106"] == 0 and lines["103"] == 0
    voids = (
        db_session.query(Transaction)
        .filter(Transaction.source_type.in_(("expense_void", "cc_charge_void")))
        .count()
    )
    assert voids == 2
