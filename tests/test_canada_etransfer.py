"""Interac e-Transfer as a way to send and receive money, for a Canadian
company (#456). The method is stored as given, so this is the menus."""

from pathlib import Path

JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js"


def test_every_money_in_and_out_menu_offers_it():
    for page, style in (
        ("payments.js", "? canadianMethodOptions() :"),
        ("sales_receipts.js", "? canadianMethodOptions() :"),
        ("bills.js", "? canadianMethodOptions('code') :"),
        ("batch_payments.js", "? canadianMethodOptions('code') :"),
    ):
        assert style in (JS / page).read_text(encoding="utf-8"), page


def test_only_a_canadian_company_is_offered_it():
    src = (JS / "utils.js").read_text(encoding="utf-8")
    start = src.index("function canadianMethodOptions(")
    assert "if (s.tax_country !== 'CA') return '';" in src[start : start + 600]


def test_a_payment_by_e_transfer_is_kept_as_such(client, seed_accounts, seed_customer):
    r = client.post(
        "/api/invoices",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-02",
            "lines": [{"description": "Sign", "quantity": 1, "rate": 100}],
        },
    )
    assert r.status_code == 201, r.text
    inv = r.json()
    r = client.post(
        "/api/payments",
        json={
            "customer_id": seed_customer.id,
            "date": "2026-07-03",
            "amount": 100,
            "method": "Interac e-Transfer",
            "reference": "CAxyz123",
            "allocations": [{"invoice_id": inv["id"], "amount": 100}],
        },
    )
    assert r.status_code in (200, 201), r.text
    assert r.json()["method"] == "Interac e-Transfer"
