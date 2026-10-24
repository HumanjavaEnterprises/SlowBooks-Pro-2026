"""Tax codes: a document's tax as one or more components (#456).

The first seam of docs/design/canada.md, built as a general feature. A code
is a set of components, each taxed and rounded on its own and posted to its
own payable account; a recoverable component on a purchase posts to its
recoverable account instead of into what the lines cost. A document with no
code keeps its single rate, posted to Sales Tax Payable (2200) as before.
"""

from decimal import Decimal

import pytest

from app.models.accounts import Account, AccountType
from app.models.transactions import Transaction, TransactionLine

DAY = "2026-07-02"


@pytest.fixture
def accounts(db_session, seed_accounts):
    """The seeded chart plus a county payable and a recoverable-tax asset."""
    extra = {
        "2210": ("County Sales Tax Payable", AccountType.LIABILITY),
        "1450": ("Input Tax Credits", AccountType.ASSET),
    }
    out = dict(seed_accounts)
    for number, (name, kind) in extra.items():
        acct = Account(
            account_number=number, name=name, account_type=kind, balance=Decimal(0)
        )
        db_session.add(acct)
        out[number] = acct
    db_session.commit()
    return out


def _code(client, name, *components):
    r = client.post(
        "/api/tax-codes", json={"name": name, "components": list(components)}
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def state_county(client, accounts):
    """6% state to Sales Tax Payable, 1.25% county to its own account."""
    return _code(
        client,
        "Springfield 7.25%",
        {"name": "State", "rate": 0.06},
        {"name": "County", "rate": 0.0125, "payable_account_id": accounts["2210"].id},
    )


@pytest.fixture
def gst_pst(client, accounts):
    """5% recoverable (claimed back on purchases), 7% not."""
    return _code(
        client,
        "GST + PST",
        {
            "name": "GST",
            "rate": 0.05,
            "is_recoverable": True,
            "recoverable_account_id": accounts["1450"].id,
        },
        {"name": "PST", "rate": 0.07},
    )


def _lines(db_session, source_type, source_id):
    # an edit re-posts under the same transaction: read it afresh
    db_session.expire_all()
    txn = (
        db_session.query(Transaction)
        .filter_by(source_type=source_type, source_id=source_id)
        .order_by(Transaction.id.desc())
        .first()
    )
    assert txn is not None
    lines = db_session.query(TransactionLine).filter_by(transaction_id=txn.id).all()
    assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines)
    return lines


def _on(lines, account):
    return [ln for ln in lines if ln.account_id == account.id]


