"""
Database layer for the Ciliconchip Portal: people and roles, employees, HR (policies, holidays, leave) and
Finance (salary structures, tax regime, payroll runs, payslips), notifications and the audit log.

Uses the same MySQL connection as the Resume Bank tables. Salary figures, payslips, PAN and bank details are
stored encrypted (see security.py).
"""

import re
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import (
    Column, Date, DateTime, Float, Integer, LargeBinary, MetaData, String, Table, Text, UniqueConstraint, and_,
    desc, func, or_, select, text,
)
from sqlalchemy.dialects import mysql

from .db import (MAX_FAILED_LOGINS, LOCK_MINUTES, check_password, hash_password, password_problem,  # noqa: F401
                 temporary_password)
from .leave import DEFAULT_LEAVE_TYPES, LeaveType, balances, leave_days, lwp_days_in_month
from .payroll import DEFAULT_SETTINGS
from .security import Vault

LONGBLOB = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
BLOB = LargeBinary().with_variant(mysql.BLOB(), "mysql")

# Everyone is an employee ("Resource"). These roles add access on top. Managers are recognised automatically
# from the "hardline manager" field - no role needed.
PORTAL_ROLES = {
    "Super Admin": "Manages who can use the Portal and their roles; reads the audit log; full Resume Bank access",
    "HR Admin": "Employees, policies, holidays, leave rules, all leave requests and balance corrections",
    "Finance Admin": "Salary structures, tax regime, payroll runs and all payslips",
    "Recruiter": "Resume Bank: upload, search, match jobs, update profiles",
    "TA Viewer": "Resume Bank: search, match jobs, view and download only",
}
LEAVE_STATUSES = ["Pending", "Approved", "Rejected", "Cancelled"]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today_ist() -> date:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()


