"""A customer can pay the federal tax, the provincial tax, or both (#456).

A reseller with a BC PST exemption certificate pays GST but not PST. A
tax-code component carries a level (federal or provincial); a customer
says which levels it pays; a sales document keeps the levels its
customer was exempt from when it was saved, so changing the customer
later never re-taxes it.
"""

from decimal import Decimal

import pytest

from app.models.transactions import Transaction, TransactionLine

DAY = "2026-07-02"
PERIOD = "start_date=2026-07-01&end_date=2026-09-30"


@pytest.fixture
def bc(client, seed_accounts):
    r = client.put("/api/settings", json={"tax_country": "CA", "tax_province": "BC"})
    assert r.status_code == 200, r.text
    r = client.post("/api/tax-codes/presets/canada", json={"province": "BC"})
    assert r.status_code == 200, r.text
    codes = {c["name"]: c for c in client.get("/api/tax-codes").json()}
    return codes["GST + PST BC 12%"]


def _customer(client, **flags):
    r = client.post("/api/customers", json={"name": "Harbour Resale", **flags})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _invoice(client, customer_id, code, rate=100):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": customer_id,
            "date": DAY,
            "tax_code_id": code["id"],
            "lines": [{"description": "Stock", "quantity": 1, "rate": rate}],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _tax_lines(db_session, invoice_id):
    db_session.expire_all()
    txn = (
        db_session.query(Transaction)
        .filter_by(source_type="invoice", source_id=invoice_id)
        .one()
    )
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn.id).all()
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines)
    return [ln for ln in lines if ln.tax_component_id is not None]


def test_the_presets_give_each_component_its_level(client, bc):
    levels = {c["name"]: c["level"] for c in bc["components"]}
    assert levels == {"GST": "federal", "PST": "provincial"}


def test_a_pst_exempt_customer_pays_gst_only(client, db_session, bc):
    cust = _customer(
        client, charges_provincial_tax=False, tax_exemption_number="PST-1234"
    )
    assert cust["charges_provincial_tax"] is False
    inv = _invoice(client, cust["id"], bc)
    assert Decimal(str(inv["tax_amount"])) == Decimal("5.00")
    assert Decimal(inv["tax_rate"]) == Decimal("0.05")
    assert inv["exempt_tax_levels"] == "provincial"
    (gst,) = _tax_lines(db_session, inv["id"])
    assert gst.credit == Decimal("5.00")

    # the printout shows the GST it charged, and no PST
    page = client.get(f"/api/invoices/{inv['id']}/print-preview").text
    assert "GST (5.00%)" in page and "PST (" not in page

    # and the return counts it like any other GST
    lines = {
        line["key"]: line["amount"]
        for line in client.get(f"/api/reports/gst-hst-return?{PERIOD}").json()["lines"]
    }
    assert lines["103"] == 5.00


def test_a_customer_who_pays_both_pays_both(client, bc):
    inv = _invoice(client, _customer(client)["id"], bc)
    assert Decimal(str(inv["tax_amount"])) == Decimal("12.00")


def test_changing_the_customer_later_does_not_retax_old_invoices(
    client, db_session, bc
):
    cust = _customer(client, charges_provincial_tax=False)
    inv = _invoice(client, cust["id"], bc)
    r = client.put(
        f"/api/customers/{cust['id']}", json={"charges_provincial_tax": True}
    )
    assert r.status_code == 200, r.text
    # an edit of the old invoice keeps the exemption it was saved with
    r = client.put(
        f"/api/invoices/{inv['id']}",
        json={"lines": [{"description": "Stock", "quantity": 2, "rate": 100}]},
    )
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["tax_amount"])) == Decimal("10.00")
    # a new invoice takes the customer as they are now
    assert Decimal(str(_invoice(client, cust["id"], bc)["tax_amount"])) == Decimal(
        "12.00"
    )


def test_a_credit_memo_gives_back_what_was_charged(client, db_session, bc):
    cust = _customer(client, charges_provincial_tax=False)
    inv = _invoice(client, cust["id"], bc)
    client.put(f"/api/customers/{cust['id']}", json={"charges_provincial_tax": True})
    r = client.post(
        "/api/credit-memos",
        json={
            "customer_id": cust["id"],
            "date": DAY,
            "original_invoice_id": inv["id"],
            "lines": [{"description": "Returned", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["tax_amount"])) == Decimal("5.00")


def test_an_hst_code_is_charged_in_full_to_a_provincially_exempt_customer(
    client, seed_accounts
):
    client.put("/api/settings", json={"tax_country": "CA", "tax_province": "ON"})
    client.post("/api/tax-codes/presets/canada", json={"province": "ON"})
    hst = {c["name"]: c for c in client.get("/api/tax-codes").json()}["HST ON 13%"]
    cust = _customer(client, charges_provincial_tax=False)
    assert Decimal(str(_invoice(client, cust["id"], hst)["tax_amount"])) == Decimal(
        "13.00"
    )


def test_a_customer_exempt_from_both_pays_neither(client, bc):
    cust = _customer(client, charges_federal_tax=False, charges_provincial_tax=False)
    assert Decimal(str(_invoice(client, cust["id"], bc)["tax_amount"])) == Decimal(
        "0.00"
    )


def test_a_component_with_no_level_is_charged_to_everyone(client, seed_accounts):
    code = client.post(
        "/api/tax-codes",
        json={"name": "County", "components": [{"name": "County", "rate": 0.01}]},
    ).json()
    cust = _customer(client, charges_provincial_tax=False, charges_federal_tax=False)
    assert Decimal(str(_invoice(client, cust["id"], code)["tax_amount"])) == Decimal(
        "1.00"
    )


def test_the_customer_form_asks_only_a_canadian_company():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "app/static/js/customers.js"
    ).read_text(encoding="utf-8")
    assert "if (s.tax_country !== 'CA') return '';" in src
    assert 'name="charges_provincial_tax"' in src
