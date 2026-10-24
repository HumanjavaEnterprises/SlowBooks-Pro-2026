"""Canadian company setup: the tax country, the registration numbers, the
province presets and the Canadian address labels (docs/design/canada.md).

A company is US until Settings say otherwise, and a US company sees exactly
what it saw before: no Canadian keys in its settings, no Canadian section
or labels on its pages, and the presets refuse it. The rates come from one
table, app/services/canada_tax.py.
"""

import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.accounts import Account, AccountType
from app.models.settings import DEFAULT_SETTINGS
from app.models.transactions import Transaction, TransactionLine
from app.services import canada_tax
from app.services.terminology import CANADA, Terms, terms_for

ROOT = Path(__file__).resolve().parent.parent
CANADA_KEYS = (
    "tax_country",
    "tax_province",
    "gst_hst_registration_number",
    "qst_registration_number",
    "default_tax_code_id",
)
PRESET = "/api/tax-codes/presets/canada"
DAY = "2026-07-02"


def _canadian(client, province=None):
    body = {"tax_country": "CA"}
    if province:
        body["tax_province"] = province
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _preset(client, province, **extra):
    r = client.post(PRESET, json={"province": province, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _codes(client):
    r = client.get("/api/tax-codes?include_archived=true")
    assert r.status_code == 200, r.text
    return {c["name"]: c for c in r.json()}


def _account(db_session, number):
    db_session.expire_all()
    return db_session.query(Account).filter_by(account_number=number).one()


# ── Settings ─────────────────────────────────────────────────────────────


def test_a_us_company_s_settings_read_as_they_did(client):
    before = client.get("/api/settings").json()
    for key in CANADA_KEYS:
        assert key not in before
    # every other setting is there, as it was
    assert set(before) >= set(DEFAULT_SETTINGS) - set(CANADA_KEYS)

    saved = client.put("/api/settings", json={"company_name": "Boise Neon"}).json()
    assert set(saved) == set(before)
    assert not set(CANADA_KEYS) & set(client.get("/api/settings").json())


def test_a_canadian_company_reads_its_tax_settings(client):
    s = _canadian(client, "on")
    assert s["tax_country"] == "CA"
    assert s["tax_province"] == "ON"
    assert s["gst_hst_registration_number"] == ""
    assert s["qst_registration_number"] == ""
    back = client.put("/api/settings", json={"tax_country": "US"}).json()
    assert not set(CANADA_KEYS) & set(back)


def test_the_tax_country_is_us_or_ca(client):
    r = client.put("/api/settings", json={"tax_country": "MX"})
    assert r.status_code == 422
    assert "tax_country must be one of: CA, US" in r.text


@pytest.mark.parametrize(
    "typed, stored",
    [
        ("123456789RT0001", "123456789RT0001"),
        ("123456789 rt 0001", "123456789RT0001"),
        ("123456789-RT-0001", "123456789RT0001"),
        ("", ""),
    ],
)
def test_a_gst_hst_number_is_stored_in_one_piece(client, typed, stored):
    _canadian(client)
    r = client.put("/api/settings", json={"gst_hst_registration_number": typed})
    assert r.status_code == 200, r.text
    assert r.json()["gst_hst_registration_number"] == stored


@pytest.mark.parametrize(
    "typed",
    ["123456789", "12345678RT0001", "123456789RC0001", "123456789RT001", "BN"],
)
def test_a_gst_hst_number_that_cannot_be_one_is_refused(client, typed):
    _canadian(client)
    r = client.put(
        "/api/settings",
        json={"gst_hst_registration_number": typed, "company_name": "Maple Signs"},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "9-digit business number, RT and the 4-digit account number" in detail
    assert "123456789RT0001" in detail and "Nothing was saved" in detail
    assert client.get("/api/settings").json()["company_name"] != "Maple Signs"


def test_a_qst_number_is_checked(client):
    _canadian(client, "QC")
    ok = client.put(
        "/api/settings", json={"qst_registration_number": "1234567890 TQ 0001"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["qst_registration_number"] == "1234567890TQ0001"
    bad = client.put(
        "/api/settings", json={"qst_registration_number": "123456789RT0001"}
    )
    assert bad.status_code == 422
    assert "10 digits, TQ and 4 digits, like 1234567890TQ0001" in bad.json()["detail"]


def test_a_province_is_a_province(client):
    _canadian(client)
    r = client.put("/api/settings", json={"tax_province": "XX"})
    assert r.status_code == 422
    assert "two-letter code of a province or territory" in r.json()["detail"]


# ── Presets ──────────────────────────────────────────────────────────────


def test_a_us_company_gets_no_canadian_codes(client, seed_accounts):
    r = client.post(PRESET, json={"province": "ON"})
    assert r.status_code == 400
    assert "tax country is Canada" in r.json()["detail"]
    assert _codes(client) == {}


def test_an_unknown_province_is_named(client, seed_accounts):
    _canadian(client)
    r = client.post(PRESET, json={"province": "ZZ"})
    assert r.status_code == 400
    assert "not a province or territory code" in r.json()["detail"]
    assert "ON" in r.json()["detail"]


def test_ontario_gets_one_hst_account_that_takes_its_itcs_too(
    client, db_session, seed_accounts
):
    _canadian(client, "ON")
    out = _preset(client, "ON")
    assert out["province_name"] == "Ontario"
    assert out["rates_verified"] == canada_tax.RATES_VERIFIED
    assert [c["name"] for c in out["created"]["tax_codes"]] == [
        "HST ON 13%",
        "HST ON 13% (no ITC)",
        "Zero-rated",
        "Exempt",
    ]
    assert [a["account_number"] for a in out["created"]["accounts"]] == ["2250"]
    hst_account = _account(db_session, "2250")
    assert hst_account.name == "GST/HST Payable"
    assert hst_account.account_type == AccountType.LIABILITY

    codes = _codes(client)
    (hst,) = codes["HST ON 13%"]["components"]
    assert hst["name"] == "HST" and Decimal(hst["rate"]) == Decimal("0.13")
    assert hst["payable_account_id"] == hst_account.id
    assert hst["is_recoverable"] is True
    assert hst["recoverable_account_id"] == hst_account.id
    assert not codes["HST ON 13%"]["is_archived"]
    (zero,) = codes["Zero-rated"]["components"]
    assert zero["name"] == "HST" and Decimal(zero["rate"]) == 0
    (exempt,) = codes["Exempt"]["components"]
    assert Decimal(exempt["rate"]) == 0 and exempt["is_recoverable"] is False


def test_running_it_twice_makes_nothing_new_and_says_what_was_there(
    client, db_session, seed_accounts
):
    _canadian(client, "QC")
    first = _preset(client, "QC")
    accounts_before = db_session.query(Account).count()

    again = _preset(client, "QC")
    assert again["created"] == {"accounts": [], "tax_codes": []}
    assert [c["name"] for c in again["existing"]["tax_codes"]] == [
        c["name"] for c in first["created"]["tax_codes"]
    ]
    assert [a["account_number"] for a in again["existing"]["accounts"]] == [
        "2250",
        "2270",
    ]
    assert db_session.query(Account).count() == accounts_before
    assert len(_codes(client)) == len(first["created"]["tax_codes"])


def test_quebec_claims_gst_and_qst_back_each_into_its_own_account(
    client, db_session, seed_accounts
):
    _canadian(client, "QC")
    out = _preset(client, "QC")
    assert [c["name"] for c in out["created"]["tax_codes"]] == [
        "GST + QST QC 14.975%",
        "GST + QST QC 14.975% (no ITC)",
        "GST 5%",
        "Zero-rated",
        "Exempt",
    ]
    gst_acct, qst_acct = _account(db_session, "2250"), _account(db_session, "2270")
    assert qst_acct.name == "QST Payable"
    gst, qst = _codes(client)["GST + QST QC 14.975%"]["components"]
    assert (gst["name"], Decimal(gst["rate"])) == ("GST", Decimal("0.05"))
    assert (qst["name"], Decimal(qst["rate"])) == ("QST", Decimal("0.09975"))
    assert gst["payable_account_id"] == gst["recoverable_account_id"] == gst_acct.id
    assert qst["payable_account_id"] == qst["recoverable_account_id"] == qst_acct.id
    assert gst["is_recoverable"] and qst["is_recoverable"]


def test_a_quebec_sale_and_purchase_post_to_the_preset_accounts(
    client, db_session, seed_accounts, seed_customer
):
    _canadian(client, "QC")
    _preset(client, "QC")
    code = _codes(client)["GST + QST QC 14.975%"]
    inv = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "tax_code_id": code["id"],
            "lines": [{"description": "Sign", "quantity": 1, "rate": 100}],
        },
    )
    assert inv.status_code == 201, inv.text
    # each tax rounded on its own: 5.00 + 9.98 (9.975)
    assert Decimal(str(inv.json()["tax_amount"])) == Decimal("14.98")

    vendor = client.post("/api/vendors", json={"name": "Fournitures Laval"}).json()
    bill = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor["id"],
            "date": DAY,
            "tax_code_id": code["id"],
            "lines": [
                {
                    "description": "Vinyl",
                    "quantity": 1,
                    "rate": 40,
                    "account_id": seed_accounts["6000"].id,
                }
            ],
        },
    )
    assert bill.status_code == 201, bill.text

    db_session.expire_all()
    gst_acct, qst_acct = _account(db_session, "2250"), _account(db_session, "2270")

    def net(account, source_type):
        lines = (
            db_session.query(TransactionLine)
            .join(Transaction, TransactionLine.transaction_id == Transaction.id)
            .filter(
                TransactionLine.account_id == account.id,
                Transaction.source_type == source_type,
            )
            .all()
        )
        return sum((ln.credit - ln.debit for ln in lines), Decimal("0"))

    assert net(gst_acct, "invoice") == Decimal("5.00")
    assert net(qst_acct, "invoice") == Decimal("9.98")
    # the bill's GST and QST are claimed back, not part of the vinyl's cost
    assert net(gst_acct, "bill") == Decimal("-2.00")
    assert net(qst_acct, "bill") == Decimal("-3.99")


def test_bc_pst_is_owed_but_never_claimed_back(client, db_session, seed_accounts):
    _canadian(client, "BC")
    out = _preset(client, "BC")
    assert [a["name"] for a in out["created"]["accounts"]] == [
        "GST/HST Payable",
        "PST Payable",
    ]
    gst, pst = _codes(client)["GST + PST BC 12%"]["components"]
    assert gst["is_recoverable"] is True
    assert pst["name"] == "PST" and Decimal(pst["rate"]) == Decimal("0.07")
    assert pst["payable_account_id"] == _account(db_session, "2260").id
    assert pst["is_recoverable"] is False and pst["recoverable_account_id"] is None
    assert any("PST is not claimed back" in n for n in out["notes"])


def test_manitoba_calls_it_rst(client, db_session, seed_accounts):
    _canadian(client, "MB")
    out = _preset(client, "MB")
    assert "GST + RST MB 12%" in [c["name"] for c in out["created"]["tax_codes"]]
    assert _account(db_session, "2260").name == "RST Payable"


def test_a_gst_only_territory_gets_gst_and_nothing_provincial(
    client, db_session, seed_accounts
):
    _canadian(client, "YT")
    out = _preset(client, "YT")
    assert [c["name"] for c in out["created"]["tax_codes"]] == [
        "GST 5%",
        "GST 5% (no ITC)",
        "Zero-rated",
        "Exempt",
    ]
    assert [a["account_number"] for a in out["created"]["accounts"]] == ["2250"]
    # nothing to note but the default it set
    assert out["notes"] == ["New documents now start on 'GST 5%'."]
    (gst,) = _codes(client)["GST 5%"]["components"]
    assert (gst["name"], Decimal(gst["rate"])) == ("GST", Decimal("0.05"))
    assert _codes(client)["Zero-rated"]["components"][0]["name"] == "GST"


def test_nova_scotia_is_14_percent_and_offers_the_15_percent_code(
    client, seed_accounts
):
    _canadian(client, "NS")
    out = _preset(client, "NS")
    names = [c["name"] for c in out["created"]["tax_codes"]]
    assert names[0] == "HST NS 14%"
    assert canada_tax.NS_BEFORE_NAME not in names
    assert any("2025-04-01" in n and "15%" in n for n in out["notes"])

    more = _preset(client, "NS", include_before_2025_04_01=True)
    assert [c["name"] for c in more["created"]["tax_codes"]] == [
        "HST NS 15% (before 2025-04-01)"
    ]
    old = _codes(client)["HST NS 15% (before 2025-04-01)"]
    assert Decimal(old["rate"]) == Decimal("0.15") and old["is_archived"] is False
    assert not any("need the 15% code" in n for n in more["notes"])


def test_the_15_percent_code_is_nova_scotia_s_only(client, seed_accounts):
    _canadian(client, "ON")
    out = _preset(client, "ON", include_before_2025_04_01=True)
    assert canada_tax.NS_BEFORE_NAME not in [
        c["name"] for c in out["created"]["tax_codes"]
    ]


def test_an_account_already_on_the_number_is_used(client, db_session, seed_accounts):
    db_session.add(
        Account(
            account_number="2250",
            name="GST/HST Owing",
            account_type=AccountType.LIABILITY,
            balance=Decimal(0),
        )
    )
    db_session.commit()
    _canadian(client, "AB")
    out = _preset(client, "AB")
    assert out["created"]["accounts"] == []
    assert out["existing"]["accounts"][0]["name"] == "GST/HST Owing"


def test_a_number_held_by_another_kind_of_account_is_left_to_it(
    client, db_session, seed_accounts
):
    db_session.add(
        Account(
            account_number="2250",
            name="Something Else",
            account_type=AccountType.ASSET,
            balance=Decimal(0),
        )
    )
    db_session.commit()
    _canadian(client, "AB")
    out = _preset(client, "AB")
    (made,) = out["created"]["accounts"]
    assert made["name"] == "GST/HST Payable" and made["account_number"] is None


def test_a_code_of_the_same_name_is_left_as_it_is_and_said(client, seed_accounts):
    _canadian(client, "ON")
    r = client.post(
        "/api/tax-codes",
        json={"name": "HST ON 13%", "components": [{"name": "HST", "rate": 0.12}]},
    )
    assert r.status_code == 201, r.text
    out = _preset(client, "ON")
    assert "HST ON 13%" in [c["name"] for c in out["existing"]["tax_codes"]]
    assert any(
        "'HST ON 13%' was already here at 12%, not 13%" in n for n in out["notes"]
    )
    assert Decimal(_codes(client)["HST ON 13%"]["rate"]) == Decimal("0.12")


def test_every_rate_is_one_both_researchers_gave():
    # the table as checked on RATES_VERIFIED; a change here is a new rate
    combined = {
        code: sum((c.rate for c in p.components), Decimal("0"))
        for code, p in canada_tax.PROVINCES.items()
    }
    assert combined == {
        "AB": Decimal("0.05"),
        "BC": Decimal("0.12"),
        "MB": Decimal("0.12"),
        "NB": Decimal("0.15"),
        "NL": Decimal("0.15"),
        "NS": Decimal("0.14"),
        "NT": Decimal("0.05"),
        "NU": Decimal("0.05"),
        "ON": Decimal("0.13"),
        "PE": Decimal("0.15"),
        "QC": Decimal("0.14975"),
        "SK": Decimal("0.11"),
        "YT": Decimal("0.05"),
    }


# ── Words ────────────────────────────────────────────────────────────────


def _js_canada() -> dict:
    src = (ROOT / "app/static/js/terms.js").read_text(encoding="utf-8")
    m = re.search(r"const TERMS_CANADA = (\{.*?\});", src, re.S)
    assert m, "TERMS_CANADA literal not found in terms.js"
    return json.loads(m.group(1))


def test_js_and_python_canadian_labels_agree():
    assert _js_canada() == CANADA


def test_canadian_labels_are_exact_and_us_ones_unchanged():
    ca = Terms("business", "CA")
    assert ca("State") == "Province"
    assert ca("ZIP") == "Postal Code"
    assert ca("Tax ID / EIN") == "Business Number (BN)"
    # never inside a sentence: payroll's state tax stays a state tax
    assert ca.text("State withholding") == "State withholding"
    assert ca("Invoice") == "Invoice"
    us = Terms("business")
    assert us("State") == "State" and us("ZIP") == "ZIP"
    assert Terms("business", "FR").country == "US"
    # a Canadian nonprofit gets both
    both = terms_for({"company_type": "nonprofit", "tax_country": "CA"})
    assert both("State") == "Province" and both("Invoice") == "Pledge"


# ── Pages ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_settings_page_shows_canada_only_to_a_canadian_company():
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "settings_canada_probe.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        cwd=ROOT,
    )
    assert out.returncode == 0, out.stderr
    pages = json.loads(out.stdout)

    us = pages["us"]
    for word in (
        "Canadian Sales Tax",
        "GST/HST Registration Number",
        "QST Registration Number",
        "Province",
        "Postal Code",
        "Business Number",
        "Set up",
        "tax-presets-result",
    ):
        assert word not in us, word
    assert "<label>State</label>" in us and "<label>ZIP</label>" in us
    assert "<label>Tax ID / EIN</label>" in us
    # the one thing new on a US page: the country it can switch to
    assert '<option value="US" selected>United States</option>' in us

    qc = pages["qc"]
    assert "Canadian Sales Tax" in qc
    assert '<option value="QC" selected>Quebec</option>' in qc
    assert "Set up Quebec sales tax codes" in qc
    assert "<label>Province</label>" in qc and "<label>Postal Code</label>" in qc
    assert "<label>Business Number (BN)</label>" in qc
    assert "dated before 2025-04-01" not in qc

    assert "Set up Nova Scotia sales tax codes" in pages["ns"]
    assert "Also add the 15% HST code" in pages["ns"]
    assert "Choose the province under Canadian Sales Tax" in pages["no_province"]

    assert pages["posted"] == {
        "url": "/tax-codes/presets/canada",
        "body": {
            "province": "QC",
            "include_before_2025_04_01": False,
            "include_other_provinces": False,
        },
    }
    assert "Tax codes added: GST + QST QC 14.975%." in pages["result"]