def _invoice(client, customer_id, **extra):
    body = {
        "customer_id": customer_id,
        "date": DAY,
        "lines": [{"description": "Sign", "quantity": 1, "rate": 200}],
        **extra,
    }
    r = client.post("/api/invoices", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _vendor(client):
    r = client.post("/api/vendors", json={"name": "Harbor Supply"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


# ── Sales ────────────────────────────────────────────────────────────────


def test_an_invoice_posts_each_component_to_its_own_account(
    client, db_session, accounts, seed_customer, state_county
):
    inv = _invoice(client, seed_customer.id, tax_code_id=state_county["id"])

    assert inv["tax_code_id"] == state_county["id"]
    assert Decimal(inv["tax_rate"]) == Decimal("0.0725")
    assert Decimal(str(inv["tax_amount"])) == Decimal("14.50")
    assert Decimal(str(inv["total"])) == Decimal("214.50")

    lines = _lines(db_session, "invoice", inv["id"])
    (state,) = _on(lines, accounts["2200"])
    (county,) = _on(lines, accounts["2210"])
    assert state.credit == Decimal("12.00") and county.credit == Decimal("2.50")
    comps = {c["name"]: c["id"] for c in state_county["components"]}
    assert state.tax_component_id == comps["State"]
    assert county.tax_component_id == comps["County"]
    assert county.description == "Sales tax: County"


def test_each_component_is_rounded_on_its_own(client, accounts, seed_customer):
    code = _code(
        client,
        "Five and seven",
        {"name": "A", "rate": 0.05},
        {"name": "B", "rate": 0.07},
    )
    inv = _invoice(
        client,
        seed_customer.id,
        tax_code_id=code["id"],
        lines=[{"description": "Part", "quantity": 1, "rate": 10.10}],
    )
    # 0.505 -> 0.51 and 0.707 -> 0.71, as a receipt prints them; one 12%
    # rate would have made 1.21
    assert Decimal(str(inv["tax_amount"])) == Decimal("1.22")


def test_a_document_without_a_code_posts_as_it_always_did(
    client, db_session, accounts, seed_customer
):
    inv = _invoice(client, seed_customer.id, tax_rate=0.0825)
    lines = _lines(db_session, "invoice", inv["id"])
    (tax,) = _on(lines, accounts["2200"])
    assert tax.credit == Decimal("16.50")
    assert tax.description == "Sales tax"
    assert tax.tax_component_id is None
    assert inv["tax_code_id"] is None


def test_the_code_wins_over_a_rate_sent_with_it(client, seed_customer, state_county):
    inv = _invoice(
        client, seed_customer.id, tax_code_id=state_county["id"], tax_rate=0.5
    )
    assert Decimal(inv["tax_rate"]) == Decimal("0.0725")


def test_only_taxable_lines_are_taxed(client, accounts, seed_customer, state_county):
    inv = _invoice(
        client,
        seed_customer.id,
        tax_code_id=state_county["id"],
        lines=[
            {"description": "Part", "quantity": 1, "rate": 100},
            {"description": "Labour", "quantity": 1, "rate": 100, "is_taxable": False},
        ],
    )
    assert Decimal(str(inv["tax_amount"])) == Decimal("7.25")


def test_a_void_reverses_every_component(
    client, db_session, accounts, seed_customer, state_county
):
    inv = _invoice(client, seed_customer.id, tax_code_id=state_county["id"])
    r = client.post(f"/api/invoices/{inv['id']}/void")
    assert r.status_code == 200, r.text
    county = (
        db_session.query(TransactionLine)
        .filter(TransactionLine.account_id == accounts["2210"].id)
        .all()
    )
    assert sum(ln.credit - ln.debit for ln in county) == 0
    assert {ln.tax_component_id for ln in county} == {
        state_county["components"][1]["id"]
    }


def test_editing_an_invoice_to_another_code_reposts_it(
    client, db_session, accounts, seed_customer, state_county
):
    inv = _invoice(client, seed_customer.id, tax_rate=0.05)
    r = client.put(
        f"/api/invoices/{inv['id']}", json={"tax_code_id": state_county["id"]}
    )
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["tax_amount"])) == Decimal("14.50")
    lines = _lines(db_session, "invoice", inv["id"])
    assert _on(lines, accounts["2210"])[0].credit == Decimal("2.50")

    # cleared: the single rate it is sent, to Sales Tax Payable
    r = client.put(
        f"/api/invoices/{inv['id']}", json={"tax_code_id": None, "tax_rate": 0.05}
    )
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["tax_amount"])) == Decimal("10.00")
    lines = _lines(db_session, "invoice", inv["id"])
    assert not _on(lines, accounts["2210"])
    assert _on(lines, accounts["2200"])[0].credit == Decimal("10.00")