class PortalDB:
    def __init__(self, engine, vault: Vault):
        self.engine = engine
        self.vault = vault
        m = self.meta = MetaData()
        opts = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4")
        self.users = Table(
            "portal_users", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("email", String(200), unique=True, nullable=False),
            Column("full_name", String(120), nullable=False),
            Column("roles", String(200), default=""),
            Column("password_hash", String(200), nullable=False),
            Column("must_change_password", Integer, default=1),
            Column("active", Integer, default=1),
            Column("failed_logins", Integer, default=0),
            Column("locked_until", DateTime),
            Column("last_login_at", DateTime),
            Column("created_by", String(200)),
            Column("created_at", DateTime),
            Column("updated_at", DateTime), **opts)
        self.emp = Table(
            "portal_employees", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("email", String(200), unique=True, nullable=False),
            Column("emp_code", String(30), unique=True, nullable=False),
            Column("full_name", String(120), nullable=False),
            Column("designation", String(120)),
            Column("department", String(120)),
            Column("location", String(120)),
            Column("work_state", String(60)),
            Column("date_of_joining", Date, nullable=False),
            Column("date_of_exit", Date),
            Column("date_of_birth", Date),
            Column("gender", String(20)),
            Column("phone", String(40)),
            Column("manager_email", String(200), index=True),
            Column("uan", String(30)),
            Column("private_enc", BLOB),          # encrypted: PAN, bank account, IFSC, bank name
            Column("updated_by", String(200)),
            Column("created_at", DateTime),
            Column("updated_at", DateTime), **opts)
        self.audit_t = Table(
            "portal_audit", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("at", DateTime, index=True),
            Column("user_email", String(200), index=True),
            Column("user_name", String(120)),
            Column("action", String(40), index=True),
            Column("ref", String(80)),
            Column("details", Text), **opts)
        self.notes = Table(
            "portal_notifications", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("user_email", String(200), index=True),
            Column("at", DateTime),
            Column("message", Text),
            Column("read_at", DateTime), **opts)
        self.policies = Table(
            "hr_policies", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("title", String(200), nullable=False),
            Column("category", String(80)),
            Column("summary", Text),
            Column("effective_date", Date),
            Column("version", Integer, default=1),
            Column("file_name", String(300)),
            Column("file_data", LONGBLOB),
            Column("active", Integer, default=1),
            Column("uploaded_by", String(200)),
            Column("uploaded_at", DateTime), **opts)
        self.policy_ack = Table(
            "hr_policy_ack", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("policy_id", Integer, index=True),
            Column("user_email", String(200)),
            Column("at", DateTime),
            UniqueConstraint("policy_id", "user_email", name="uq_policy_ack"), **opts)
        self.holidays = Table(
            "hr_holidays", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("day", Date, unique=True, nullable=False),
            Column("name", String(120)), **opts)
        self.ltypes = Table(
            "hr_leave_types", m,
            Column("code", String(10), primary_key=True),
            Column("name", String(60)),
            Column("monthly_accrual", Float),
            Column("carry_forward_cap", Float),
            Column("lapse_year_end", Integer),
            Column("paid", Integer),
            Column("active", Integer, default=1),
            Column("sort", Integer), **opts)
        self.leaves = Table(
            "hr_leave_requests", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("employee_email", String(200), index=True),
            Column("leave_type", String(10)),
            Column("start_date", Date),
            Column("end_date", Date),
            Column("half_day", String(20)),
            Column("days", Float),
            Column("reason", Text),
            Column("status", String(20), index=True),
            Column("approver_email", String(200), index=True),
            Column("applied_at", DateTime),
            Column("decided_by", String(200)),
            Column("decided_at", DateTime),
            Column("decision_comment", Text), **opts)
        self.adjust = Table(
            "hr_leave_adjustments", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("employee_email", String(200), index=True),
            Column("leave_type", String(10)),
            Column("days", Float),
            Column("effective_date", Date),
            Column("reason", Text),
            Column("created_by", String(200)),
            Column("created_at", DateTime), **opts)
        self.settings_t = Table(
            "fin_settings", m,
            Column("skey", String(60), primary_key=True),
            Column("value", Text), **opts)
        self.salary = Table(
            "fin_salary", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("employee_email", String(200), index=True),
            Column("effective_from", Date),
            Column("data_enc", BLOB),
            Column("created_by", String(200)),
            Column("created_at", DateTime), **opts)
        self.tax = Table(
            "fin_tax", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("employee_email", String(200)),
            Column("fy", String(10)),
            Column("regime", String(5)),
            Column("data_enc", BLOB),
            Column("updated_by", String(200)),
            Column("updated_at", DateTime),
            UniqueConstraint("employee_email", "fy", name="uq_tax_emp_fy"), **opts)
        self.runs = Table(
            "fin_payroll_runs", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("month", String(7), unique=True),
            Column("status", String(12)),
            Column("created_by", String(200)),
            Column("created_at", DateTime),
            Column("published_by", String(200)),
            Column("published_at", DateTime), **opts)
        self.slips = Table(
            "fin_payslips", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("run_id", Integer, index=True),
            Column("employee_email", String(200), index=True),
            Column("data_enc", BLOB),
            Column("extra_enc", BLOB),        # this month's one-off earning / deduction entered by Finance
            UniqueConstraint("run_id", "employee_email", name="uq_slip"), **opts)
        self.opening = Table(
            "fin_opening_ytd", m,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("employee_email", String(200)),
            Column("fy", String(10)),
            Column("data_enc", BLOB),
            Column("updated_by", String(200)),
            Column("updated_at", DateTime),
            UniqueConstraint("employee_email", "fy", name="uq_opening_emp_fy"), **opts)
        self.meta_t = Table(
            "portal_meta", m,
            Column("mkey", String(40), primary_key=True),
            Column("value", Integer), **opts)
        m.create_all(engine)
        self._db_version = None
        self._cache = {}          # key -> (version, loaded_at, value); holds encrypted data only
        self._version = 0         # bumped by every write, so nobody ever reads stale data
        self._lock = threading.Lock()
        self._seed()

    # ================================================================= shared cache
    # Streamlit Cloud and the database can be far apart (e.g. US and Bengaluru), so every query costs a
    # round trip. The small tables are kept in memory and reloaded after any change (or after a minute).
    # Salary, payslip, PAN and bank data stay ENCRYPTED in this cache and are decrypted only when shown.
    CACHE_SECONDS = 300

    def new_run(self):
        """Start of every click: one tiny query tells us whether anything changed anywhere since the last one."""
        try:
            with self.engine.connect() as conn:
                v = conn.execute(select(self.meta_t.c.value).where(self.meta_t.c.mkey == "data_version")).scalar()
        except Exception:
            v = None
        if v is None or v != self._db_version:
            with self._lock:
                self._db_version = v
                self._version += 1

    def _c(self, key, loader):
        now = time.time()
        hit = self._cache.get(key)
        if hit and hit[0] == self._version and now - hit[1] < self.CACHE_SECONDS:
            return hit[2]
        version = self._version
        value = loader()
        self._cache[key] = (version, now, value)
        return value

    def _dirty(self):
        with self._lock:
            self._version += 1
        try:   # tell every other copy of the app that data changed
            with self.engine.begin() as conn:
                if not conn.execute(self.meta_t.update().where(self.meta_t.c.mkey == "data_version")
                                    .values(value=self.meta_t.c.value + 1)).rowcount:
                    conn.execute(self.meta_t.insert().values(mkey="data_version", value=1))
        except Exception:
            pass

    @contextmanager
    def _tx(self):
        """A write transaction; the page cache is cleared once it is committed."""
        with self.engine.begin() as conn:
            yield conn
        self._dirty()

    def _rows(self, table, order=None):
        q = select(table)
        if order is not None:
            q = q.order_by(*order)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(q).mappings()]

    # ================================================================= setup
    def _seed(self):
        with self._tx() as conn:
            if not conn.execute(select(func.count()).select_from(self.ltypes)).scalar():
                for i, (code, name, acc, cap, lapse, paid) in enumerate(DEFAULT_LEAVE_TYPES):
                    conn.execute(self.ltypes.insert().values(code=code, name=name, monthly_accrual=acc,
                                                             carry_forward_cap=cap, lapse_year_end=int(lapse),
                                                             paid=int(paid), active=1, sort=i))

    # ================================================================= audit & notifications
    def audit(self, user: Optional[Dict], action: str, details: str = "", ref: str = ""):
        user = user or {}
        try:
            with self._tx() as conn:
                conn.execute(self.audit_t.insert().values(
                    at=_now(), user_email=(user.get("email") or "")[:200], user_name=(user.get("full_name") or "")[:120],
                    action=action[:40], ref=str(ref or "")[:80], details=(details or "")[:4000]))
        except Exception:
            pass

    def audit_log(self, user_email="", actions=None, since=None, until=None, limit=3000) -> List[Dict]:
        a = self.audit_t
        conds = []
        if user_email:
            conds.append(a.c.user_email == user_email)
        if actions:
            conds.append(a.c.action.in_(actions))
        if since is not None:
            conds.append(a.c.at >= since)
        if until is not None:
            conds.append(a.c.at < until)
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(a).where(and_(*conds) if conds else text("1=1"))
                                                  .order_by(desc(a.c.at), desc(a.c.id)).limit(limit)).mappings()]

    def notify(self, email: str, message: str):
        self._dirty()
        if not email:
            return
        with self._tx() as conn:
            conn.execute(self.notes.insert().values(user_email=email, at=_now(), message=message[:1000]))

    def notifications(self, email: str, unread_only=False, limit=20) -> List[Dict]:
        def load():
            n = self.notes
            with self.engine.connect() as conn:
                return [dict(r) for r in conn.execute(select(n).where(n.c.user_email == email)
                                                      .order_by(desc(n.c.at), desc(n.c.id)).limit(200)).mappings()]
        rows = self._c(("notes", email), load)
        if unread_only:
            rows = [r for r in rows if not r["read_at"]]
        return rows[:limit]

    def mark_read(self, email: str):
        self._dirty()
        with self._tx() as conn:
            conn.execute(self.notes.update().where(and_(self.notes.c.user_email == email,
                                                        self.notes.c.read_at.is_(None))).values(read_at=_now()))

    # ================================================================= users
    def user_count(self, super_admins_only=False) -> int:
        users = self._all_users()
        if super_admins_only:
            users = [u for u in users if u["active"] and "Super Admin" in self.roles_of(u)]
        return len(users)

    def _all_users(self) -> List[Dict]:
        return self._c("users", lambda: self._rows(self.users, (desc(self.users.c.active), self.users.c.full_name)))

    def get_user(self, email: str) -> Optional[Dict]:
        email = (email or "").strip().lower()
        return next((dict(u) for u in self._all_users() if u["email"] == email), None)

    def list_users(self) -> List[Dict]:
        return [dict(u) for u in self._all_users()]

    def create_user(self, email, full_name, roles: List[str], password, created_by="", must_change=True) -> str:
        self._dirty()
        email = (email or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return "Enter a valid email address."
        if not (full_name or "").strip():
            return "Enter the person's name."
        if self.get_user(email):
            return "This email already has a login."
        now = _now()
        with self._tx() as conn:
            conn.execute(self.users.insert().values(
                email=email, full_name=full_name.strip()[:120], roles=",".join(r for r in roles if r in PORTAL_ROLES),
                password_hash=hash_password(password), must_change_password=int(must_change), active=1,
                failed_logins=0, created_by=(created_by or "")[:200], created_at=now, updated_at=now))
        return ""

    def update_user(self, email: str, **values):
        self._dirty()
        values["updated_at"] = _now()
        with self._tx() as conn:
            conn.execute(self.users.update().where(self.users.c.email == email.strip().lower()).values(**values))

    def set_password(self, email, password, must_change: bool):
        self.update_user(email, password_hash=hash_password(password), must_change_password=int(must_change),
                         failed_logins=0, locked_until=None)

    def verify_login(self, email, password):
        user = self.get_user(email)
        now = _now()
        generic = "Email or password is not correct, or this account has no access."
        if not user or not user["active"]:
            if user is None:
                check_password(password, hash_password("timing-equaliser"))
            return None, generic
        if user["locked_until"] and user["locked_until"] > now:
            mins = max(1, int((user["locked_until"] - now).total_seconds() // 60) + 1)
            return None, f"Too many wrong attempts. This account is locked for {mins} more minute(s)."
        if not check_password(password, user["password_hash"]):
            fails = (user["failed_logins"] or 0) + 1
            if fails >= MAX_FAILED_LOGINS:
                self.update_user(email, failed_logins=0, locked_until=now + timedelta(minutes=LOCK_MINUTES))
                return None, f"Too many wrong attempts. This account is locked for {LOCK_MINUTES} minutes."
            self.update_user(email, failed_logins=fails)
            return None, generic
        self.update_user(email, failed_logins=0, locked_until=None, last_login_at=now)
        return user, ""

    @staticmethod
    def roles_of(user: Dict) -> List[str]:
        return [r for r in (user.get("roles") or "").split(",") if r]

    # ================================================================= employees
    def _emp_row(self, r) -> Dict:
        d = dict(r)
        d["private"] = {}
        if d.get("private_enc") and self.vault.ready:
            try:
                d["private"] = self.vault.unlock(d["private_enc"], {})
            except RuntimeError:
                d["private"] = {}
        d.pop("private_enc", None)
        return d

    def _all_employees(self) -> List[Dict]:
        return self._c("emps", lambda: self._rows(self.emp, (self.emp.c.full_name,)))

    def get_employee(self, email: str) -> Optional[Dict]:
        email = (email or "").lower()
        return next((self._emp_row(e) for e in self._all_employees() if e["email"] == email), None)

    def list_employees(self, include_exited=True) -> List[Dict]:
        rows = [self._emp_row(e) for e in self._all_employees()]
        if not include_exited:
            rows = [e for e in rows if not e["date_of_exit"] or e["date_of_exit"] >= today_ist()]
        return rows

    def team_of(self, manager_email: str) -> List[Dict]:
        return [self._emp_row(e) for e in self._all_employees() if e["manager_email"] == manager_email]

    def is_manager(self, email: str) -> bool:
        return any(e["manager_email"] == email for e in self._all_employees())

    def save_employee(self, data: Dict, by: str, new: bool) -> str:
        """Create or update an employee. Returns '' on success, otherwise the problem."""
        self._dirty()
        email = (data.get("email") or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return "Enter a valid work email."
        if not (data.get("full_name") or "").strip():
            return "Enter the employee's name."
        if not (data.get("emp_code") or "").strip():
            return "Enter the employee code."
        if not data.get("date_of_joining"):
            return "Enter the date of joining."
        mgr = (data.get("manager_email") or "").strip().lower() or None
        if mgr == email:
            return "An employee cannot be their own manager."
        e = self.emp
        with self.engine.connect() as conn:
            clash = conn.execute(select(e.c.email).where(and_(e.c.emp_code == data["emp_code"].strip(),
                                                              e.c.email != email))).first()
            exists = conn.execute(select(e.c.id).where(e.c.email == email)).first()
        if clash:
            return "This employee code is already used."
        if new and exists:
            return "An employee with this email already exists."
        if mgr and not self.get_employee(mgr):
            return "The manager must be an existing employee."
        if mgr and self._would_loop(email, mgr):
            return "That manager reports (directly or indirectly) to this employee."
        values = {k: data.get(k) for k in ("full_name", "designation", "department", "location", "work_state",
                                           "date_of_joining", "date_of_exit", "date_of_birth", "gender", "phone",
                                           "uan")}
        values["full_name"] = values["full_name"].strip()
        values["emp_code"] = data["emp_code"].strip()
        values["manager_email"] = mgr
        if data.get("private") is not None and self.vault.ready:
            values["private_enc"] = self.vault.lock(data["private"])
        values["updated_by"] = by
        values["updated_at"] = _now()
        with self._tx() as conn:
            if new:
                conn.execute(e.insert().values(email=email, created_at=_now(), **values))
            else:
                conn.execute(e.update().where(e.c.email == email).values(**values))
        return ""

    def _would_loop(self, email, mgr) -> bool:
        seen, cur = set(), mgr
        while cur and cur not in seen:
            if cur == email:
                return True
            seen.add(cur)
            m = self.get_employee(cur)
            cur = m["manager_email"] if m else None
        return False

    # ================================================================= policies
    def add_policy(self, title, category, summary, effective, file_name, data, by, replaces: Optional[int] = None):
        self._dirty()
        version = 1
        with self._tx() as conn:
            if replaces:
                old = conn.execute(select(self.policies.c.version).where(self.policies.c.id == replaces)).scalar()
                version = (old or 0) + 1
                conn.execute(self.policies.update().where(self.policies.c.id == replaces).values(active=0))
            conn.execute(self.policies.insert().values(
                title=title.strip(), category=category, summary=summary, effective_date=effective, version=version,
                file_name=file_name, file_data=data, active=1, uploaded_by=by, uploaded_at=_now()))

    def list_policies(self, active_only=True) -> List[Dict]:
        def load():
            p = self.policies
            cols = [c for c in p.c if c.name != "file_data"]
            with self.engine.connect() as conn:
                return [dict(r) for r in conn.execute(select(*cols).order_by(p.c.category, p.c.title)).mappings()]
        rows = self._c("policies", load)
        return [dict(r) for r in rows if r["active"] or not active_only]

    def policy_file(self, pid: int):
        with self.engine.connect() as conn:
            return conn.execute(select(self.policies.c.file_name, self.policies.c.file_data)
                                .where(self.policies.c.id == pid)).first()

    def retire_policy(self, pid: int):
        self._dirty()
        with self._tx() as conn:
            conn.execute(self.policies.update().where(self.policies.c.id == pid).values(active=0))

    def acknowledge(self, pid: int, email: str):
        self._dirty()
        with self._tx() as conn:
            if not conn.execute(select(self.policy_ack.c.id).where(and_(self.policy_ack.c.policy_id == pid,
                                                                         self.policy_ack.c.user_email == email))).first():
                conn.execute(self.policy_ack.insert().values(policy_id=pid, user_email=email, at=_now()))

    def acks(self, pid: Optional[int] = None, email: Optional[str] = None) -> List[Dict]:
        rows = self._c("acks", lambda: self._rows(self.policy_ack))
        return [r for r in rows if (pid is None or r["policy_id"] == pid) and (email is None or r["user_email"] == email)]

    # ================================================================= holidays & leave types
    def holiday_list(self, year: Optional[int] = None) -> List[Dict]:
        rows = self._c("holidays", lambda: self._rows(self.holidays, (self.holidays.c.day,)))
        return [dict(r) for r in rows if not year or r["day"].year == year]

    def holiday_set(self) -> set:
        return {h["day"] for h in self.holiday_list()}

    def set_holidays(self, year: int, rows: List[Dict]):
        """Replace all holidays of `year` with `rows`."""
        self._dirty()
        with self._tx() as conn:
            conn.execute(self.holidays.delete().where(and_(self.holidays.c.day >= date(year, 1, 1),
                                                           self.holidays.c.day <= date(year, 12, 31))))
            seen = set()
            for r in rows:
                if r["day"] in seen or r["day"].year != year:
                    continue
                seen.add(r["day"])
                conn.execute(self.holidays.insert().values(day=r["day"], name=(r.get("name") or "Holiday")[:120]))

    def leave_types(self, active_only=True) -> List[LeaveType]:
        rows = self._c("ltypes", lambda: self._rows(self.ltypes, (self.ltypes.c.sort,)))
        return [LeaveType(r["code"], r["name"], float(r["monthly_accrual"] or 0), float(r["carry_forward_cap"] or 0),
                          bool(r["lapse_year_end"]), bool(r["paid"])) for r in rows if r["active"] or not active_only]

    def save_leave_type(self, code, name, accrual, cap, lapse, paid, active=True):
        self._dirty()
        t = self.ltypes
        with self._tx() as conn:
            exists = conn.execute(select(t.c.code).where(t.c.code == code)).first()
            vals = dict(name=name, monthly_accrual=float(accrual), carry_forward_cap=float(cap),
                        lapse_year_end=int(lapse), paid=int(paid), active=int(active))
            if exists:
                conn.execute(t.update().where(t.c.code == code).values(**vals))
            else:
                n = conn.execute(select(func.count()).select_from(t)).scalar() or 0
                conn.execute(t.insert().values(code=code, sort=n, **vals))

    # ================================================================= leave
    def leave_requests(self, email: Optional[str] = None, approver: Optional[str] = None, statuses=None,
                       since: Optional[date] = None, until: Optional[date] = None) -> List[Dict]:
        lr = self.leaves
        rows = self._c("leaves", lambda: self._rows(lr, (desc(lr.c.start_date), desc(lr.c.id))))
        return [dict(r) for r in rows
                if (not email or r["employee_email"] == email) and (not approver or r["approver_email"] == approver)
                and (not statuses or r["status"] in statuses) and (not since or r["end_date"] >= since)
                and (not until or r["start_date"] <= until)]

    def adjustments(self, email: str) -> List[Dict]:
        rows = self._c("adjust", lambda: self._rows(self.adjust, (desc(self.adjust.c.created_at),)))
        return [dict(r) for r in rows if r["employee_email"] == email]

    def add_adjustment(self, email, leave_type, days, effective, reason, by):
        self._dirty()
        with self._tx() as conn:
            conn.execute(self.adjust.insert().values(employee_email=email, leave_type=leave_type, days=float(days),
                                                     effective_date=effective, reason=reason, created_by=by,
                                                     created_at=_now()))

    def balance_for(self, email: str, as_of: Optional[date] = None) -> Dict[str, Dict]:
        emp = self.get_employee(email)
        if not emp:
            return {}
        return balances(self.leave_types(), emp["date_of_joining"], emp["date_of_exit"],
                        self.leave_requests(email=email, statuses=["Approved", "Pending"]), self.adjustments(email),
                        as_of or today_ist(), self.holiday_set())

    @staticmethod
    def _balance_date(start: date) -> date:
        """Balance is what has been credited so far; leave in a later year uses that year's opening + January."""
        t = today_ist()
        return t if start.year <= t.year else date(start.year, 1, 1)

    def apply_leave(self, email, leave_type, start, end, half_day, reason) -> str:
        """Returns '' when submitted, otherwise why not."""
        self._dirty()
        emp = self.get_employee(email)
        if not emp:
            return "Your employee record has not been set up by HR yet."
        if end < start:
            return "The end date is before the start date."
        if half_day not in ("", "Full day", None) and start != end:
            return "A half day can only be taken for a single date."
        if start < emp["date_of_joining"]:
            return "These dates are before your date of joining."
        if (end - start).days > 90:
            return "A single request can cover at most 90 days."
        hol = self.holiday_set()
        days = leave_days(start, end, half_day or "", hol)
        if days <= 0:
            return "Those dates are weekends or holidays - no leave is needed."
        for r in self.leave_requests(email=email, statuses=["Pending", "Approved"]):
            if r["start_date"] <= end and r["end_date"] >= start:
                return (f"These dates overlap your {r['status'].lower()} {r['leave_type']} request for "
                        f"{r['start_date']:%d %b} - {r['end_date']:%d %b %Y}.")
        types = {t.code: t for t in self.leave_types()}
        t = types.get(leave_type)
        if not t:
            return "Pick a leave type."
        if t.has_balance:
            bal = self.balance_for(email, as_of=self._balance_date(start))
            avail = (bal.get(leave_type) or {}).get("available") or 0
            if days > avail + 1e-9:
                return (f"Not enough {t.name}: this request needs {days:g} day(s) and {avail:g} are available "
                        f"(after any pending requests). You can apply for Leave Without Pay instead.")
        approver = emp["manager_email"] or ""
        with self._tx() as conn:
            rid = conn.execute(self.leaves.insert().values(
                employee_email=email, leave_type=leave_type, start_date=start, end_date=end,
                half_day=half_day if half_day in ("First half", "Second half") else "", days=days, reason=reason,
                status="Pending", approver_email=approver, applied_at=_now())).inserted_primary_key[0]
        if approver:
            self.notify(approver, f"{emp['full_name']} applied for {days:g} day(s) of {t.name} "
                                  f"({start:%d %b} - {end:%d %b %Y}). Please approve or reject in HR > Approvals.")
        for hr in self.users_with_role("HR Admin"):
            if not approver:
                self.notify(hr, f"{emp['full_name']} applied for leave but has no manager set - please review it "
                                f"in HR > Approvals.")
        return f"ok:{rid}"

    def decide_leave(self, rid: int, decision: str, by_email: str, comment: str, as_hr: bool) -> str:
        self._dirty()
        lr = self.leaves
        with self.engine.connect() as conn:
            r = conn.execute(select(lr).where(lr.c.id == rid)).mappings().first()
        if not r:
            return "Request not found."
        r = dict(r)
        if r["status"] != "Pending":
            return f"This request is already {r['status'].lower()}."
        if r["employee_email"] == by_email:
            return "You cannot approve your own leave."
        if not as_hr and r["approver_email"] != by_email:
            return "Only the employee's manager (or HR) can decide this request."
        if decision == "Approved":
            # re-check the balance at approval time (other leave may have been approved meanwhile)
            types = {t.code: t for t in self.leave_types()}
            t = types.get(r["leave_type"])
            if t and t.has_balance:
                bal = self.balance_for(r["employee_email"], as_of=self._balance_date(r["start_date"]))
                avail = (bal.get(r["leave_type"]) or {}).get("available") or 0
                if avail + r["days"] < r["days"] - 1e-9:   # this request is already counted as pending
                    return "The employee no longer has enough balance for this request."
        with self._tx() as conn:
            conn.execute(lr.update().where(lr.c.id == rid).values(status=decision, decided_by=by_email,
                                                                  decided_at=_now(), decision_comment=comment))
        self.notify(r["employee_email"], f"Your {r['leave_type']} request for {r['start_date']:%d %b} - "
                                         f"{r['end_date']:%d %b %Y} ({r['days']:g} day(s)) was {decision.lower()}"
                                         + (f": {comment}" if comment else "."))
        return ""

    def cancel_leave(self, rid: int, by_email: str, as_hr: bool) -> str:
        self._dirty()
        lr = self.leaves
        with self.engine.connect() as conn:
            r = conn.execute(select(lr).where(lr.c.id == rid)).mappings().first()
        if not r:
            return "Request not found."
        if r["status"] not in ("Pending", "Approved"):
            return f"This request is already {r['status'].lower()}."
        if not as_hr:
            if r["employee_email"] != by_email:
                return "You can only cancel your own leave."
            if r["status"] == "Approved" and r["start_date"] <= today_ist():
                return "This leave has already started. Please ask HR to cancel it."
        with self._tx() as conn:
            conn.execute(lr.update().where(lr.c.id == rid).values(status="Cancelled", decided_by=by_email,
                                                                  decided_at=_now()))
        if r["approver_email"] and r["approver_email"] != by_email:
            self.notify(r["approver_email"], f"A {r['leave_type']} request ({r['start_date']:%d %b} - "
                                             f"{r['end_date']:%d %b %Y}) was cancelled.")
        if as_hr and r["employee_email"] != by_email:
            self.notify(r["employee_email"], f"HR cancelled your {r['leave_type']} request for "
                                             f"{r['start_date']:%d %b} - {r['end_date']:%d %b %Y}.")
        return ""

    def users_with_role(self, role: str) -> List[str]:
        return [u["email"] for u in self._all_users() if u["active"] and role in self.roles_of(u)]

    def lwp_days(self, email: str, year: int, month: int) -> float:
        codes = {t.code for t in self.leave_types(active_only=False) if not t.paid}
        return lwp_days_in_month(self.leave_requests(email=email, statuses=["Approved"]), year, month,
                                 self.holiday_set(), codes)

    # ================================================================= finance
    def settings(self) -> Dict:
        def load():
            out = dict(DEFAULT_SETTINGS)
            with self.engine.connect() as conn:
                for k, v in conn.execute(select(self.settings_t)).all():
                    try:
                        out[k] = float(v) if k not in ("company_name", "company_address") else v
                    except ValueError:
                        out[k] = v
            return out
        return dict(self._c("settings", load))

    def save_settings(self, values: Dict):
        self._dirty()
        with self._tx() as conn:
            for k, v in values.items():
                conn.execute(self.settings_t.delete().where(self.settings_t.c.skey == k))
                conn.execute(self.settings_t.insert().values(skey=k, value=str(v)))

    def salary_history(self, email: str) -> List[Dict]:
        def load():
            s = self.salary
            out = {}
            for r in self._rows(s, (desc(s.c.effective_from), desc(s.c.id))):
                out.setdefault(r["employee_email"], []).append(r)
            return out
        return [{**r, "data": self.vault.unlock(r["data_enc"], {})} for r in self._c("salaries", load).get(email, [])]

    def salary_on(self, email: str, d: date) -> Optional[Dict]:
        for r in self.salary_history(email):
            if r["effective_from"] <= d:
                return r["data"]
        return None

    def save_salary(self, email: str, effective: date, data: Dict, by: str):
        self._dirty()
        with self._tx() as conn:
            conn.execute(self.salary.insert().values(employee_email=email, effective_from=effective,
                                                     data_enc=self.vault.lock(data), created_by=by, created_at=_now()))

    def tax_choice(self, email: str, fy: str) -> Dict:
        def load():
            t = self.tax
            with self.engine.connect() as conn:
                return {r["employee_email"]: dict(r) for r in conn.execute(select(t).where(t.c.fy == fy)).mappings()}
        r = self._c(("tax", fy), load).get(email)
        if not r:
            return {"regime": "new", "decl": {}, "updated_by": None, "updated_at": None}
        return {"regime": r["regime"], "decl": self.vault.unlock(r["data_enc"], {}), "updated_by": r["updated_by"],
                "updated_at": r["updated_at"]}

    def save_tax_choice(self, email: str, fy: str, regime: str, decl: Dict, by: str):
        self._dirty()
        t = self.tax
        with self._tx() as conn:
            conn.execute(t.delete().where(and_(t.c.employee_email == email, t.c.fy == fy)))
            conn.execute(t.insert().values(employee_email=email, fy=fy, regime=regime, data_enc=self.vault.lock(decl),
                                           updated_by=by, updated_at=_now()))

    def get_run(self, month: str) -> Optional[Dict]:
        return next((dict(r) for r in self.list_runs() if r["month"] == month), None)

    def list_runs(self) -> List[Dict]:
        return [dict(r) for r in self._c("runs", lambda: self._rows(self.runs, (desc(self.runs.c.month),)))]

    def save_run(self, month: str, slips: Dict[str, Dict], extras: Dict[str, Dict], by: str) -> int:
        """Create or replace a DRAFT run with the computed payslips."""
        self._dirty()
        with self._tx() as conn:
            run = conn.execute(select(self.runs).where(self.runs.c.month == month)).mappings().first()
            if run and run["status"] == "Published":
                raise ValueError("This month is already published.")
            if run:
                rid = run["id"]
                conn.execute(self.slips.delete().where(self.slips.c.run_id == rid))
                conn.execute(self.runs.update().where(self.runs.c.id == rid).values(created_by=by, created_at=_now()))
            else:
                rid = conn.execute(self.runs.insert().values(month=month, status="Draft", created_by=by,
                                                             created_at=_now())).inserted_primary_key[0]
            for email, slip in slips.items():
                conn.execute(self.slips.insert().values(run_id=rid, employee_email=email,
                                                        data_enc=self.vault.lock(slip),
                                                        extra_enc=self.vault.lock(extras.get(email) or {})))
        return rid

    def publish_run(self, month: str, by: str):
        self._dirty()
        with self._tx() as conn:
            conn.execute(self.runs.update().where(self.runs.c.month == month)
                         .values(status="Published", published_by=by, published_at=_now()))
        run = self.get_run(month)
        for s in self.run_slips(run["id"]):
            self.notify(s["employee_email"], f"Your payslip for {datetime.strptime(month, '%Y-%m'):%B %Y} is ready "
                                             f"in Finance > My Payslips.")

    def delete_draft(self, month: str):
        self._dirty()
        run = self.get_run(month)
        if run and run["status"] == "Draft":
            with self._tx() as conn:
                conn.execute(self.slips.delete().where(self.slips.c.run_id == run["id"]))
                conn.execute(self.runs.delete().where(self.runs.c.id == run["id"]))

    def run_slips(self, run_id: int) -> List[Dict]:
        def load():
            with self.engine.connect() as conn:
                return [dict(r) for r in conn.execute(select(self.slips).where(self.slips.c.run_id == run_id)).mappings()]
        return [{"employee_email": r["employee_email"], "data": self.vault.unlock(r["data_enc"], {}),
                 "extra": self.vault.unlock(r["extra_enc"], {})} for r in self._c(("slips", run_id), load)]

    def my_payslips(self, email: str) -> List[Dict]:
        """Published payslips of one employee, newest first."""
        def load():
            s, r = self.slips, self.runs
            out = {}
            with self.engine.connect() as conn:
                rows = conn.execute(select(s.c.employee_email, r.c.month, s.c.data_enc)
                                    .select_from(s.join(r, s.c.run_id == r.c.id)).where(r.c.status == "Published")
                                    .order_by(desc(r.c.month))).all()
            for em, m, dta in rows:
                out.setdefault(em, []).append((m, dta))
            return out
        return [{"month": m, "data": self.vault.unlock(dta, {})} for m, dta in self._c("published", load).get(email, [])]

    def opening_ytd(self, email: str, fy: str) -> Dict[str, float]:
        def load():
            o = self.opening
            with self.engine.connect() as conn:
                return {r["employee_email"]: dict(r) for r in conn.execute(select(o).where(o.c.fy == fy)).mappings()}
        r = self._c(("opening", fy), load).get(email)
        if not r:
            return {}
        return {**self.vault.unlock(r["data_enc"], {}), "updated_by": r["updated_by"], "updated_at": r["updated_at"]}

    def save_opening_ytd(self, email: str, fy: str, data: Dict, by: str):
        self._dirty()
        o = self.opening
        with self._tx() as conn:
            conn.execute(o.delete().where(and_(o.c.employee_email == email, o.c.fy == fy)))
            conn.execute(o.insert().values(employee_email=email, fy=fy, data_enc=self.vault.lock(data), updated_by=by,
                                           updated_at=_now()))

    def ytd(self, email: str, fy: str, before_month: str) -> Dict[str, float]:
        """
        Tax-year totals before `before_month`, used for TDS: the opening figures Finance entered (pay and tax
        before the Portal was used, or from a previous employer) plus PUBLISHED Portal payslips.
        """
        out = {"gross": 0.0, "tds": 0.0, "pf_vpf": 0.0, "pt": 0.0, "basic": 0.0, "hra": 0.0}
        op = self.opening_ytd(email, fy)
        for k in out:
            out[k] += float(op.get(k) or 0)
        for p in self.my_payslips(email):
            d = p["data"]
            if d.get("fy") != fy or p["month"] >= before_month:
                continue
            out["gross"] += d.get("gross", 0)
            out["tds"] += d.get("deductions", {}).get("Income Tax (TDS)", 0)
            out["pf_vpf"] += d.get("ytd_pf_vpf", 0)
            out["pt"] += d.get("deductions", {}).get("Professional Tax", 0)
            out["basic"] += d.get("ytd_basic", 0)
            out["hra"] += d.get("ytd_hra", 0)
        return out