# ── Found by the independent check ───────────────────────────────────────


def test_a_deactivated_account_on_the_number_is_not_brought_back(
    client, db_session, seed_accounts
):
    db_session.add(
        Account(
            account_number="2250",
            name="Old GST",
            account_type=AccountType.LIABILITY,
            balance=Decimal(0),
            is_active=False,
        )
    )
    db_session.commit()
    _canadian(client, "AB")
    out = _preset(client, "AB")
    (made,) = out["created"]["accounts"]
    assert made["name"] != "Old GST" and made["account_number"] is None


def test_the_tax_country_stays_once_a_document_carries_a_code(
    client, seed_accounts, seed_customer
):
    _canadian(client, "ON")
    # still free to change while nothing is taxed under it
    assert client.put("/api/settings", json={"tax_country": "US"}).status_code == 200
    _canadian(client, "ON")
    _preset(client, "ON")
    code = _codes(client)["HST ON 13%"]
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": DAY,
            "tax_code_id": code["id"],
            "lines": [{"description": "Sign", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    r = client.put("/api/settings", json={"tax_country": "US"})
    assert r.status_code == 409 and "stays as it is" in r.json()["detail"]
    # saving other settings with the same country is untouched
    assert client.put("/api/settings", json={"tax_country": "CA"}).status_code == 200


def test_a_province_typed_with_spaces_is_read(client, seed_accounts):
    _canadian(client, "ON")
    r = client.post(PRESET, json={"province": " on "})
    assert r.status_code == 200, r.text


def test_the_presets_follow_the_province_saved_and_take_one_click_at_a_time():
    src = (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
    assert "presets.innerHTML = SettingsPage._canadaPresets(saved)" in src
    assert "b.disabled = true" in src and "finally { buttons.forEach" in src


def test_the_preset_makes_its_province_code_the_default(client, seed_accounts):
    _canadian(client, "ON")
    out = _preset(client, "ON")
    code = _codes(client)["HST ON 13%"]
    assert client.get("/api/settings").json()["default_tax_code_id"] == str(code["id"])
    assert any("start on 'HST ON 13%'" in n for n in out["notes"])
    # a default already chosen is left as it is
    gst = _codes(client)["Zero-rated"]
    r = client.put("/api/settings", json={"default_tax_code_id": str(gst["id"])})
    assert r.status_code == 200, r.text
    _preset(client, "ON")
    assert client.get("/api/settings").json()["default_tax_code_id"] == str(gst["id"])


def test_the_default_tax_code_must_be_a_current_one(client, seed_accounts):
    _canadian(client, "ON")
    r = client.put("/api/settings", json={"default_tax_code_id": "999"})
    assert r.status_code == 422 and "current tax codes" in r.json()["detail"]
    assert (
        client.put("/api/settings", json={"default_tax_code_id": ""}).status_code == 200
    )


def test_a_us_company_never_sees_the_default_tax_code(client):
    assert "default_tax_code_id" not in client.get("/api/settings").json()


def test_a_blank_gst_hst_number_is_pointed_out():
    src = (ROOT / "app/static/js/settings.js").read_text(encoding="utf-8")
    assert "No GST/HST registration number yet." in src


def test_the_no_itc_code_puts_the_tax_in_the_cost(client, db_session, seed_accounts):
    """A dental clinic's supplies (inputs to exempt sales): the vendor
    charged HST, the clinic can't claim it, so it is part of the cost."""
    _canadian(client, "ON")
    _preset(client, "ON")
    code = _codes(client)["HST ON 13% (no ITC)"]
    assert [c["is_recoverable"] for c in code["components"]] == [False]
    vendor = client.post("/api/vendors", json={"name": "Dental Supply"}).json()
    r = client.post(
        "/api/bills",
        json={
            "vendor_id": vendor["id"],
            "date": DAY,
            "tax_code_id": code["id"],
            "lines": [
                {
                    "description": "Gloves",
                    "quantity": 1,
                    "rate": 100,
                    "account_id": seed_accounts["6000"].id,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["total"] in (113, 113.0, "113.00")
    assert _account(db_session, "2250").balance == 0


def test_other_provinces_codes_for_shipping_out_of_province(client, seed_accounts):
    _canadian(client, "AB")
    out = _preset(client, "AB", include_other_provinces=True)
    names = [c["name"] for c in out["created"]["tax_codes"]]
    for want in ("HST ON 13%", "HST NS 14%", "HST NB 15%", "HST NL 15%", "HST PE 15%"):
        assert want in names
    assert names.count("GST 5%") == 1
    hst_ns = _codes(client)["HST NS 14%"]
    (comp,) = hst_ns["components"]
    assert comp["is_recoverable"] and comp["level"] == "federal"


def test_a_canadian_company_keeps_its_books_in_canadian_dollars(client, db_session):
    from app.services.currency import home_currency

    _canadian(client, "QC")
    db_session.expire_all()
    assert home_currency(db_session) == "CAD"
    client.put("/api/settings", json={"tax_country": "US"})
    db_session.expire_all()
    assert home_currency(db_session) == "USD"
