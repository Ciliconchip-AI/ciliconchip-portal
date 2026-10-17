"""
Leave rules for the Ciliconchip Portal.

Balances are always CALCULATED from the rules, the joining date, approved leave and HR adjustments - nothing
is credited by a nightly job, so balances can never drift or be "missed" when the app was asleep.

Rules (HR can change the numbers in the app):
  - Leave is credited on the 1st of every month of service. The joining month counts when the person joined
    on or before the 15th; otherwise crediting starts the next month. No credit for a month in which the person
    left before the 15th.
  - Leave year = calendar year (1 Jan - 31 Dec).
  - Types that lapse (e.g. CL, SL) start every year at zero. Types that carry forward (e.g. PL) carry the unused
    balance into the next year, up to the carry-forward cap.
  - Leave days exclude Saturdays, Sundays and company holidays. A single day can be taken as a half day.
  - LWP (leave without pay) has no balance; it reduces salary in payroll.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_LEAVE_TYPES = [
    # code, name, monthly accrual, carry-forward cap (0 = none), lapses at year end, paid
    ("CL", "Casual Leave", 1.0, 0.0, True, True),
    ("PL", "Privilege Leave", 1.25, 30.0, False, True),
    ("SL", "Sick Leave", 0.5, 0.0, True, True),
    ("LWP", "Leave Without Pay", 0.0, 0.0, True, False),
]


@dataclass
class LeaveType:
    code: str
    name: str
    monthly_accrual: float
    carry_forward_cap: float
    lapse_year_end: bool
    paid: bool

    @property
    def has_balance(self) -> bool:
        return self.paid


def day_parts(start: date, end: date, half_day: str, holidays: Set[date]) -> List[Tuple[date, float]]:
    """Every working day in the period with its fraction (1 or 0.5)."""
    if end < start:
        return []
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            out.append((d, 1.0))
        d += timedelta(days=1)
    if half_day in ("First half", "Second half") and start == end and out:
        out = [(out[0][0], 0.5)]
    return out


def leave_days(start: date, end: date, half_day: str, holidays: Set[date]) -> float:
    return sum(f for _, f in day_parts(start, end, half_day, holidays))


def credited_months(year: int, joined: date, exited: Optional[date], as_of: date) -> int:
    """How many monthly credits fall in `year`, up to and including `as_of`."""
    n = 0
    for m in range(1, 13):
        first = date(year, m, 1)
        if first > as_of:
            break
        mid = date(year, m, 15)
        if joined > mid:
            continue
        if exited and exited < mid:
            continue
        n += 1
    return n


def balances(types: Iterable[LeaveType], joined: date, exited: Optional[date], taken: List[Dict],
             adjustments: List[Dict], as_of: date, holidays: Set[date]) -> Dict[str, Dict]:
    """
    taken: leave requests [{leave_type, start_date, end_date, half_day, status}] - Approved ones are used,
           Pending ones are held back from the available balance.
    adjustments: [{leave_type, days, effective_date}] - HR corrections and opening balances.
    Returns per leave type: opening, accrued, adjusted, used, pending, available (for the year of `as_of`).
    """
    types = list(types)
    used_by_year: Dict[Tuple[str, int], float] = {}
    pending_by_year: Dict[Tuple[str, int], float] = {}
    for r in taken:
        if r["status"] not in ("Approved", "Pending"):
            continue
        for d, f in day_parts(r["start_date"], r["end_date"], r.get("half_day") or "", holidays):
            bucket = used_by_year if r["status"] == "Approved" else pending_by_year
            key = (r["leave_type"], d.year)
            bucket[key] = bucket.get(key, 0.0) + f
    adj_by_year: Dict[Tuple[str, int], float] = {}
    for a in adjustments:
        key = (a["leave_type"], a["effective_date"].year)
        adj_by_year[key] = adj_by_year.get(key, 0.0) + float(a["days"])

    out = {}
    for t in types:
        carry = 0.0
        row = {}
        for year in range(joined.year, as_of.year + 1):
            opening = 0.0 if t.lapse_year_end else carry
            full_year_end = date(year, 12, 31)
            to_date = as_of if year == as_of.year else full_year_end
            accrued = t.monthly_accrual * credited_months(year, joined, exited, to_date)
            adjusted = adj_by_year.get((t.code, year), 0.0)
            used = used_by_year.get((t.code, year), 0.0)
            pending = pending_by_year.get((t.code, year), 0.0)
            row = {"opening": opening, "accrued": accrued, "adjusted": adjusted, "used": used, "pending": pending,
                   "available": opening + accrued + adjusted - used - pending}
            if year < as_of.year:
                closing = opening + accrued + adjusted - used
                carry = 0.0 if t.lapse_year_end else max(0.0, min(closing, t.carry_forward_cap or 0.0))
        if not row:   # joined in the future
            row = {"opening": 0.0, "accrued": 0.0, "adjusted": 0.0, "used": 0.0, "pending": 0.0, "available": 0.0}
        if not t.has_balance:
            row["available"] = None
        out[t.code] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}
    return out


def lwp_days_in_month(requests: List[Dict], year: int, month: int, holidays: Set[date],
                      lwp_codes: Set[str]) -> float:
    total = 0.0
    for r in requests:
        if r["status"] != "Approved" or r["leave_type"] not in lwp_codes:
            continue
        for d, f in day_parts(r["start_date"], r["end_date"], r.get("half_day") or "", holidays):
            if d.year == year and d.month == month:
                total += f
    return total
