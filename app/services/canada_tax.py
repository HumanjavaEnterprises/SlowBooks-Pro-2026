# ============================================================================
# Canadian sales tax: the rate table and the province presets
#
# Only a company whose Settings say tax_country = CA sees any of this
# (docs/design/canada.md: the country is chosen once, and a US company sees
# exactly what it saw before). The preset turns a province into the tax
# codes a Canadian bookkeeper expects to find: the province's combined code,
# GST alone where a provincial tax sits beside it, Zero-rated and Exempt.
#
# GST and HST, and the input tax credits claimed back against them, share
# ONE liability account, as Canadian charts keep it: the GST/HST return
# nets the two, and so does the account. QST is reported to Revenu Québec
# on its own lines and is claimed back as input tax refunds, so it has its
# own account. PST and RST are never claimed back: on a purchase they are
# part of what the lines cost.
#
# The rates are data. They were checked on 2026-10-23 by two researchers
# working apart, who agreed on every rate, against:
#   - CRA, GST/HST rates by province ("Charge and collect the tax: which
#     rate to charge" and the GST/HST calculator), canada.ca
#   - Revenu Québec, tables of GST and QST rates, revenuquebec.ca
#   - BC Ministry of Finance, PST, gov.bc.ca
#   - Manitoba Finance, Retail Sales Tax, gov.mb.ca
#   - Saskatchewan, Provincial Sales Tax, saskatchewan.ca
# A rate that changes is a new row here and, in the books, a new code with
# the old one archived: codes are not date-effective (app/models/tax_codes.py).
# ============================================================================

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

RATES_VERIFIED = "2026-10-23"

GST = Decimal("0.05")

# What each kind of component is owed to, and whether it is claimed back.
GST_HST, PST, QST = "gst_hst", "pst", "qst"

# (number, name) of the liability each kind posts to. 2250-2270 sit beside
# Sales Tax Payable (2200) and are not used by app/seed/chart_of_accounts.py.
ACCOUNTS = {
    GST_HST: ("2250", "GST/HST Payable"),
    QST: ("2270", "QST Payable"),
}
# PST is "RST" in Manitoba; the account takes the province's own word.
PST_ACCOUNT_NUMBER = "2260"


@dataclass(frozen=True)
class Component:
    name: str  # what a receipt prints: "GST", "HST", "PST", "RST", "QST"
    rate: Decimal  # a fraction
    kind: str  # GST_HST | PST | QST


@dataclass(frozen=True)
class Province:
    code: str
    name: str
    components: tuple[Component, ...]


def _gst_plus(name: str, rate: str, kind: str) -> tuple[Component, ...]:
    return (Component("GST", GST, GST_HST), Component(name, Decimal(rate), kind))


def _hst(rate: str) -> tuple[Component, ...]:
    return (Component("HST", Decimal(rate), GST_HST),)


_GST_ONLY = (Component("GST", GST, GST_HST),)

PROVINCES: dict[str, Province] = {
    p.code: p
    for p in (
        Province("AB", "Alberta", _GST_ONLY),
        Province("BC", "British Columbia", _gst_plus("PST", "0.07", PST)),
        Province("MB", "Manitoba", _gst_plus("RST", "0.07", PST)),
        Province("NB", "New Brunswick", _hst("0.15")),
        Province("NL", "Newfoundland and Labrador", _hst("0.15")),
        Province("NS", "Nova Scotia", _hst("0.14")),
        Province("NT", "Northwest Territories", _GST_ONLY),
        Province("NU", "Nunavut", _GST_ONLY),
        Province("ON", "Ontario", _hst("0.13")),
        Province("PE", "Prince Edward Island", _hst("0.15")),
        Province("QC", "Quebec", _gst_plus("QST", "0.09975", QST)),
        Province("SK", "Saskatchewan", _gst_plus("PST", "0.06", PST)),
        Province("YT", "Yukon", _GST_ONLY),
    )
}

# Nova Scotia's HST fell from 15% to 14% on 2025-04-01 (the provincial part
# from 10% to 9%). A document dated before then is still taxed at 15%, and a
# code has one rate, so that rate is a code of its own, offered on request.
NS_BEFORE_2025_04_01 = Component("HST", Decimal("0.15"), GST_HST)
NS_BEFORE_NAME = "HST NS 15% (before 2025-04-01)"
NS_RATE_CHANGE_NOTE = (
    "Nova Scotia's HST is 14% from 2025-04-01 and was 15% before. Documents "
    f"dated before 2025-04-01 need the 15% code, '{NS_BEFORE_NAME}'."
)


@dataclass(frozen=True)
class CodePlan:
    name: str
    description: str
    components: tuple[Component, ...]
    # Zero-rated and Exempt are both 0%; the difference is whether tax paid
    # on what went into the sale is claimed back. Zero-rated sales are
    # taxable, so their inputs earn ITCs; exempt ones do not.
    recoverable: bool = True


def percent(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}%"


