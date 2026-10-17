"""
Payroll and income-tax (TDS) calculation for the Ciliconchip Portal.

Tax rules: tax year 2026-27 (1 Apr 2026 - 31 Mar 2027), Income-tax Act 2025 (in force from 1 Apr 2026).
Slabs, standard deduction and rebate are unchanged from 2025-26:
  New regime: 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, 20-24L 25%, above 24L 30%;
              standard deduction 75,000; rebate up to 60,000 so taxable income up to 12L pays no tax
              (with marginal relief just above 12L).
  Old regime: 0-2.5L nil (3L for 60+, 5L for 80+), 2.5-5L 5%, 5-10L 20%, above 10L 30%;
              standard deduction 50,000; rebate up to 12,500 for taxable income up to 5L.
  Surcharge: 10% above 50L, 15% above 1Cr, 25% above 2Cr (old regime 37% above 5Cr), with marginal relief.
  Health & education cess 4%.
Deduction names below use the familiar 1961-Act section numbers (80C, 80D, ...) because that is what employees
know; the 2025 Act keeps the same deductions under new section numbers.

This is a payroll aid: Finance should check the figures, especially for unusual cases, before publishing.
"""

import calendar
from datetime import date
from typing import Dict, List, Optional

TAX_RULES = {
    "2026-27": {
        "new": {"slabs": [(400000, 0.0), (800000, 0.05), (1200000, 0.10), (1600000, 0.15), (2000000, 0.20),
                          (2400000, 0.25), (None, 0.30)],
                "std_deduction": 75000, "rebate_limit": 1200000, "rebate_max": 60000, "surcharge_cap": 0.25},
        "old": {"slabs": [(250000, 0.0), (500000, 0.05), (1000000, 0.20), (None, 0.30)],
                "slabs_60": [(300000, 0.0), (500000, 0.05), (1000000, 0.20), (None, 0.30)],
                "slabs_80": [(500000, 0.0), (1000000, 0.20), (None, 0.30)],
                "std_deduction": 50000, "rebate_limit": 500000, "rebate_max": 12500, "surcharge_cap": 0.37},
        "cess": 0.04,
    },
}
TAX_RULES["2025-26"] = TAX_RULES["2026-27"]   # same slabs
SURCHARGE = [(5_00_00_000, 0.37), (2_00_00_000, 0.25), (1_00_00_000, 0.15), (50_00_000, 0.10)]

DEFAULT_SETTINGS = {
    "company_name": "Ciliconchip",
    "company_address": "",
    "pf_wage_ceiling": 15000,
    "pf_rate": 0.12,
    "esi_gross_limit": 21000,
    "esi_employee_rate": 0.0075,
    "esi_employer_rate": 0.0325,
    "default_pt": 200,
}

EARNING_FIELDS = [("basic", "Basic"), ("hra", "HRA"), ("special_allowance", "Special Allowance"),
                  ("conveyance", "Conveyance"), ("lta", "LTA"), ("other_allowance", "Other Allowance")]


