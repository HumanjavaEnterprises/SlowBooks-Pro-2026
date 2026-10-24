"""The GST/HST return (GST34) and the QST summary, for Canadian companies.

Read off the tax-code engine's postings: GST and HST components' tax
collected (line 103) and claimed back (line 106) from the ledger, so voids,
credit memos and vendor credits net as they posted, and line 101 from the
sales documents the Sales Tax report lists. Ontario books one 13% HST to a
single account for both sides; Quebec books 5% GST and 9.975% QST to their
own accounts, and its QST is summed beside the return without line numbers.
"""

import json
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.accounts import Account, AccountType
from app.services.gst_hst_return import is_gst_hst, is_qst

ROOT = Path(__file__).resolve().parent.parent
DAY = "2026-07-02"
PERIOD = "start_date=2026-07-01&end_date=2026-09-30"


@pytest.fixture
def canada(db_session):
    from app.services.settings_service import set_setting

    set_setting(db_session, "tax_country", "CA")
    db_session.commit()


@pytest.fixture
def accounts(db_session, seed_accounts):
    extra = {
        "2210": ("GST/HST Payable", AccountType.LIABILITY),
        "2220": ("QST Payable", AccountType.LIABILITY),
        "1450": ("GST/HST Input Tax Credits", AccountType.ASSET),
        "1460": ("QST Input Tax Refunds", AccountType.ASSET),
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
def hst_on(client, accounts):
    """Ontario: 13% HST, owed to and claimed back on Sales Tax Payable."""
    return _code(
        client,
        "HST ON",
        {
            "name": "HST",
            "rate": 0.13,
            "is_recoverable": True,
            "recoverable_account_id": accounts["2200"].id,
        },
    )


@pytest.fixture
def gst_qst(client, accounts):
    """Quebec: 5% GST and 9.975% QST, each with its own accounts."""
    return _code(
        client,
        "GST + QST",
        {
            "name": "GST",
            "rate": 0.05,
            "payable_account_id": accounts["2210"].id,
            "is_recoverable": True,
            "recoverable_account_id": accounts["1450"].id,
        },
        {
            "name": "QST",
            "rate": 0.09975,
            "payable_account_id": accounts["2220"].id,
            "is_recoverable": True,
            "recoverable_account_id": accounts["1460"].id,
        },
    )


def _post(client, path, **body):
    r = client.post(path, json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _invoice(client, customer_id, rate, **extra):
    return _post(
        client,
        "/api/invoices",
        customer_id=customer_id,
        date=DAY,
        lines=[{"description": "Sign", "quantity": 1, "rate": rate}],
        **extra,
    )


def _credit_memo(client, customer_id, invoice, rate):
    return _post(
        client,
        "/api/credit-memos",
        customer_id=customer_id,
        date=DAY,
        original_invoice_id=invoice["id"],
        lines=[{"description": "Returned", "quantity": 1, "rate": rate}],
    )


def _purchase(client, path, vendor_id, accounts, rate, code):
    return _post(
        client,
        path,
        vendor_id=vendor_id,
        date=DAY,
        tax_code_id=code["id"],
        lines=[
            {
                "description": "Supplies",
                "quantity": 1,
                "rate": rate,
                "account_id": accounts["6000"].id,
            }
        ],
    )


def _vendor(client):
    return _post(client, "/api/vendors", name="Harbour Supply")["id"]


def _report(client, extra=""):
    r = client.get(f"/api/reports/gst-hst-return?{PERIOD}{extra}")
    assert r.status_code == 200, r.text
    data = r.json()
    return data, {line["key"]: line["amount"] for line in data["lines"]}


@pytest.fixture
def ontario_books(client, accounts, seed_customer, hst_on):
    cid = seed_customer.id
    sale = _invoice(client, cid, 200, tax_code_id=hst_on["id"])  # HST 26.00
    voided = _invoice(client, cid, 100, tax_code_id=hst_on["id"])
    _post(client, f"/api/invoices/{voided['id']}/void")
    _invoice(client, cid, 50, tax_rate=0)  # zero-rated: revenue, no tax
    _credit_memo(client, cid, sale, 100)  # HST 13.00 back
    vendor = _vendor(client)
    _purchase(client, "/api/bills", vendor, accounts, 100, hst_on)  # ITC 13.00
    _purchase(client, "/api/vendor-credits", vendor, accounts, 40, hst_on)  # -5.20


def test_the_ontario_return_line_by_line(client, canada, ontario_books, accounts):
    data, lines = _report(client)
    # 200 + 50 - 100: the void is no sale, the zero-rated one is
    assert lines["101"] == 150.00
    assert lines["103"] == 13.00  # 26.00 less the credit memo's 13.00
    assert lines["104"] == 0
    assert lines["105"] == 13.00
    assert lines["106"] == 7.80  # 13.00 less the vendor credit's 5.20
    assert lines["107"] == 0
    assert lines["108"] == 7.80
    assert lines["109"] == 5.20
    assert lines["110"] == 0 and lines["111"] == 0 and lines["112"] == 0
    assert lines["113A"] == 5.20
    assert lines["114"] == 0
    assert lines["115"] == 5.20
    assert [line["line"] for line in data["lines"]][-4:] == [
        "112",
        "113A",
        "114",
        "115",
    ]
    assert data["qst"] is None and data["files_with_revenu_quebec"] is False

    (hst,) = data["components"]
    assert (hst["name"], hst["collected"], hst["recoverable"]) == ("HST", 13.0, 7.8)

    # one account for both sides: what it holds is the net tax, and agrees
    ledger = data["ledger"]
    assert [a["account_number"] for a in ledger["accounts"]] == ["2200"]
    assert ledger["tax_posted"] == 5.20
    assert ledger["difference"] == 0
    assert ledger["other_tax"] == 0
    assert ledger["balance"] == 5.20
    assert ledger["accounts"][0]["payable"] is True


def test_the_lines_the_filer_enters_carry_into_the_totals(
    client, canada, ontario_books
):
    _, lines = _report(client, "&line_104=1.00&line_107=0.50&line_110=10&line_111=0")
    assert lines["104"] == 1.00 and lines["105"] == 14.00
    assert lines["107"] == 0.50 and lines["108"] == 8.30
    assert lines["109"] == 5.70
    assert lines["110"] == 10.00 and lines["112"] == 10.00
    assert lines["113A"] == -4.30
    # a negative balance is a refund, never a negative amount owing
    assert lines["114"] == 4.30 and lines["115"] == 0


def test_the_quebec_return_and_its_qst(
    client, db_session, canada, accounts, seed_customer, gst_qst
):
    from app.services.settings_service import set_setting

    set_setting(db_session, "company_state", "QC")
    db_session.commit()
    cid = seed_customer.id
    sale = _invoice(client, cid, 1000, tax_code_id=gst_qst["id"])  # 50.00 + 99.75
    _credit_memo(client, cid, sale, 200)  # 10.00 + 19.95 back
    vendor = _vendor(client)
    _purchase(client, "/api/bills", vendor, accounts, 400, gst_qst)  # 20.00 + 39.90
    vc = _purchase(client, "/api/vendor-credits", vendor, accounts, 100, gst_qst)
    _post(client, f"/api/vendor-credits/{vc['id']}/void")

    data, lines = _report(client)
    assert lines["101"] == 800.00
    assert lines["103"] == 40.00
    assert lines["106"] == 20.00  # the vendor credit and its void net out
    assert lines["109"] == 20.00
    assert lines["115"] == 20.00
    # QST is never on the GST34
    assert [c["name"] for c in data["components"]] == ["GST"]
    qst = data["qst"]
    assert qst["collected"] == 79.80  # 99.75 - 19.95
    assert qst["input_tax_refunds"] == 39.90
    assert qst["net"] == 39.90

    # Revenu Quebec's FPZ-500 numbers 101-111 as the GST34 does, and not
    # what follows: those lines are shown without a number
    assert data["files_with_revenu_quebec"] is True
    numbers = {line["key"]: line["line"] for line in data["lines"]}
    assert numbers["111"] == "111" and numbers["109"] == "109"
    assert all(numbers[k] is None for k in ("112", "113A", "114", "115"))

    ledger = data["ledger"]
    assert {a["account_number"] for a in ledger["accounts"]} == {"2210", "1450"}
    assert ledger["tax_posted"] == 20.00 and ledger["difference"] == 0
    assert ledger["balance"] == 20.00
    payable = {a["account_number"]: a["payable"] for a in ledger["accounts"]}
    assert payable == {"2210": True, "1450": False}


def test_a_tax_sharing_the_account_is_named_not_a_difference(
    client, canada, accounts, seed_customer
):
    """British Columbia, with PST owed to Sales Tax Payable beside the GST."""
    code = _code(
        client,
        "GST + PST BC",
        {
            "name": "gst ",
            "rate": 0.05,
            "is_recoverable": True,
            "recoverable_account_id": accounts["2200"].id,
        },
        {"name": "PST", "rate": 0.07},
    )
    _invoice(client, seed_customer.id, 100, tax_code_id=code["id"])  # 5 + 7
    _purchase(client, "/api/bills", _vendor(client), accounts, 40, code)  # ITC 2

    data, lines = _report(client)
    assert lines["103"] == 5.00 and lines["106"] == 2.00 and lines["109"] == 3.00
    ledger = data["ledger"]
    assert ledger["tax_posted"] == 3.00 and ledger["difference"] == 0
    assert ledger["other_tax"] == 7.00 and ledger["other_tax_names"] == ["PST"]
    assert ledger["balance"] == 10.00


def test_tax_posted_without_a_component_shows_as_a_difference(
    client, canada, ontario_books, seed_customer
):
    _invoice(client, seed_customer.id, 100, tax_rate=0.13)  # no tax code
    data, lines = _report(client)
    assert lines["103"] == 13.00  # only components named GST or HST count
    assert data["ledger"]["difference"] == -13.00


def test_the_return_is_paid_on_the_gst_account(
    client, canada, accounts, seed_customer, gst_qst
):
    _invoice(client, seed_customer.id, 1000, tax_code_id=gst_qst["id"])
    data, _ = _report(client)
    (gst_account,) = [a for a in data["ledger"]["accounts"] if a["payable"]]
    r = client.post(
        "/api/reports/sales-tax/pay",
        json={
            "date": "2026-09-30",
            "amount": 50.00,
            "pay_from_account_id": accounts["1000"].id,
            "tax_account_id": gst_account["id"],
        },
    )
    assert r.status_code == 200, r.text
    ledger = _report(client)[0]["ledger"]
    assert ledger["payments"] == 50.00
    assert ledger["balance"] == 0
    assert ledger["tax_posted"] == 50.00 and ledger["difference"] == 0


def test_the_csv_and_pdf(client, canada, ontario_books):
    r = client.get(f"/api/reports/gst-hst-return/csv?{PERIOD}&line_110=1")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8-sig")
    assert "Report,GST/HST Return" in text
    assert "103,GST/HST collected or collectible,13.00" in text
    assert "110,Instalments and other annual filer payments,1.00" in text
    assert "115,Amount owing,4.20" in text

    r = client.get(f"/api/reports/gst-hst-return/pdf?{PERIOD}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


def test_the_qst_summary_has_no_line_numbers_in_the_csv(
    client, canada, accounts, seed_customer, gst_qst
):
    _invoice(client, seed_customer.id, 100, tax_code_id=gst_qst["id"])
    text = client.get(f"/api/reports/gst-hst-return/csv?{PERIOD}").content.decode(
        "utf-8-sig"
    )
    assert ",QST collected,9.98" in text
    assert "FPZ-500" in text


@pytest.mark.parametrize(
    "name, gst, qst",
    [
        ("GST", True, False),
        ("hst", True, False),
        (" HST ", True, False),
        ("QST", False, True),
        ("PST", False, False),
        ("RST", False, False),
        ("GST/HST", False, False),
        ("", False, False),
        (None, False, False),
    ],
)
def test_which_components_count(name, gst, qst):
    assert is_gst_hst(name) is gst
    assert is_qst(name) is qst


def test_the_sales_tax_report_still_reads_the_same_documents(
    client, canada, ontario_books
):
    rep = client.get(f"/api/reports/sales-tax?{PERIOD}").json()
    assert rep["total_sales"] == 150.00
    assert rep["total_tax"] == 13.00


# ── The Report Center: Canada only ─────────────────────────────────────


def _cards(settings):
    """The Report Center's cards, from reports.js run in node on stubs."""
    probe = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync('app/static/js/reports.js', 'utf8');
const ctx = {
  console, Promise, JSON, URLSearchParams, CSS: { escape: (s) => s },
  App: { settings: JSON.parse(process.argv[1]) },
  API: { get: async () => [] },
  T: (s) => s, Terms: { text: (s) => s, isNonprofit: () => false },
  escapeHtml: (s) => String(s ?? ''), toast: () => {},
  localStorage: { getItem: () => null },
};
vm.createContext(ctx);
vm.runInContext(src + '\nthis.ReportsPage = ReportsPage;', ctx);
ctx.ReportsPage.render().then((html) => {
  console.log(JSON.stringify((html.match(/data-row-key="report:[^"]+"/g) || [])));
});
"""
    out = subprocess.run(
        ["node", "-e", probe, json.dumps(settings)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_only_a_canadian_company_is_offered_the_return():
    us = _cards({})
    assert 'data-row-key="report:sales-tax"' in us
    assert not any("gst-hst-return" in c for c in us)
    assert _cards({"tax_country": "US"}) == us
    ca = _cards({"tax_country": "CA"})
    assert 'data-row-key="report:gst-hst-return"' in ca
    assert [c for c in ca if "gst-hst-return" not in c] == us


@pytest.mark.parametrize("value", ["1e30", "NaN", "Infinity", "0.001"])
def test_an_entered_line_no_return_could_hold_is_refused(client, canada, value):
    r = client.get(f"/api/reports/gst-hst-return?{PERIOD}&line_104={value}")
    assert r.status_code == 422, r.text


def test_paying_clears_credits_kept_in_their_own_account(
    client, db_session, canada, accounts, seed_customer, gst_qst
):
    """GST collected on 2210, its ITCs on 1450: the remittance pays 2210
    down by the tax collected and takes the ITCs off 1450, so neither
    balance carries into the next period."""
    _invoice(client, seed_customer.id, 1000, tax_code_id=gst_qst["id"])  # 50.00
    _purchase(client, "/api/bills", _vendor(client), accounts, 400, gst_qst)  # 20.00
    r = client.post(
        "/api/reports/sales-tax/pay",
        json={
            "date": "2026-09-30",
            "amount": 30.00,
            "pay_from_account_id": accounts["1000"].id,
            "tax_account_id": accounts["2210"].id,
            "recoverable_account_id": accounts["1450"].id,
            "recoverable_amount": 20.00,
        },
    )
    assert r.status_code == 200, r.text
    accounts_now = {
        a["account_number"]: a["balance"]
        for a in _report(client)[0]["ledger"]["accounts"]
    }
    assert accounts_now == {"2210": 0, "1450": 0}

    # a credit account that holds no tax claimed back is refused
    bad = client.post(
        "/api/reports/sales-tax/pay",
        json={
            "amount": 1,
            "pay_from_account_id": accounts["1000"].id,
            "tax_account_id": accounts["2210"].id,
            "recoverable_account_id": accounts["6000"].id,
            "recoverable_amount": 1,
        },
    )
    assert bad.status_code == 400 and "claimed back" in bad.json()["detail"]


def test_a_tax_sharing_the_recoverable_account_is_named(
    client, canada, accounts, seed_customer
):
    """GST ITCs and QST ITRs in one Taxes Recoverable account: the QST on
    it is named, not left as an unnamed other tax."""
    code = _code(
        client,
        "GST + QST shared",
        {
            "name": "GST",
            "rate": 0.05,
            "payable_account_id": accounts["2210"].id,
            "is_recoverable": True,
            "recoverable_account_id": accounts["1450"].id,
        },
        {
            "name": "QST",
            "rate": 0.09975,
            "payable_account_id": accounts["2220"].id,
            "is_recoverable": True,
            "recoverable_account_id": accounts["1450"].id,
        },
    )
    _purchase(client, "/api/bills", _vendor(client), accounts, 400, code)
    ledger = _report(client)[0]["ledger"]
    assert ledger["other_tax_names"] == ["QST"]


def test_quebec_is_known_from_the_tax_province(client, db_session, canada):
    from app.services.settings_service import set_setting

    set_setting(db_session, "company_state", "ON")
    set_setting(db_session, "tax_province", "QC")
    db_session.commit()
    data, _ = _report(client)
    assert data["files_with_revenu_quebec"] is True
    assert any("FPZ-500" in n for n in data["notes"])