def code_plans(
    province: Province,
    *,
    before_2025_04_01: bool = False,
    other_provinces: bool = False,
) -> list[CodePlan]:
    """The codes the preset makes for a province, in the order it makes them."""
    comps = province.components
    total = sum((c.rate for c in comps), Decimal("0"))
    if len(comps) == 1:
        main = comps[0]
        if main.name == "GST":
            first = CodePlan(
                f"GST {percent(main.rate)}",
                f"GST, {province.name}",
                comps,
            )
        else:
            first = CodePlan(
                f"{main.name} {province.code} {percent(main.rate)}",
                f"{main.name} {percent(main.rate)}, {province.name}",
                comps,
            )
        plans = [first]
    else:
        names = " + ".join(c.name for c in comps)
        plans = [
            CodePlan(
                f"{names} {province.code} {percent(total)}",
                f"{names}, {province.name}",
                comps,
            ),
            # The provincial tax doesn't reach everything GST does (most
            # services in BC, say), so GST alone is a code of its own.
            CodePlan(f"GST {percent(GST)}", "GST alone", _GST_ONLY),
        ]
    # The same tax paid on a purchase that earns no input tax credit (inputs
    # to exempt sales, personal use, the half of meals not claimed): the
    # tax is part of what it cost, as on any purchase whose tax is spread.
    plans.insert(
        1,
        CodePlan(
            f"{plans[0].name} (no ITC)",
            f"{plans[0].description}, paid on a purchase that earns no input "
            "tax credit: the tax is part of its cost",
            comps,
            recoverable=False,
        ),
    )
    if other_provinces:
        # Place of supply: a sale shipped to another province is taxed at
        # that province's rate. Its HST, and GST alone for a GST-only or
        # PST province, all owed on the one GST/HST account.
        names = {p.name for p in plans}
        for other in PROVINCES.values():
            if other.code == province.code:
                continue
            if len(other.components) == 1 and other.components[0].name == "HST":
                hst = other.components[0]
                plan = CodePlan(
                    f"HST {other.code} {percent(hst.rate)}",
                    f"HST {percent(hst.rate)}, for sales to {other.name}",
                    other.components,
                )
            else:
                plan = CodePlan(f"GST {percent(GST)}", "GST alone", _GST_ONLY)
            if plan.name not in names:
                names.add(plan.name)
                plans.append(plan)
    if before_2025_04_01 and province.code == "NS":
        plans.append(
            CodePlan(
                NS_BEFORE_NAME,
                "HST 15%, Nova Scotia, for documents dated before 2025-04-01",
                (NS_BEFORE_2025_04_01,),
            )
        )
    federal = "HST" if comps[0].name == "HST" else "GST"
    zero = (Component(federal, Decimal("0"), GST_HST),)
    plans.append(
        CodePlan(
            "Zero-rated",
            "Taxable at 0%: basic groceries, prescription drugs, exports",
            zero,
        )
    )
    plans.append(
        CodePlan(
            "Exempt",
            "Exempt supply: no GST/HST is charged, and tax paid on its inputs "
            "is generally not an input tax credit",
            zero,
            recoverable=False,
        )
    )
    return plans


def account_plan(province: Province) -> dict[str, tuple[str, str]]:
    """kind -> (number, name) of each account the province's codes post to."""
    out = {GST_HST: ACCOUNTS[GST_HST]}
    for c in province.components:
        if c.kind == QST:
            out[QST] = ACCOUNTS[QST]
        elif c.kind == PST:
            out[PST] = (PST_ACCOUNT_NUMBER, f"{c.name} Payable")
    return out


def notes(province: Province, *, before_2025_04_01: bool = False) -> list[str]:
    """What the person setting up the codes should know about them."""
    out = []
    if province.code == "NS" and not before_2025_04_01:
        out.append(NS_RATE_CHANGE_NOTE)
    if any(c.kind == PST for c in province.components):
        word = next(c.name for c in province.components if c.kind == PST)
        out.append(
            f"{word} is not claimed back: on a bill it is part of what the lines cost."
        )
    return out


def tax_account(db, number: str, name: str):
    """The liability a preset posts to, and whether it was made now. An
    active account already holding the number is used if it is a liability,
    then an active one holding the name; otherwise it is made, keeping the
    number only if the chart hasn't used it for something else. A
    deactivated account is never quietly brought back into use."""
    from decimal import Decimal

    from app.models.accounts import Account, AccountType

    by_number = db.query(Account).filter(Account.account_number == number).first()
    if (
        by_number is not None
        and by_number.is_active is not False
        and by_number.account_type == AccountType.LIABILITY
    ):
        return by_number, False
    by_name = (
        db.query(Account)
        .filter(
            Account.name == name,
            Account.account_type == AccountType.LIABILITY,
            Account.is_active.isnot(False),
        )
        .first()
    )
    if by_name is not None:
        return by_name, False
    acct = Account(
        name=name,
        account_number=None if by_number is not None else number,
        account_type=AccountType.LIABILITY,
        is_system=True,
        balance=Decimal("0"),
    )
    db.add(acct)
    db.flush()
    return acct, True


def level_of(kind: str) -> str:
    """GST and HST are federal (HST is filed with GST, on the GST34);
    QST, PST and RST are the province's."""
    return "federal" if kind == GST_HST else "provincial"