def test_a_credit_memo_gives_back_each_component(
    client, db_session, accounts, seed_customer, state_county
):
    inv = _invoice(client, seed_customer.id, tax_code_id=state_county["id"])
    r = client.post(
        "/api/credit-memos",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "original_invoice_id": inv["id"],
            "lines": [{"description": "Returned", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    cm = r.json()
    assert cm["tax_code_id"] == state_county["id"]
    assert Decimal(str(cm["tax_amount"])) == Decimal("7.25")
    lines = _lines(db_session, "credit_memo", cm["id"])
    assert _on(lines, accounts["2200"])[0].debit == Decimal("6.00")
    assert _on(lines, accounts["2210"])[0].debit == Decimal("1.25")


def test_a_sales_receipt_takes_a_code(
    client, db_session, accounts, seed_customer, state_county
):
    r = client.post(
        "/api/sales-receipts",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "tax_code_id": state_county["id"],
            "lines": [{"description": "Counter sale", "quantity": 1, "rate": 200}],
        },
    )
    assert r.status_code == 201, r.text
    inv = r.json()["invoice"]
    assert inv["tax_code_id"] == state_county["id"]
    assert Decimal(str(inv["tax_amount"])) == Decimal("14.50")
    lines = _lines(db_session, "invoice", inv["id"])
    assert _on(lines, accounts["2210"])[0].credit == Decimal("2.50")


def test_an_estimate_carries_its_code_to_the_invoice(
    client, db_session, accounts, seed_customer, state_county
):
    r = client.post(
        "/api/estimates",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "tax_code_id": state_county["id"],
            "lines": [{"description": "Sign", "quantity": 1, "rate": 200}],
        },
    )
    assert r.status_code == 201, r.text
    est = r.json()
    assert Decimal(str(est["tax_amount"])) == Decimal("14.50")
    r = client.post(f"/api/estimates/{est['id']}/convert")
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["tax_code_id"] == state_county["id"]
    lines = _lines(db_session, "invoice", inv["id"])
    assert _on(lines, accounts["2210"])[0].credit == Decimal("2.50")


# ── Purchases ────────────────────────────────────────────────────────────


def _bill(client, vendor_id, accounts, **extra):
    r = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor_id,
            "date": DAY,
            "lines": [
                {
                    "description": "Shelving",
                    "quantity": 1,
                    "rate": 100,
                    "account_id": accounts["6000"].id,
                }
            ],
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_a_bill_claims_the_recoverable_component_back(
    client, db_session, accounts, gst_pst
):
    bill = _bill(client, _vendor(client), accounts, tax_code_id=gst_pst["id"])
    assert Decimal(str(bill["tax_amount"])) == Decimal("12.00")
    assert Decimal(str(bill["total"])) == Decimal("112.00")

    lines = _lines(db_session, "bill", bill["id"])
    (itc,) = _on(lines, accounts["1450"])
    assert itc.debit == Decimal("5.00")
    assert itc.tax_component_id == gst_pst["components"][0]["id"]
    # the PST is part of what the shelving cost; the GST is not
    assert _on(lines, accounts["6000"])[0].debit == Decimal("107.00")
    assert _on(lines, accounts["2000"])[0].credit == Decimal("112.00")
    assert not _on(lines, accounts["2200"])


def test_a_bill_without_a_recoverable_component_spreads_all_of_it(
    client, db_session, accounts, state_county
):
    bill = _bill(client, _vendor(client), accounts, tax_code_id=state_county["id"])
    lines = _lines(db_session, "bill", bill["id"])
    assert _on(lines, accounts["6000"])[0].debit == Decimal("107.25")
    assert not _on(lines, accounts["2200"]) and not _on(lines, accounts["2210"])


def test_a_vendor_credit_gives_the_recoverable_tax_back(
    client, db_session, accounts, gst_pst
):
    r = client.post(
        "/api/vendor-credits",
        json={
            "vendor_id": _vendor(client),
            "date": DAY,
            "tax_code_id": gst_pst["id"],
            "lines": [
                {
                    "description": "Returned shelf",
                    "quantity": 1,
                    "rate": 100,
                    "account_id": accounts["6000"].id,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    lines = _lines(db_session, "vendor_credit", r.json()["id"])
    assert _on(lines, accounts["1450"])[0].credit == Decimal("5.00")
    assert _on(lines, accounts["6000"])[0].credit == Decimal("107.00")


def test_a_purchase_order_becomes_a_bill_with_its_code(
    client, db_session, accounts, gst_pst
):
    r = client.post(
        "/api/purchase-orders",
        json={
            "vendor_id": _vendor(client),
            "date": DAY,
            "tax_code_id": gst_pst["id"],
            "lines": [{"description": "Shelving", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    po = r.json()
    assert Decimal(str(po["tax_amount"])) == Decimal("12.00")
    line_id = po["lines"][0]["id"]
    r = client.post(
        f"/api/purchase-orders/{po['id']}/convert-to-bill",
        json={"lines": [{"line_id": line_id, "account_id": accounts["6000"].id}]},
    )
    assert r.status_code in (200, 201), r.text
    bill_id = r.json()["bill_id"] if "bill_id" in r.json() else r.json()["id"]
    lines = _lines(db_session, "bill", bill_id)
    assert _on(lines, accounts["1450"])[0].debit == Decimal("5.00")
    assert _on(lines, accounts["6000"])[0].debit == Decimal("107.00")


# ── The codes themselves ─────────────────────────────────────────────────


def test_a_code_in_use_keeps_its_rates(client, seed_customer, state_county):
    _invoice(client, seed_customer.id, tax_code_id=state_county["id"])
    r = client.put(
        f"/api/tax-codes/{state_county['id']}",
        json={"components": [{"name": "State", "rate": 0.07}]},
    )
    assert r.status_code == 409
    assert "archive" in r.json()["detail"]
    # its name is not a rate: that still changes
    r = client.put(f"/api/tax-codes/{state_county['id']}", json={"name": "Old county"})
    assert r.status_code == 200 and r.json()["in_use"] is True
    assert client.delete(f"/api/tax-codes/{state_county['id']}").status_code == 409


def test_a_code_not_yet_used_can_change_or_go(client, state_county):
    r = client.put(
        f"/api/tax-codes/{state_county['id']}",
        json={"components": [{"name": "State", "rate": 0.07}]},
    )
    assert r.status_code == 200, r.text
    assert Decimal(r.json()["rate"]) == Decimal("0.07")
    assert client.delete(f"/api/tax-codes/{state_county['id']}").status_code == 204


def test_an_archived_code_stays_on_old_documents_only(
    client, seed_customer, state_county
):
    inv = _invoice(client, seed_customer.id, tax_code_id=state_county["id"])
    r = client.put(f"/api/tax-codes/{state_county['id']}", json={"is_archived": True})
    assert r.status_code == 200, r.text

    listed = client.get("/api/tax-codes").json()
    assert state_county["id"] not in [c["id"] for c in listed]
    everything = client.get("/api/tax-codes?include_archived=true").json()
    assert state_county["id"] in [c["id"] for c in everything]

    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "tax_code_id": state_county["id"],
            "lines": [{"description": "Sign", "quantity": 1, "rate": 200}],
        },
    )
    assert r.status_code == 400 and "archived" in r.json()["detail"]

    # the old invoice is edited as it stands, at its own code's rate
    r = client.put(f"/api/invoices/{inv['id']}", json={"notes": "Called about it"})
    assert r.status_code == 200, r.text
    r = client.put(
        f"/api/invoices/{inv['id']}",
        json={"lines": [{"description": "Sign", "quantity": 2, "rate": 200}]},
    )
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["tax_amount"])) == Decimal("29.00")


def test_a_schedule_on_an_archived_code_makes_nothing_and_says_why(
    client, seed_customer, state_county
):
    r = client.post(
        "/api/recurring",
        json={
            "customer_id": seed_customer.id,
            "frequency": "monthly",
            "start_date": DAY,
            "tax_code_id": state_county["id"],
            "lines": [{"description": "Hosting", "quantity": 1, "rate": 50}],
        },
    )
    assert r.status_code == 201, r.text
    client.put(f"/api/tax-codes/{state_county['id']}", json={"is_archived": True})
    run = client.post(f"/api/recurring/generate?as_of={DAY}").json()
    assert run["invoices_created"] == 0
    assert "archived" in run["skipped"][0]["message"]


def test_a_schedule_posts_its_components(
    client, db_session, accounts, seed_customer, state_county
):
    r = client.post(
        "/api/recurring",
        json={
            "customer_id": seed_customer.id,
            "frequency": "monthly",
            "start_date": DAY,
            "tax_code_id": state_county["id"],
            "lines": [{"description": "Hosting", "quantity": 1, "rate": 200}],
        },
    )
    assert r.status_code == 201, r.text
    run = client.post(f"/api/recurring/generate?as_of={DAY}").json()
    (inv_id,) = run["invoice_ids"]
    lines = _lines(db_session, "invoice", inv_id)
    assert _on(lines, accounts["2210"])[0].credit == Decimal("2.50")


@pytest.mark.parametrize(
    "component, said",
    [
        ({"name": "GST", "rate": 0.05, "is_recoverable": True}, "recoverable"),
        ({"name": "GST", "rate": 0.05, "payable_account_id": "6000"}, "liability"),
        ({"name": "GST", "rate": 5}, "more than 100%"),
    ],
)
def test_a_component_is_checked(client, accounts, component, said):
    if component.get("payable_account_id") == "6000":
        component["payable_account_id"] = accounts["6000"].id
    r = client.post("/api/tax-codes", json={"name": "Bad", "components": [component]})
    assert r.status_code in (400, 422), r.text
    assert said in r.text


def test_names_are_unique(client, state_county):
    r = client.post(
        "/api/tax-codes",
        json={
            "name": state_county["name"],
            "components": [{"name": "X", "rate": 0.01}],
        },
    )
    assert r.status_code == 409
    r = client.post(
        "/api/tax-codes",
        json={
            "name": "Twice",
            "components": [{"name": "X", "rate": 0.01}, {"name": "x", "rate": 0.02}],
        },
    )
    assert r.status_code == 400


# ── The Sales Tax report ─────────────────────────────────────────────────

PERIOD = "start_date=2026-07-01&end_date=2026-07-31"


def test_the_report_breaks_tax_out_by_component(
    client, accounts, seed_customer, state_county, gst_pst
):
    inv = _invoice(client, seed_customer.id, tax_code_id=state_county["id"])
    client.post(
        "/api/credit-memos",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "original_invoice_id": inv["id"],
            "lines": [{"description": "Returned", "quantity": 1, "rate": 100}],
        },
    )
    _invoice(client, seed_customer.id, tax_code_id=gst_pst["id"])
    _bill(client, _vendor(client), accounts, tax_code_id=gst_pst["id"])

    rep = client.get(f"/api/reports/sales-tax?{PERIOD}").json()
    by = {(c["tax_code"], c["name"]): c for c in rep["components"]}
    assert by[("Springfield 7.25%", "State")]["collected"] == 6.00  # 12 - 6
    assert by[("Springfield 7.25%", "County")]["collected"] == 1.25  # 2.50 - 1.25
    gst = by[("GST + PST", "GST")]
    assert (gst["collected"], gst["recoverable"], gst["net"]) == (10.00, 5.00, 5.00)
    assert by[("GST + PST", "PST")]["recoverable"] == 0

    # every account tax is owed to is read with 2200, so the report's total
    # and the ledger agree: 7.25 + 24.00 of tax on sales, nothing unexplained
    ledger = rep["ledger"]
    assert {a["account_number"] for a in ledger["accounts"]} == {"2200", "2210"}
    assert rep["total_tax"] == 31.25
    assert ledger["tax_posted"] == 31.25
    assert ledger["difference"] == 0


def test_recoverable_tax_on_the_payable_account_nets_the_balance(
    client, accounts, seed_customer
):
    """One GST/HST account for both sides, as many Canadian charts keep it:
    the input tax credits net against the tax collected."""
    code = _code(
        client,
        "HST 13%",
        {
            "name": "HST",
            "rate": 0.13,
            "is_recoverable": True,
            "recoverable_account_id": accounts["2200"].id,
        },
    )
    _invoice(client, seed_customer.id, tax_code_id=code["id"])  # 26.00
    _bill(client, _vendor(client), accounts, tax_code_id=code["id"])  # 13.00

    rep = client.get(f"/api/reports/sales-tax?{PERIOD}").json()
    (hst,) = rep["components"]
    assert (hst["collected"], hst["recoverable"], hst["net"]) == (26.0, 13.0, 13.0)
    ledger = rep["ledger"]
    assert ledger["tax_posted"] == 26.0 and ledger["difference"] == 0
    assert ledger["recoverable"] == 13.0
    assert ledger["purchase_tax"] == 0
    assert ledger["balance"] == 13.0


def test_tax_is_paid_from_the_account_it_is_owed_on(
    client, db_session, accounts, seed_customer, state_county
):
    _invoice(client, seed_customer.id, tax_code_id=state_county["id"])
    body = {
        "date": "2026-07-31",
        "amount": 2.50,
        "pay_from_account_id": accounts["1000"].id,
        "tax_account_id": accounts["2210"].id,
    }
    r = client.post("/api/reports/sales-tax/pay", json=body)
    assert r.status_code == 200, r.text
    lines = (
        db_session.query(TransactionLine)
        .filter_by(transaction_id=r.json()["transaction_id"])
        .all()
    )
    assert _on(lines, accounts["2210"])[0].debit == Decimal("2.50")

    r = client.post(
        "/api/reports/sales-tax/pay",
        json={**body, "tax_account_id": accounts["6000"].id},
    )
    assert r.status_code == 400 and "holds no sales tax" in r.json()["detail"]


# ── The migration ────────────────────────────────────────────────────────


def test_the_migration_adds_the_codes_and_takes_them_away(tmp_path):
    from pathlib import Path

    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    db = tmp_path / "fresh.db"
    url = "sqlite:///" + db.as_posix()
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = url
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1
    command.upgrade(cfg, "head")
    engine = sa.create_engine(url)
    insp = sa.inspect(engine)
    assert {"tax_codes", "tax_code_components"} <= set(insp.get_table_names())
    for table in ("invoices", "bills", "estimates", "purchase_orders"):
        assert "tax_code_id" in {c["name"] for c in insp.get_columns(table)}
    assert "tax_component_id" in {
        c["name"] for c in insp.get_columns("transaction_lines")
    }

    command.downgrade(cfg, "d3e4f5a6b7c8")
    insp = sa.inspect(engine)
    assert "tax_codes" not in insp.get_table_names()
    assert "tax_code_id" not in {c["name"] for c in insp.get_columns("invoices")}
    # and up again, on a file that has been down
    command.upgrade(cfg, "head")
    engine.dispose()
