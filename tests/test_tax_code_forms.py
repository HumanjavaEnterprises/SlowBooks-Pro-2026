"""The Tax Code picker on every tax-bearing form (#456).

The invoice form took a tax code first; the sales receipt, estimate, credit
memo, recurring schedule, purchase order, vendor credit and bill forms now
take one the same way. Each form here is opened against real API answers
(tests/js/tax_code_forms_probe.js), and what it showed and sent is held to
what the server does with it: a form that rounded GST + PST as one 12% rate
would show $1.21 on $10.10 where the books post $1.22.

A company with no codes sees no picker and sends no tax_code_id, as before
codes existed; a form whose codes could not be read sends none either, so
the edit keeps the code its document has.
"""

import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.accounts import Account, AccountType

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-07-02"
TEN = {"description": "Shelving", "quantity": 1, "rate": 10.10}

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _ok(r):
    assert r.status_code in (200, 201), r.text
    return r.json()


def _money(value):
    return Decimal(str(value))


def _code(client, name, *components):
    return _ok(
        client.post(
            "/api/tax-codes", json={"name": name, "components": list(components)}
        )
    )


@pytest.fixture
def books(client, db_session, seed_accounts, seed_customer):
    itc = Account(
        account_number="1450",
        name="GST/HST Input Tax Credits",
        account_type=AccountType.ASSET,
        balance=Decimal(0),
    )
    db_session.add(itc)
    db_session.commit()

    def gst():
        return {
            "name": "GST",
            "rate": 0.05,
            "is_recoverable": True,
            "recoverable_account_id": itc.id,
        }

    gst_pst = _code(client, "GST + PST", gst(), {"name": "PST", "rate": 0.07})
    hst = _code(
        client,
        "HST",
        {
            "name": "HST",
            "rate": 0.13,
            "is_recoverable": True,
            "recoverable_account_id": itc.id,
        },
    )
    gst_qst = _code(
        client,
        "GST + QST",
        gst(),
        {
            "name": "QST",
            "rate": 0.09975,
            "is_recoverable": True,
            "recoverable_account_id": itc.id,
        },
    )
    vendor = _ok(client.post("/api/vendors", json={"name": "Harbour Supply"}))
    expense = seed_accounts["6000"].id
    bill_line = dict(TEN, account_id=expense)
    customer = {"customer_id": seed_customer.id}

    estimate = _ok(
        client.post(
            "/api/estimates",
            json={
                **customer,
                "date": DAY,
                "tax_code_id": gst_pst["id"],
                "lines": [TEN],
            },
        )
    )
    recurring = _ok(
        client.post(
            "/api/recurring",
            json={
                **customer,
                "frequency": "monthly",
                "start_date": DAY,
                "tax_code_id": hst["id"],
                "lines": [TEN],
            },
        )
    )
    po = _ok(
        client.post(
            "/api/purchase-orders",
            json={
                "vendor_id": vendor["id"],
                "date": DAY,
                "tax_code_id": gst_pst["id"],
                "lines": [TEN],
            },
        )
    )
    bill = _ok(
        client.post(
            "/api/bills",
            json={
                "vendor_id": vendor["id"],
                "date": DAY,
                "tax_code_id": gst_pst["id"],
                "lines": [bill_line],
            },
        )
    )
    rated = _ok(
        client.post(
            "/api/bills",
            json={
                "vendor_id": vendor["id"],
                "date": DAY,
                "tax_rate": 0.05,
                "lines": [bill_line],
            },
        )
    )
    # an invoice charged under GST + QST, whose code has since been archived
    invoice = _ok(
        client.post(
            "/api/invoices",
            json={
                **customer,
                "date": DAY,
                "tax_code_id": gst_qst["id"],
                "lines": [TEN],
            },
        )
    )
    _ok(client.put(f"/api/tax-codes/{gst_qst['id']}", json={"is_archived": True}))

    given = {
        "day": DAY,
        "customerId": seed_customer.id,
        "vendorId": vendor["id"],
        "accountId": expense,
        "gstPst": gst_pst["id"],
        "codes": client.get("/api/tax-codes?include_archived=true").json(),
        "vendors": client.get("/api/vendors").json(),
        "estimate": client.get(f"/api/estimates/{estimate['id']}").json(),
        "recurring": client.get(f"/api/recurring/{recurring['id']}").json(),
        "po": client.get(f"/api/purchase-orders/{po['id']}").json(),
        "bill": client.get(f"/api/bills/{bill['id']}").json(),
        "ratedBill": client.get(f"/api/bills/{rated['id']}").json(),
        "archivedInvoice": client.get(f"/api/invoices/{invoice['id']}").json(),
    }
    out = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "tax_code_forms_probe.js")],
        cwd=ROOT,
        input=json.dumps(given),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return {
        "shown": json.loads(out.stdout),
        "codes": {"gst_pst": gst_pst["id"], "hst": hst["id"], "gst_qst": gst_qst["id"]},
        "ids": {
            k: given[k]["id"]
            for k in ("estimate", "recurring", "po", "bill", "ratedBill")
        },
    }