def fy_of(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def fy_months(fy: str) -> List[date]:
    start = int(fy[:4])
    return [date(start + (m < 4), m, 1) for m in list(range(4, 13)) + [1, 2, 3]]


def _slab_tax(income: float, slabs) -> float:
    tax, lower = 0.0, 0.0
    for upper, rate in slabs:
        if upper is None or income <= upper:
            tax += max(0.0, income - lower) * rate
            break
        tax += (upper - lower) * rate
        lower = upper
    return tax


def annual_tax(taxable: float, regime: str, fy: str = "2026-27", age: int = 30) -> Dict[str, float]:
    """Tax on annual taxable income, with rebate, surcharge (with marginal relief) and cess."""
    rules = TAX_RULES.get(fy, TAX_RULES["2026-27"])
    r = rules[regime]
    taxable = max(0.0, float(taxable))
    slabs = r["slabs"]
    if regime == "old" and age >= 80:
        slabs = r["slabs_80"]
    elif regime == "old" and age >= 60:
        slabs = r["slabs_60"]
    base = _slab_tax(taxable, slabs)

    rebate = 0.0
    if taxable <= r["rebate_limit"]:
        rebate = min(base, r["rebate_max"])
    elif regime == "new":   # marginal relief: tax may not exceed the income above 12L
        excess = taxable - r["rebate_limit"]
        if base > excess:
            rebate = base - excess
    after_rebate = base - rebate

    surcharge = 0.0
    for threshold, rate in SURCHARGE:
        rate = min(rate, r["surcharge_cap"])
        if taxable > threshold:
            surcharge = after_rebate * rate
            # marginal relief: tax + surcharge may not exceed tax at the threshold + income above it
            at_threshold = _slab_tax(threshold, slabs)
            lower_rate = next((rt for th, rt in SURCHARGE if th < threshold), 0.0)
            at_threshold += at_threshold * min(lower_rate, r["surcharge_cap"])
            limit = at_threshold + (taxable - threshold)
            if after_rebate + surcharge > limit:
                surcharge = max(0.0, limit - after_rebate)
            break
    cess = (after_rebate + surcharge) * rules["cess"]
    total = round(after_rebate + surcharge + cess)
    return {"taxable": round(taxable), "slab_tax": round(base), "rebate": round(rebate),
            "surcharge": round(surcharge), "cess": round(cess), "total": total}


def taxable_income(annual_gross: float, regime: str, fy: str, decl: Dict, annual_basic: float, annual_hra: float,
                   annual_pf_vpf: float, annual_pt: float) -> Dict[str, float]:
    """Annual taxable salary after standard deduction and (old regime) the employee's declarations."""
    rules = TAX_RULES.get(fy, TAX_RULES["2026-27"])[regime]
    lines = {"gross": annual_gross, "standard_deduction": min(rules["std_deduction"], annual_gross)}
    if regime == "old":
        rent = float(decl.get("rent_paid_annual") or 0)
        metro = bool(decl.get("metro_city"))
        hra_exempt = 0.0
        if rent > 0 and annual_hra > 0:
            hra_exempt = max(0.0, min(annual_hra, rent - 0.10 * annual_basic, (0.5 if metro else 0.4) * annual_basic))
        lines["hra_exemption"] = hra_exempt
        lines["professional_tax"] = min(annual_pt, 2500)
        lines["sec_80c"] = min(150000.0, float(decl.get("sec_80c") or 0) + annual_pf_vpf)
        lines["sec_80d"] = min(100000.0, float(decl.get("sec_80d") or 0))
        lines["sec_80ccd_1b"] = min(50000.0, float(decl.get("sec_80ccd_1b") or 0))
        lines["home_loan_interest"] = min(200000.0, float(decl.get("home_loan_interest") or 0))
        lines["other_deductions"] = float(decl.get("other_deductions") or 0)
    deductions = sum(v for k, v in lines.items() if k != "gross")
    lines["taxable"] = max(0.0, annual_gross - deductions)
    return {k: round(v) for k, v in lines.items()}


def professional_tax_default(state: str, gross: float, month: int, default: float = 200) -> float:
    """A sensible default only - Finance can override per employee. Rules vary by state."""
    s = (state or "").strip().lower()
    if s in ("karnataka",):
        return 200.0 if gross >= 25000 else 0.0
    if s in ("telangana", "andhra pradesh"):
        return 200.0 if gross > 20000 else (150.0 if gross > 15000 else 0.0)
    if s in ("maharashtra",):
        return (300.0 if month == 2 else 200.0) if gross > 10000 else 0.0
    if s in ("tamil nadu",):
        return 208.0 if gross > 12500 else 0.0     # half-yearly tax spread monthly (approx.)
    if s in ("west bengal",):
        return 200.0 if gross > 40000 else (150.0 if gross > 25000 else 0.0)
    if s in ("delhi", "haryana", "uttar pradesh", "rajasthan", "punjab", "chandigarh"):
        return 0.0
    return float(default)


def compute_payslip(struct: Dict, month_start: date, joined: date, exited: Optional[date], lwp_days: float,
                    settings: Dict, regime: str, decl: Dict, ytd: Dict, extra_earning: float = 0.0,
                    extra_earning_label: str = "", extra_deduction: float = 0.0, extra_deduction_label: str = "",
                    state: str = "", age: int = 30) -> Dict:
    """
    One month's pay.
    struct: monthly amounts {basic, hra, special_allowance, conveyance, lta, other_allowance, pf_mode, vpf,
            pt (None = default for the state), other_deduction, other_deduction_label, tds_override}
    ytd: {"gross": paid so far this tax year, "tds": deducted so far, "pf_vpf": ..., "pt": ..., "basic": ..., "hra": ...}
    """
    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = date(month_start.year, month_start.month, days_in_month)
    first_day = max(month_start, joined)
    last_day = min(month_end, exited) if exited else month_end
    employed_days = max(0, (last_day - first_day).days + 1) if first_day <= last_day else 0
    paid_days = max(0.0, employed_days - lwp_days)
    factor = paid_days / days_in_month

    earnings = {}
    for key, label in EARNING_FIELDS:
        amt = round(float(struct.get(key) or 0) * factor)
        if amt:
            earnings[label] = amt
    if extra_earning:
        earnings[extra_earning_label or "Additional earning"] = round(extra_earning)
    gross = sum(earnings.values())
    basic_paid = earnings.get("Basic", 0)

    pf_mode = struct.get("pf_mode") or "Capped at PF wage ceiling"
    rate = float(settings.get("pf_rate", 0.12))
    if pf_mode == "Not applicable":
        pf_base = 0
    elif pf_mode == "On full basic":
        pf_base = basic_paid
    else:
        pf_base = min(basic_paid, float(settings.get("pf_wage_ceiling", 15000)))
    emp_pf = round(pf_base * rate)
    employer_pf = round(pf_base * rate)
    vpf = round(float(struct.get("vpf") or 0) * factor) if pf_mode != "Not applicable" else 0

    monthly_gross_full = sum(float(struct.get(k) or 0) for k, _ in EARNING_FIELDS)
    esi_emp = esi_employer = 0
    if monthly_gross_full and monthly_gross_full <= float(settings.get("esi_gross_limit", 21000)):
        esi_emp = round(gross * float(settings.get("esi_employee_rate", 0.0075)) + 0.4999)
        esi_employer = round(gross * float(settings.get("esi_employer_rate", 0.0325)) + 0.4999)

    pt_set = struct.get("pt")
    if pt_set is None or pt_set == "":
        pt = professional_tax_default(state, gross, month_start.month, settings.get("default_pt", 200)) if gross else 0
    else:
        pt = float(pt_set) if gross else 0
    pt = round(pt)

    # ---- TDS: project the year, take off what is already deducted, spread over the remaining months
    fy = fy_of(month_start)
    months = fy_months(fy)
    last_month = exited.replace(day=1) if exited and exited.replace(day=1) <= months[-1] else months[-1]
    remaining = [m for m in months if month_start <= m <= last_month]
    n_rem = max(1, len(remaining))
    future = n_rem - 1
    proj_gross = ytd.get("gross", 0) + gross + monthly_gross_full * future
    proj_basic = ytd.get("basic", 0) + basic_paid + float(struct.get("basic") or 0) * future
    proj_hra = ytd.get("hra", 0) + earnings.get("HRA", 0) + float(struct.get("hra") or 0) * future
    proj_pf_vpf = ytd.get("pf_vpf", 0) + emp_pf + vpf + (emp_pf + vpf) * future
    proj_pt = ytd.get("pt", 0) + pt + pt * future
    ti = taxable_income(proj_gross, regime, fy, decl or {}, proj_basic, proj_hra, proj_pf_vpf, proj_pt)
    tax = annual_tax(ti["taxable"], regime, fy, age)
    tds_auto = max(0, round((tax["total"] - ytd.get("tds", 0)) / n_rem))
    override = struct.get("tds_override")
    tds = round(float(override)) if override not in (None, "") else tds_auto
    if not gross:
        tds = 0

    deductions = {}
    if emp_pf:
        deductions["PF (Employee)"] = emp_pf
    if vpf:
        deductions["VPF"] = vpf
    if esi_emp:
        deductions["ESI (Employee)"] = esi_emp
    if pt:
        deductions["Professional Tax"] = pt
    if tds:
        deductions["Income Tax (TDS)"] = tds
    od = round(float(struct.get("other_deduction") or 0) * (1 if factor else 0))
    if od:
        deductions[struct.get("other_deduction_label") or "Other deduction"] = od
    if extra_deduction:
        deductions[extra_deduction_label or "Additional deduction"] = round(extra_deduction)
    total_ded = sum(deductions.values())
    return {
        "month": month_start.strftime("%Y-%m"), "fy": fy, "regime": regime,
        "days_in_month": days_in_month, "employed_days": employed_days, "lwp_days": lwp_days,
        "paid_days": round(paid_days, 2), "earnings": earnings, "deductions": deductions,
        "gross": gross, "total_deductions": total_ded, "net": gross - total_ded,
        "employer": {k: v for k, v in (("PF (Employer)", employer_pf), ("ESI (Employer)", esi_employer)) if v},
        "tax": {"projected_gross": round(proj_gross), "taxable": ti["taxable"], "annual_tax": tax["total"],
                "tds_so_far": round(ytd.get("tds", 0)), "remaining_months": n_rem, "tds_auto": tds_auto,
                "tds_overridden": override not in (None, ""), "lines": ti},
        "ytd_basic": basic_paid, "ytd_hra": earnings.get("HRA", 0), "ytd_pf_vpf": emp_pf + vpf,
    }


_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n):
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _three(n):
    h, r = divmod(n, 100)
    return ((_ONES[h] + " Hundred" + (" " if r else "")) if h else "") + (_two(r) if r else "")


def rupees_in_words(amount: float) -> str:
    n = int(round(amount))
    if n == 0:
        return "Rupees Zero Only"
    parts = []
    for div, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        q, n = divmod(n, div)
        if q:
            parts.append(f"{_three(q) if q < 1000 else rupees_in_words(q)[7:-5]} {name}")
    if n:
        parts.append(_three(n))
    return "Rupees " + " ".join(parts) + " Only"
