"""Printed documents show a tax code's components (docs/design/canada.md).

A Canadian customer claims GST back separately from PST, so a document with
a tax code prints each component as its own total line ("GST (5.00%)
$10.00", "PST (7.00%) $14.00") in place of one Tax line, and the lines add
up to the tax the document stored. A sales document also prints the
company's GST/HST and QST registration numbers when they are set. A
document with no code, from a company with no numbers set (every US
company), prints exactly as it did.

The print preview is the PDF's own render, so most of these read its HTML;
the PDF test needs WeasyPrint and skips without it.
"""

import io
import re
from decimal import Decimal

import pytest

from app.services.settings_service import set_setting

DAY = "2026-07-02"


@pytest.fixture
def gst_pst(client, seed_accounts):
    r = client.post(
        "/api/tax-codes",
        json={
            "name": "BC GST + PST",
            "components": [
                {"name": "GST", "rate": 0.05},
                {"name": "PST", "rate": 0.07},
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def registered(db_session):
    set_setting(db_session, "gst_hst_registration_number", "123456789 RT0001")
    set_setting(db_session, "qst_registration_number", "1234567890 TQ0001")
    db_session.commit()


def _text(html):
    """The page's words without its markup, spaces collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _sale(client, path, customer_id, **extra):
    body = {
        "customer_id": customer_id,
        "date": DAY,
        "lines": [{"description": "Sign", "quantity": 1, "rate": 200}],
        **extra,
    }
    r = client.post(path, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    return out.get("invoice", out)


def _preview(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:300]
    return _text(r.text)


def _assert_components(text):
    assert "GST (5.00%) $10.00" in text, text
    assert "PST (7.00%) $14.00" in text, text
    assert "Tax (" not in text, text


def test_an_invoice_prints_each_component(client, seed_customer, gst_pst):
    inv = _sale(client, "/api/invoices", seed_customer.id, tax_code_id=gst_pst["id"])
    _assert_components(_preview(client, f"/api/invoices/{inv['id']}/print-preview"))


def test_a_sales_receipt_prints_each_component(client, seed_customer, gst_pst):
    inv = _sale(
        client, "/api/sales-receipts", seed_customer.id, tax_code_id=gst_pst["id"]
    )
    text = _preview(client, f"/api/invoices/{inv['id']}/print-preview")
    assert "SALES RECEIPT" in text
    _assert_components(text)


def test_an_estimate_prints_each_component(client, seed_customer, gst_pst):
    est = _sale(client, "/api/estimates", seed_customer.id, tax_code_id=gst_pst["id"])
    _assert_components(_preview(client, f"/api/estimates/{est['id']}/print-preview"))


def test_a_credit_memo_prints_each_component(client, seed_customer, gst_pst):
    cm = _sale(client, "/api/credit-memos", seed_customer.id, tax_code_id=gst_pst["id"])
    _assert_components(_preview(client, f"/api/credit-memos/{cm['id']}/print-preview"))


def test_a_purchase_order_prints_each_component(client, gst_pst):
    r = client.post("/api/vendors", json={"name": "Harbor Supply"})
    assert r.status_code in (200, 201), r.text
    r = client.post(
        "/api/purchase-orders",
        json={
            "vendor_id": r.json()["id"],
            "date": DAY,
            "tax_code_id": gst_pst["id"],
            "lines": [{"description": "Shelving", "quantity": 1, "rate": 200}],
        },
    )
    assert r.status_code == 201, r.text
    text = _preview(client, f"/api/purchase-orders/{r.json()['id']}/print-preview")
    _assert_components(text)


def test_a_bill_prints_each_component(client, seed_accounts, gst_pst):
    """The bill is the purchase whose GST is claimed back, so it breaks the
    tax out as the purchase order it may have come from does."""
    r = client.post("/api/vendors", json={"name": "Harbor Supply"})
    assert r.status_code in (200, 201), r.text
    r = client.post(
        "/api/bills",
        json={
            "vendor_id": r.json()["id"],
            "date": DAY,
            "tax_code_id": gst_pst["id"],
            "lines": [
                {
                    "description": "Shelving",
                    "quantity": 1,
                    "rate": 200,
                    "account_id": seed_accounts["6000"].id,
                }
            ],
        },
    )
    assert r.status_code == 201, r.text
    _assert_components(_preview(client, f"/api/bills/{r.json()['id']}/print-preview"))


def test_the_printed_components_add_up_to_the_stored_tax(
    client, db_session, seed_customer, gst_pst
):
    """A tax brought across as it was (an import) is shared over the
    components by rate, so the lines still come to what was charged."""
    from app.models.invoices import Invoice
    from app.services.pdf_service import document_tax_lines

    inv = _sale(client, "/api/invoices", seed_customer.id, tax_code_id=gst_pst["id"])
    doc = db_session.get(Invoice, inv["id"])
    doc.tax_amount = Decimal("24.01")
    db_session.flush()
    lines = document_tax_lines(doc)
    assert [ln["name"] for ln in lines] == ["GST", "PST"]
    assert sum(ln["amount"] for ln in lines) == Decimal("24.01")


def test_a_document_without_a_code_prints_one_tax_line(
    client, seed_accounts, seed_customer
):
    inv = _sale(client, "/api/invoices", seed_customer.id, tax_rate=0.0825)
    text = _preview(client, f"/api/invoices/{inv['id']}/print-preview")
    assert "Tax (8.25%) $16.50" in text, text
    assert "Registration No." not in text


def test_a_code_on_untaxed_lines_prints_no_tax_line(client, seed_customer, gst_pst):
    inv = _sale(
        client,
        "/api/invoices",
        seed_customer.id,
        tax_code_id=gst_pst["id"],
        lines=[
            {"description": "Labour", "quantity": 1, "rate": 50, "is_taxable": False}
        ],
    )
    text = _preview(client, f"/api/invoices/{inv['id']}/print-preview")
    assert "GST (" not in text and "Tax (" not in text, text


def test_sales_documents_print_the_registration_numbers(
    client, seed_customer, gst_pst, registered
):
    inv = _sale(client, "/api/invoices", seed_customer.id, tax_code_id=gst_pst["id"])
    est = _sale(client, "/api/estimates", seed_customer.id, tax_code_id=gst_pst["id"])
    cm = _sale(client, "/api/credit-memos", seed_customer.id, tax_code_id=gst_pst["id"])
    for path in (
        f"/api/invoices/{inv['id']}/print-preview",
        f"/api/estimates/{est['id']}/print-preview",
        f"/api/credit-memos/{cm['id']}/print-preview",
    ):
        text = _preview(client, path)
        assert "GST/HST Registration No. 123456789 RT0001" in text, (path, text)
        assert "QST Registration No. 1234567890 TQ0001" in text, (path, text)


def test_only_the_number_that_is_set_prints(
    client, db_session, seed_accounts, seed_customer
):
    set_setting(db_session, "gst_hst_registration_number", "123456789 RT0001")
    db_session.commit()
    inv = _sale(client, "/api/invoices", seed_customer.id)
    text = _preview(client, f"/api/invoices/{inv['id']}/print-preview")
    assert "GST/HST Registration No. 123456789 RT0001" in text
    assert "QST Registration No." not in text


def test_the_invoice_pdf_prints_the_components_and_number(
    client, seed_customer, gst_pst, registered
):
    pypdf = pytest.importorskip("pypdf")
    inv = _sale(client, "/api/invoices", seed_customer.id, tax_code_id=gst_pst["id"])
    r = client.get(f"/api/invoices/{inv['id']}/pdf")
    assert r.status_code == 200, r.text[:200]
    text = pypdf.PdfReader(io.BytesIO(r.content)).pages[0].extract_text()
    text = re.sub(r"\s+", " ", text)
    for value in ("GST (5.00%)", "$10.00", "PST (7.00%)", "$14.00"):
        assert value in text, (value, text)
    assert "GST/HST Registration No. 123456789 RT0001" in text, text


def test_a_zero_rated_sale_says_so(client, seed_customer, seed_accounts):
    r = client.post(
        "/api/tax-codes",
        json={"name": "Zero-rated", "components": [{"name": "GST", "rate": 0}]},
    )
    assert r.status_code == 201, r.text
    inv = _sale(client, "/api/invoices", seed_customer.id, tax_code_id=r.json()["id"])
    text = _preview(client, f"/api/invoices/{inv['id']}/print-preview")
    assert "Zero-rated (no tax charged) $0.00" in text