def _sent(case, path):
    assert case["sent"] is not None, "the form sent nothing"
    assert case["sent"]["path"] == path
    return case["sent"]["data"]


def test_a_new_form_offers_the_current_codes_and_none(books):
    shown, codes = books["shown"], books["codes"]
    current = ["", str(codes["gst_pst"]), str(codes["hst"])]
    for key in ("srOpened", "cmOpened", "billOpened"):
        assert shown[key]["picker"]["options"] == current, key  # not the archived one
        assert shown[key]["picker"]["selected"] == "", key
    assert shown["srOpened"]["rate"]["readOnly"] is False
    assert shown["billOpened"]["picker"]["labels"][0] == "None"


def test_a_sales_receipt_taxes_each_component_and_posts_what_it_showed(client, books):
    case = books["shown"]["sr"]
    assert case["rate"] == {"value": "12", "readOnly": True}
    # 5% of $10.10 is $0.51 and 7% is $0.71: $1.22, where 12% at once is $1.21
    assert case["tax"] == "$1.22"
    inv = _ok(client.post("/api/sales-receipts", json=_sent(case, "/sales-receipts")))[
        "invoice"
    ]
    assert inv["tax_code_id"] == books["codes"]["gst_pst"]
    assert _money(inv["tax_amount"]) == Decimal("1.22")


@pytest.mark.parametrize(
    "key, path, code, tax",
    [
        ("estimate", "estimates", "gst_pst", "1.22"),
        ("recurring", "recurring", "hst", "1.31"),
        ("po", "purchase-orders", "gst_pst", "1.22"),
    ],
)
def test_an_edit_that_leaves_the_picker_keeps_the_code(
    client, books, key, path, code, tax
):
    case, doc_id = books["shown"][key], books["ids"][key]
    assert case["picker"]["selected"] == str(books["codes"][code])
    assert case["rate"]["readOnly"] is True
    data = _sent(case, f"/{path}/{doc_id}")
    assert data["tax_code_id"] == books["codes"][code]
    saved = _ok(client.put(f"/api/{path}/{doc_id}", json=data))
    assert saved["tax_code_id"] == books["codes"][code]
    if "tax_amount" in saved:
        assert case["tax"] == f"${tax}" and _money(saved["tax_amount"]) == Decimal(tax)
    else:  # a schedule stores no totals: the form's figure is the run's
        assert case["tax"] == f"${tax}"


def test_a_credit_memo_takes_the_invoices_code_even_archived(client, books):
    case, qst = books["shown"]["cm"], books["codes"]["gst_qst"]
    # put on the picker, which did not offer it, and chosen
    assert case["picker"]["selected"] == str(qst) and case["picker"]["offered"]
    assert case["rate"] == {"value": "14.975", "readOnly": True}
    assert case["tax"] == "$1.52"  # $0.51 + $1.01; 14.975% at once is $1.51
    data = _sent(case, "/credit-memos")
    assert data["tax_code_id"] == qst
    cm = _ok(client.post("/api/credit-memos", json=data))
    assert cm["tax_code_id"] == qst and _money(cm["tax_amount"]) == Decimal("1.52")


def test_a_vendor_credit_rounds_each_component_as_the_server_does(client, books):
    case = books["shown"]["vc"]
    assert case["tax"] == "$1.22"
    vc = _ok(client.post("/api/vendor-credits", json=_sent(case, "/vendor-credits")))
    assert vc["tax_code_id"] == books["codes"]["gst_pst"]
    assert _money(vc["tax_amount"]) == Decimal("1.22")


