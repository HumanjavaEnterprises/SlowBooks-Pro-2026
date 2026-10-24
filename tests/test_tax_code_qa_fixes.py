"""What the final QA found in the tax-code engine, kept fixed (#456)."""

from decimal import Decimal

import pytest

from app.models.accounts import Account, AccountType
from app.models.invoices import Invoice
from app.models.transactions import Transaction

DAY = "2026-07-02"
PERIOD = "start_date=2026-07-01&end_date=2026-09-30"


@pytest.fixture
def gst_hst(db_session, seed_accounts):
    acct = Account(
        account_number="2250",
        name="GST/HST Payable",
        account_type=AccountType.LIABILITY,
        balance=Decimal(0),
    )
    db_session.add(acct)
    db_session.commit()
    return acct


def _code(client, name, *components):
    r = client.post(
        "/api/tax-codes", json={"name": name, "components": list(components)}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_a_zero_rate_code_on_an_imported_tax_does_not_unbalance_the_entry(
    client, db_session, seed_accounts, seed_customer
):
    """A QuickBooks Online import keeps a tax amount with no rate behind it.
    Giving that invoice a 0% code used to carry the kept tax onto a code
    with no rate to post it by: an entry out of balance by the tax."""
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "lines": [{"description": "Sign", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    inv = db_session.get(Invoice, r.json()["id"])
    inv.tax_amount, inv.total, inv.balance_due = Decimal("5.00"), 105, 105
    db_session.commit()

    zero = _code(client, "Zero-rated", {"name": "GST", "rate": 0})
    r = client.put(
        f"/api/invoices/{inv.id}",
        json={
            "tax_code_id": zero["id"],
            "lines": [{"description": "Sign", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["tax_amount"])) == Decimal("0.00")


def test_a_bill_voided_from_the_journal_takes_its_itc_back(
    client, db_session, seed_accounts, gst_hst
):
    from app.services.settings_service import set_setting

    set_setting(db_session, "tax_country", "CA")
    db_session.commit()
    hst = _code(
        client,
        "HST ON 13%",
        {
            "name": "HST",
            "rate": 0.13,
            "payable_account_id": gst_hst.id,
            "is_recoverable": True,
            "recoverable_account_id": gst_hst.id,
        },
    )
    vendor = client.post("/api/vendors", json={"name": "Harbour Supply"}).json()
    r = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor["id"],
            "date": DAY,
            "tax_code_id": hst["id"],
            "lines": [
                {
                    "description": "Shelving",
                    "quantity": 1,
                    "rate": 100,
                    "account_id": seed_accounts["6000"].id,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    txn = (
        db_session.query(Transaction)
        .filter_by(source_type="bill", source_id=r.json()["id"])
        .one()
    )
    r = client.post(f"/api/journal/{txn.id}/void")
    assert r.status_code == 200, r.text

    lines = {
        line["key"]: line["amount"]
        for line in client.get(f"/api/reports/gst-hst-return?{PERIOD}").json()["lines"]
    }
    assert lines["106"] == 0  # the ITC is taken back
    assert lines["103"] == 0  # not counted as tax collected


def test_the_list_says_which_codes_are_in_use(client, seed_accounts, seed_customer):
    used = _code(client, "Used", {"name": "A", "rate": 0.05})
    _code(client, "Unused", {"name": "B", "rate": 0.05})
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "tax_code_id": used["id"],
            "lines": [{"description": "Sign", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    listed = {c["name"]: c["in_use"] for c in client.get("/api/tax-codes").json()}
    assert listed == {"Unused": False, "Used": True}