def test_a_bill_takes_a_code_and_shows_what_it_claims_back(client, books):
    opened, case = books["shown"]["billOpened"], books["shown"]["bill"]
    # no code yet: the total alone, as a bill without tax shows it
    assert opened["taxHidden"] is True and opened["recoverableHidden"] is True
    assert case["taxHidden"] is False
    assert (case["taxLabel"], case["tax"]) == ("Tax (GST + PST)", "$1.22")
    assert (
        case["recoverableHidden"],
        case["recoverableLabel"],
        case["recoverable"],
    ) == (
        False,
        "Recoverable (GST)",
        "$0.51",
    )
    assert case["total"] == "$11.32"
    bill = _ok(client.post("/api/bills", json=_sent(case, "/bills")))
    assert bill["tax_code_id"] == books["codes"]["gst_pst"]
    assert (_money(bill["tax_amount"]), _money(bill["total"])) == (
        Decimal("1.22"),
        Decimal("11.32"),
    )


def test_a_bill_edit_keeps_its_code_and_taking_it_off_takes_its_tax(client, books):
    shown, bill_id, gst_pst = (
        books["shown"],
        books["ids"]["bill"],
        books["codes"]["gst_pst"],
    )
    kept = shown["billKept"]
    assert kept["picker"]["selected"] == str(gst_pst)
    assert (kept["tax"], kept["recoverable"]) == ("$1.22", "$0.51")
    data = _sent(kept, f"/bills/{bill_id}")
    assert data["tax_code_id"] == gst_pst and "tax_rate" not in data
    saved = _ok(client.put(f"/api/bills/{bill_id}", json=data))
    assert saved["tax_code_id"] == gst_pst and _money(saved["tax_amount"]) == Decimal(
        "1.22"
    )

    cleared = shown["billCleared"]
    assert cleared["taxHidden"] is True and cleared["total"] == "$10.10"
    data = _sent(cleared, f"/bills/{bill_id}")
    assert data["tax_code_id"] is None and data["tax_rate"] == 0
    saved = _ok(client.put(f"/api/bills/{bill_id}", json=data))
    assert saved["tax_code_id"] is None
    assert (_money(saved["tax_amount"]), _money(saved["total"])) == (
        Decimal("0"),
        Decimal("10.10"),
    )


def test_a_bill_with_its_own_rate_keeps_it(client, books):
    case, bill_id = books["shown"]["ratedBill"], books["ids"]["ratedBill"]
    assert case["picker"]["labels"][0] == "None (this bill's 5%)"
    assert case["picker"]["selected"] == ""
    assert (case["taxLabel"], case["tax"]) == ("Tax (5%)", "$0.51")
    data = _sent(case, f"/bills/{bill_id}")
    assert data["tax_code_id"] is None and "tax_rate" not in data
    saved = _ok(client.put(f"/api/bills/{bill_id}", json=data))
    assert saved["tax_code_id"] is None
    assert (_money(saved["tax_rate"]), _money(saved["tax_amount"])) == (
        Decimal("0.05"),
        Decimal("0.51"),
    )


@pytest.mark.parametrize("key", ["noCodes", "unread"])
def test_without_codes_there_is_no_picker_and_nothing_is_cleared(client, books, key):
    got, ids, gst_pst = books["shown"][key], books["ids"], books["codes"]["gst_pst"]
    est = got["estimate"]
    assert est["picker"] is None and est["rate"]["readOnly"] is False
    data = _sent(est, f"/estimates/{ids['estimate']}")
    assert "tax_code_id" not in data
    saved = _ok(client.put(f"/api/estimates/{ids['estimate']}", json=data))
    assert saved["tax_code_id"] == gst_pst

    new_bill = got["bill"]
    assert new_bill["picker"] is None and new_bill["tax"] is None
    assert "tax_code_id" not in _sent(new_bill, "/bills")

    kept = got["billKept"]
    data = _sent(kept, f"/bills/{ids['bill']}")
    assert "tax_code_id" not in data and "tax_rate" not in data
    # the form shows the stored rate when it cannot read the code
    assert (kept["taxLabel"], kept["tax"]) == ("Tax (12%)", "$1.21")
    saved = _ok(client.put(f"/api/bills/{ids['bill']}", json=data))
    assert saved["tax_code_id"] == gst_pst and _money(saved["tax_amount"]) == Decimal(
        "1.22"
    )
