"""
MySQL storage for the Ciliconchip.ai Resume Bank.

Connection details come from Streamlit secrets:

    [mysql]
    host = "..."
    port = 3306
    database = "resume_bank"
    user = "..."
    password = "..."
    ssl = true            # optional: encrypt the connection (needed by most cloud MySQL providers)
    ssl_ca = "-----BEGIN CERTIFICATE-----..."   # optional: provider's CA certificate, if they give one

Without a [mysql] section the app falls back to a local SQLite file for testing only.
The table is created automatically on first run.
"""

import hashlib
import hmac
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, LargeBinary, MetaData, String, Table, Text, and_,
    create_engine, desc, func, inspect, or_, select, text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import URL

import numpy as np

from .ai import ai_pct, from_blob

LONGBLOB = LargeBinary().with_variant(mysql.LONGBLOB(), "mysql")
LONGTEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")
PROFILE_STATUSES = ["In process", "Selected", "Rejected", "On hold", "Employee referral", "Other"]

# Who can do what (v1.7)
ROLES = {
    "Admin": "Everything, plus adding people, changing access, deleting resumes and reading the audit log",
    "Recruiter": "Upload, search, match jobs, update profiles and download resumes",
    "Viewer": "Search, match jobs, view and download resumes - no uploads or changes",
}
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
PBKDF2_ROUNDS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = (stored or "").split("$")
        test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(test, digest)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str:
    """'' when the password is acceptable, otherwise what is wrong with it."""
    if len(password or "") < 8:
        return "Use at least 8 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Use both letters and numbers."
    return ""


def temporary_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(10))
        if not password_problem(pw):
            return pw
# Remark wording that means the same as a status, so 'Rejected' also finds 'Rejected - no Swift' in remarks.
STATUS_IN_REMARKS = {
    "Rejected": ["reject"], "Selected": ["selected", "offer released", "offer accepted"],
    "On hold": ["on hold", "on-hold"], "Employee referral": ["referral", "referred", "refered"],
    "In process": ["in process", "in-process"],
}
LOCAL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resume_bank_local.db")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)  # stored as UTC


# Common abbreviations so "AI Engineer" also finds "Artificial Intelligence Engineer" (and vice versa).
SYNONYMS = {
    "ai": ["artificial intelligence"], "artificial intelligence": ["ai"],
    "ml": ["machine learning"], "machine learning": ["ml"],
    "dl": ["deep learning"], "deep learning": ["dl"],
    "genai": ["generative ai", "gen ai"], "gen ai": ["genai", "generative ai"], "generative ai": ["genai", "gen ai"],
    "llm": ["llms", "large language model", "large language models"],
    "nlp": ["natural language processing"], "natural language processing": ["nlp"],
    "cv": ["computer vision"], "computer vision": ["cv"],
    "qa": ["quality assurance", "quality analyst"], "quality assurance": ["qa"],
    "k8s": ["kubernetes"], "kubernetes": ["k8s"],
    "js": ["javascript"], "javascript": ["js"], "ts": ["typescript"],
    "dev ops": ["devops"], "devops": ["dev ops"],
    "ci cd": ["cicd", "continuous integration"], "sde": ["software development engineer", "software engineer"],
    "sdet": ["software development engineer in test"], "ios": ["iphone"],
    "golang": ["go"], "postgres": ["postgresql"], "postgresql": ["postgres"],
    "referral": ["referred", "refer"], "referred": ["referral"],
}


# Small words that carry no meaning in a keyword search: "referred by Arul" = "referred" + "Arul".
STOP_WORDS = set("by from of the a an and for to in at on with is was who whose has have had our my".split())

# Different forms of the same word, so "referred by Arul" also finds "Arul's referral" or "Referral - Arul".
WORD_FAMILIES = [
    ["refer", "refers", "referred", "referral", "referrals", "referring", "refered", "referal"],
    ["reject", "rejected", "rejection", "rejects"],
    ["select", "selected", "selection"],
    ["hold", "held", "parked"],
    ["interview", "interviews", "interviewed"],
    ["offer", "offers", "offered"],
    ["join", "joins", "joined", "joining"],
    ["decline", "declined"],
    ["shortlist", "shortlisted"],
]
_FAMILY = {w: fam for fam in WORD_FAMILIES for w in fam}


def normalize_search(*parts) -> str:
    """
    Lower-case text reduced to single-spaced words, padded with spaces, so a whole-word search is simply
    LIKE '% word %'. Line breaks, slashes and punctuation no longer split or glue words.
    Keeps + # and inner dots so C++, C#, .NET and Node.js still match.
    """
    txt = " ".join(str(p) for p in parts if p).lower()
    txt = re.sub(r"[^a-z0-9+#.]+", " ", txt)
    txt = re.sub(r"\.(?![a-z0-9])", " ", txt)          # drop sentence-ending dots
    return " " + re.sub(r"\s+", " ", txt).strip() + " "


def _term_variants(term: str) -> List[str]:
    """
    The normalised term plus every version with a known abbreviation swapped in or out,
    e.g. 'artificial intelligence engineer' -> also 'ai engineer'.
    """
    base = normalize_search(term).strip()
    if not base:
        return []
    out = [base]
    for key in sorted(SYNONYMS, key=len, reverse=True):
        for cur in list(out):
            if f" {key} " in f" {cur} ":
                for alt in SYNONYMS[key]:
                    out.append(f" {cur} ".replace(f" {key} ", f" {normalize_search(alt).strip()} ").strip())
    return list(dict.fromkeys(out))[:12]


def _word_variants(word: str) -> List[str]:
    out = [word] + [normalize_search(v).strip() for v in SYNONYMS.get(word, [])] + _FAMILY.get(word, [])
    if len(word) > 3:
        out.append(word[:-1] if word.endswith("s") else word + "s")   # engineer <-> engineers
    return list(dict.fromkeys(out))


def query_words(text_: str) -> List[str]:
    """The words of a keyword / AI search that must be found, without small words such as 'by' or 'the'."""
    return [w for w in normalize_search(text_).split() if w not in STOP_WORDS]


def word_in(norm_text: str, word: str) -> bool:
    return any(f" {v} " in norm_text for v in _word_variants(word))


def text_match(norm_text: str, term: str) -> str:
    """'phrase' if the whole term (or a variant) appears, 'words' if every word of a variant appears, else ''."""
    variants = _term_variants(term)
    for v in variants:
        ws = v.split()
        if f" {v} " in norm_text or (len(ws) == 1 and any(f" {x} " in norm_text for x in _word_variants(ws[0]))):
            return "phrase"
    for v in variants:
        ws = v.split()
        if len(ws) > 1 and all(any(f" {x} " in norm_text for x in _word_variants(w)) for w in ws):
            return "words"
    return ""


def _esc(value: str) -> str:
    """Escape LIKE wildcards in user input."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pack(skills: List[str]) -> str:
    """Store skills as |Swift|Xcode| so a search for 'Swift' never matches 'SwiftUI'."""
    clean = [s.strip() for s in skills if s and s.strip()]
    return "|" + "|".join(dict.fromkeys(clean)) + "|" if clean else ""


def _unpack(value: Optional[str]) -> List[str]:
    return [s for s in (value or "").split("|") if s]


class ResumeDB:
    def __init__(self, cfg: Optional[Dict] = None):
        self.is_mysql = bool(cfg)
        if cfg:
            url = URL.create(
                "mysql+pymysql",
                username=str(cfg["user"]),
                password=str(cfg["password"]),
                host=str(cfg["host"]),
                port=int(cfg.get("port", 3306)),
                database=str(cfg["database"]),
                query={"charset": "utf8mb4"},
            )
            connect_args = {"connect_timeout": 10}
            if cfg.get("ssl_ca"):
                ca = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
                ca.write(str(cfg["ssl_ca"]))
                ca.close()
                connect_args["ssl"] = {"ca": ca.name}
            elif str(cfg.get("ssl", "")).lower() in ("true", "1", "yes", "required"):
                import certifi
                connect_args["ssl"] = {"ca": certifi.where()}
            self.engine = create_engine(url, pool_pre_ping=True, pool_recycle=280, pool_size=3,
                                        max_overflow=2, connect_args=connect_args)
        else:
            self.engine = create_engine(f"sqlite:///{LOCAL_DB}")

        self.meta = MetaData()
        self.t = Table(
            "resume_bank", self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("full_name", String(200)),
            Column("email", String(200), index=True),
            Column("phone", String(40)),
            Column("location", String(120), index=True),
            Column("current_role", String(200)),
            Column("primary_skills", Text),
            Column("secondary_skills", Text),
            Column("experience_years", Float, index=True),
            Column("current_ctc", Float),     # lakhs per annum (LPA)
            Column("expected_ctc", Float),    # lakhs per annum (LPA)
            Column("experience_source", String(40)),
            Column("experience_detail", Text),
            Column("education", String(40)),
            Column("notes", Text),
            Column("latest_status", String(40), index=True),
            Column("latest_remark", Text),
            Column("latest_remark_at", DateTime),
            Column("file_name", String(300)),
            Column("file_type", String(10)),
            Column("file_size", Integer),
            Column("file_hash", String(64), unique=True, nullable=False),
            Column("file_data", LONGBLOB),
            Column("resume_text", LONGTEXT),
            Column("search_text", LONGTEXT),   # normalised copy of name/role/skills/resume for searching
            Column("record_text", LONGTEXT),   # normalised name, email, phone, every status/remark and who added it
            Column("embedding", LargeBinary),   # AI meaning fingerprint (384 float32 values)
            Column("embedding_model", String(80)),
            Column("uploaded_by", String(100)),
            Column("uploaded_at", DateTime),
            Column("updated_by", String(100)),
            Column("updated_at", DateTime),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        self.remarks = Table(
            "resume_bank_remarks", self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("resume_id", Integer, ForeignKey("resume_bank.id", ondelete="CASCADE"), nullable=False,
                   index=True),
            Column("status", String(40)),
            Column("remark", Text),
            Column("added_by", String(100)),
            Column("added_at", DateTime),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        self.users = Table(
            "resume_bank_users", self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("email", String(200), unique=True, nullable=False),
            Column("full_name", String(120), nullable=False),
            Column("role", String(20), nullable=False),
            Column("password_hash", String(200), nullable=False),
            Column("must_change_password", Integer, default=1),
            Column("active", Integer, default=1),
            Column("failed_logins", Integer, default=0),
            Column("locked_until", DateTime),
            Column("last_login_at", DateTime),
            Column("created_by", String(200)),
            Column("created_at", DateTime),
            Column("updated_at", DateTime),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        self.audit_t = Table(
            "resume_bank_audit", self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("at", DateTime, index=True),
            Column("user_email", String(200), index=True),
            Column("user_name", String(120)),
            Column("action", String(40), index=True),
            Column("resume_id", Integer),
            Column("details", Text),
            mysql_engine="InnoDB",
            mysql_charset="utf8mb4",
        )
        self.meta.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self):
        """Upgrade a resume_bank table created by v1.0 (before remarks existed)."""
        have = {c["name"] for c in inspect(self.engine).get_columns("resume_bank")}
        wanted = {"latest_status": "VARCHAR(40)", "latest_remark": "TEXT", "latest_remark_at": "DATETIME",
                  "current_ctc": "FLOAT", "expected_ctc": "FLOAT", "search_text": "LONGTEXT",
                  "embedding": "BLOB", "embedding_model": "VARCHAR(80)", "record_text": "LONGTEXT",
                  "updated_by": "VARCHAR(100)"}
        with self.engine.begin() as conn:
            for col, ddl in wanted.items():
                if col not in have:
                    conn.execute(text(f"ALTER TABLE resume_bank ADD COLUMN {col} {ddl}"))
        self._backfill_search_text()
        self._backfill_record_text()

    def _search_blob(self, row) -> str:
        return normalize_search(row["full_name"], row["current_role"], row["location"], row["education"],
                                " ".join(_unpack(row["primary_skills"])), " ".join(_unpack(row["secondary_skills"])),
                                row["resume_text"])

    def _backfill_search_text(self):
        """Resumes saved before v1.3 get their searchable text built once."""
        t = self.t
        with self.engine.begin() as conn:
            rows = conn.execute(select(t.c.id, t.c.full_name, t.c.current_role, t.c.location, t.c.education,
                                       t.c.primary_skills, t.c.secondary_skills, t.c.resume_text)
                                .where(t.c.search_text.is_(None))).mappings().all()
            for r in rows:
                conn.execute(t.update().where(t.c.id == r["id"]).values(search_text=self._search_blob(r)))

    @staticmethod
    def _record_blob(row, remarks) -> str:
        """Name, email, phone (searchable as 9876543210 or 98765 43210) and every status, remark and its author."""
        d = re.sub(r"\D", "", row["phone"] or "")[-10:]
        phone = f"{d} {d[:5]} {d[5:]}" if len(d) == 10 else (row["phone"] or "")
        email = row["email"] or ""
        notes = " ".join(f"{m['status'] or ''} {m['remark'] or ''} {m['added_by'] or ''}" for m in remarks)
        return normalize_search(row["full_name"], email, email.split("@")[0].replace(".", " ").replace("_", " "),
                                phone, notes)

    def _refresh_record_text(self, conn, rid: int):
        t, r = self.t, self.remarks
        row = conn.execute(select(t.c.full_name, t.c.email, t.c.phone).where(t.c.id == rid)).mappings().first()
        if not row:
            return
        rem = conn.execute(select(r.c.status, r.c.remark, r.c.added_by).where(r.c.resume_id == rid)).mappings().all()
        conn.execute(t.update().where(t.c.id == rid).values(record_text=self._record_blob(row, rem)))

    def _backfill_record_text(self):
        """Resumes saved before v1.5 get their name / contact / remarks text built once."""
        with self.engine.begin() as conn:
            for (rid,) in conn.execute(select(self.t.c.id).where(self.t.c.record_text.is_(None))).all():
                self._refresh_record_text(conn, rid)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def file_hash(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def phone_key(phone: str) -> str:
        """Last 10 digits, so '+91 96764 40530', '09676440530' and '9676440530' all match."""
        digits = re.sub(r"\D", "", phone or "")
        return digits[-10:] if len(digits) >= 10 else ""

    @staticmethod
    def name_key(name: str) -> str:
        return re.sub(r"[^a-z]", "", (name or "").lower())

    def find_duplicate(self, file_hash: str = "", email: str = "", full_name: str = "",
                       phone: str = "") -> Optional[Dict]:
        """
        A profile already exists when ANY of these match an existing record:
          - the same email address
          - the same name AND the same mobile number
          - the exact same resume file
        Returns the existing record with the reason, or None.
        """
        t = self.t
        cols = (t.c.id, t.c.full_name, t.c.email, t.c.phone, t.c.uploaded_at, t.c.latest_status, t.c.file_hash)
        email = (email or "").strip().lower()
        nkey, pkey = self.name_key(full_name), self.phone_key(phone)
        with self.engine.connect() as conn:
            if email:
                row = conn.execute(select(*cols).where(t.c.email == email).limit(1)).mappings().first()
                if row:
                    return {**dict(row), "reason": "same email"}
            if nkey and pkey:
                for row in conn.execute(select(*cols).where(t.c.phone.like(f"%{pkey[-4:]}%"))).mappings():
                    if self.phone_key(row["phone"]) == pkey and self.name_key(row["full_name"]) == nkey:
                        return {**dict(row), "reason": "same name and mobile number"}
            if file_hash:
                row = conn.execute(select(*cols).where(t.c.file_hash == file_hash).limit(1)).mappings().first()
                if row:
                    return {**dict(row), "reason": "same resume file"}
        return None

    # --------------------------------------------------------------------- save
    def save(self, record: Dict, file_bytes: bytes, file_name: str, uploaded_by: str = "",
             status: str = "", remark: str = "", embedding: Optional[bytes] = None,
             embedding_model: str = "") -> str:
        """Insert a new resume. Returns 'new', or 'duplicate' (nothing saved) when the profile already exists."""
        t = self.t
        h = self.file_hash(file_bytes)
        now = _now()
        values = {
            "full_name": (record.get("full_name") or "")[:200],
            "email": (record.get("email") or "").lower()[:200],
            "phone": (record.get("phone") or "")[:40],
            "location": (record.get("location") or "")[:120],
            "current_role": (record.get("current_role") or "")[:200],
            "primary_skills": _pack(record.get("primary_skills") or []),
            "secondary_skills": _pack(record.get("secondary_skills") or []),
            "experience_years": record.get("experience_years"),
            "current_ctc": record.get("current_ctc"),
            "expected_ctc": record.get("expected_ctc"),
            "experience_source": (record.get("experience_source") or "")[:40],
            "experience_detail": record.get("experience_detail") or "",
            "education": (record.get("education") or "")[:40],
            "notes": record.get("notes") or "",
            "file_name": file_name[:300],
            "file_type": file_name.rsplit(".", 1)[-1].lower()[:10] if "." in file_name else "",
            "file_size": len(file_bytes),
            "file_hash": h,
            "file_data": file_bytes,
            "resume_text": record.get("resume_text") or "",
            "search_text": "",
            "uploaded_by": (uploaded_by or "")[:100],
            "updated_by": (uploaded_by or "")[:100],
            "updated_at": now,
        }
        values["search_text"] = self._search_blob(values)
        if embedding:
            values["embedding"], values["embedding_model"] = embedding, embedding_model[:80]
        if self.find_duplicate(h, values["email"], values["full_name"], values["phone"]):
            return "duplicate"
        with self.engine.begin() as conn:
            rid = conn.execute(t.insert().values(**values, uploaded_at=now)).inserted_primary_key[0]
            if (status or "").strip() or (remark or "").strip():
                self._add_remark(conn, rid, status, remark, uploaded_by, now)
            else:
                self._refresh_record_text(conn, rid)
        return "new"

    # ------------------------------------------------------------------ remarks
    def _add_remark(self, conn, rid: int, status: str, remark: str, added_by: str, when):
        status = (status or "").strip()[:40]
        remark = (remark or "").strip()
        conn.execute(self.remarks.insert().values(resume_id=rid, status=status, remark=remark,
                                                  added_by=(added_by or "")[:100], added_at=when))
        latest = {"latest_remark_at": when, "updated_at": when, "updated_by": (added_by or "")[:100]}
        if remark:
            latest["latest_remark"] = remark
        if status:
            latest["latest_status"] = status
        conn.execute(self.t.update().where(self.t.c.id == rid).values(**latest))
        self._refresh_record_text(conn, rid)

    def add_remark(self, rid: int, status: str = "", remark: str = "", added_by: str = ""):
        with self.engine.begin() as conn:
            self._add_remark(conn, rid, status, remark, added_by, _now())

    def update_profile(self, rid: int, status: str = "", remark: str = "", added_by: str = "",
                       current_ctc=None, expected_ctc=None, old_current=None, old_expected=None):
        """Save a remark and/or new CTC figures; CTC changes are written into the remarks history."""
        def change(label, old, new):
            if new == old:
                return ""
            if old is None:
                return f"{label} set to {new:g} LPA"
            if new is None:
                return f"{label} cleared (was {old:g} LPA)"
            return f"{label} changed from {old:g} to {new:g} LPA"
        notes = [n for n in (change("Current CTC", old_current, current_ctc),
                             change("Expected CTC", old_expected, expected_ctc)) if n]
        text_ = "; ".join(filter(None, [(remark or "").strip(), *notes]))
        now = _now()
        with self.engine.begin() as conn:
            if notes:
                conn.execute(self.t.update().where(self.t.c.id == rid)
                             .values(current_ctc=current_ctc, expected_ctc=expected_ctc, updated_at=now,
                                     updated_by=(added_by or "")[:100]))
            if (status or "").strip() or text_:
                self._add_remark(conn, rid, status, text_, added_by, now)

    def remark_history(self, rid: int) -> List[Dict]:
        r = self.remarks
        with self.engine.connect() as conn:
            return [dict(x) for x in conn.execute(
                select(r.c.status, r.c.remark, r.c.added_by, r.c.added_at)
                .where(r.c.resume_id == rid).order_by(desc(r.c.added_at), desc(r.c.id))).mappings()]

    # ------------------------------------------------------------------- search
    def _text_cond(self, term: str):
        """SQL twin of text_match(): a phrase variant appears, or all words of a variant appear."""
        st = self.t.c.search_text
        like = lambda v: st.like(f"% {_esc(v)} %", escape="\\")
        conds = []
        for v in _term_variants(term):
            ws = v.split()
            if len(ws) == 1:
                conds += [like(x) for x in _word_variants(ws[0])]
            else:
                conds.append(like(v))
                conds.append(and_(*[or_(*[like(x) for x in _word_variants(w)]) for w in ws]))
        return or_(*conds) if conds else text("1=1")

    def _word_cond(self, word: str):
        """SQL: the word (or another form of it) is in the resume, or in the name / contact / remarks text."""
        t = self.t
        conds = []
        for v in _word_variants(word):
            pat = f"% {_esc(v)} %"
            conds += [t.c.search_text.like(pat, escape="\\"), t.c.record_text.like(pat, escape="\\")]
        return or_(*conds)

    def _words_cond(self, words: List[str]):
        return and_(*[self._word_cond(w) for w in words]) if words else text("1=1")

    def _remark_cond(self, term: str):
        """SQL: some remark for the resume contains every word of the term."""
        r = self.remarks
        words = [w for w in re.split(r"\s+", term.lower().strip()) if w]
        sub = select(r.c.resume_id).where(and_(*[func.lower(r.c.remark).like(f"%{_esc(w)}%", escape="\\")
                                                 for w in words]))
        return self.t.c.id.in_(sub)

    def _filter_conds(self, skills, match_all, known_skills, min_exp, max_exp, include_unknown_exp,
                      location, terms, statuses, min_expected_ctc, max_expected_ctc, max_current_ctc,
                      include_unknown_ctc, exclude_statuses=None, uploaded_after=None):
        t = self.t
        known = {k.lower() for k in (known_skills or [])}
        conds = []

        if skills:
            per_skill = []
            for s_ in skills:
                c = self._text_cond(s_)
                if s_.lower() in known:
                    c = or_(t.c.primary_skills.like(f"%|{_esc(s_)}|%", escape="\\"),
                            t.c.secondary_skills.like(f"%|{_esc(s_)}|%", escape="\\"), c)
                per_skill.append(c)
            conds.append(and_(*per_skill) if match_all else or_(*per_skill))

        exp = []
        if min_exp is not None:
            exp.append(t.c.experience_years >= min_exp)
        if max_exp is not None:
            exp.append(t.c.experience_years <= max_exp)
        if exp:
            conds.append(or_(and_(*exp), t.c.experience_years.is_(None)) if include_unknown_exp else and_(*exp))

        ctc = []
        if min_expected_ctc is not None:
            ctc.append(or_(t.c.expected_ctc >= min_expected_ctc,
                           t.c.expected_ctc.is_(None)) if include_unknown_ctc else t.c.expected_ctc >= min_expected_ctc)
        if max_expected_ctc is not None:
            ctc.append(or_(t.c.expected_ctc <= max_expected_ctc,
                           t.c.expected_ctc.is_(None)) if include_unknown_ctc else t.c.expected_ctc <= max_expected_ctc)
        if max_current_ctc is not None:
            ctc.append(or_(t.c.current_ctc <= max_current_ctc,
                           t.c.current_ctc.is_(None)) if include_unknown_ctc else t.c.current_ctc <= max_current_ctc)
        conds += ctc

        locs = [l.strip() for l in (location or "").split(",") if l.strip()]
        if locs:   # several locations = any of them
            conds.append(or_(*[func.lower(t.c.location).like(f"%{_esc(l.lower())}%", escape="\\") for l in locs]))

        for term in terms:
            # several keywords (comma-separated) = all of them. Inside one keyword every word must be found,
            # in any order, anywhere in the profile: resume, name, email, phone, statuses, remarks, who added them.
            words = query_words(term)
            if words:
                conds.append(self._words_cond(words))

        if statuses:
            # status field, OR any remark that says the same thing (e.g. 'Rejected - weak in Swift')
            words = [w for s_ in statuses for w in STATUS_IN_REMARKS.get(s_, [])]
            if words:
                r = self.remarks
                in_remarks = select(r.c.resume_id).where(
                    or_(*[func.lower(r.c.remark).like(f"%{_esc(w)}%", escape="\\") for w in words]))
                conds.append(or_(t.c.latest_status.in_(statuses), t.c.id.in_(in_remarks)))
            else:
                conds.append(t.c.latest_status.in_(statuses))

        if exclude_statuses:
            # leave out profiles with that status, or whose remarks say it ('Rejected - weak in Swift')
            words = [w for s_ in exclude_statuses for w in STATUS_IN_REMARKS.get(s_, [])]
            bad = [func.coalesce(t.c.latest_status, "").in_(exclude_statuses)]   # NULL status is kept
            if words:
                r = self.remarks
                bad.append(t.c.id.in_(select(r.c.resume_id).where(
                    or_(*[func.lower(r.c.remark).like(f"%{_esc(w)}%", escape="\\") for w in words]))))
            conds.append(~or_(*bad))

        if uploaded_after is not None:
            conds.append(t.c.uploaded_at >= uploaded_after)

        return conds

    def search(self, skills: Optional[List[str]] = None, match_all: bool = True,
               known_skills: Optional[List[str]] = None, min_exp: Optional[float] = None,
               max_exp: Optional[float] = None, include_unknown_exp: bool = False,
               location: str = "", keywords: str = "", statuses: Optional[List[str]] = None,
               min_expected_ctc: Optional[float] = None, max_expected_ctc: Optional[float] = None,
               max_current_ctc: Optional[float] = None, include_unknown_ctc: bool = True,
               query_vec: Optional[np.ndarray] = None, model_name: str = "", min_ai: float = 0.0,
               query_text: str = "", exclude_statuses: Optional[List[str]] = None, uploaded_after=None,
               limit: int = 200) -> List[Dict]:
        """
        Every filter that is filled in must hold (AND). Inside the skills box, 'All skills' needs every
        skill and 'Any skill' needs at least one. A skill matches when it is in the stored skill lists OR
        appears anywhere in the resume text (whole words, abbreviations understood).

        AI search box (query_text / query_vec): a profile is shown when EITHER every typed word is found in it
        (name, email, phone, remarks or resume - so a name like 'Mahesh' always works), OR its AI meaning match
        reaches `min_ai`.
        """
        t = self.t
        skills = [s.strip() for s in (skills or []) if s and s.strip()]
        terms = [k.strip() for k in (keywords or "").split(",") if k.strip()]
        conds = self._filter_conds(skills, match_all, known_skills, min_exp, max_exp, include_unknown_exp,
                                   location, terms, statuses, min_expected_ctc, max_expected_ctc,
                                   max_current_ctc, include_unknown_ctc, exclude_statuses, uploaded_after)

        ai_scores, all_scores, literal = {}, {}, set()
        qwords = query_words(query_text)
        self.last_ai_stats = None
        if query_vec is not None or qwords:
            with self.engine.connect() as conn:
                n_pass = conn.execute(select(func.count()).select_from(t).where(and_(*conds) if conds
                                                                                else text("1=1"))).scalar() or 0
                if qwords:   # every typed word found literally (name, contact, remarks, resume)
                    literal = {r_[0] for r_ in conn.execute(select(t.c.id).where(and_(self._words_cond(qwords),
                                                                                      *conds))).all()}
                cand = []
                if query_vec is not None:
                    cand = conn.execute(select(t.c.id, t.c.embedding)
                                        .where(and_(t.c.embedding_model == model_name, *conds))).all()
            for rid, blob in cand:
                vec = from_blob(blob)
                if vec is not None:
                    all_scores[rid] = ai_pct(float(np.dot(vec, query_vec)))
            ai_scores = {k: v for k, v in all_scores.items() if v >= min_ai}
            self.last_ai_stats = {"passed_filters": n_pass, "above_min": len(ai_scores), "literal": len(literal),
                                  "best": max(all_scores.values(), default=None)}
            keep = sorted(set(ai_scores) | literal,
                          key=lambda k: (k in literal, all_scores.get(k, 0)), reverse=True)[:limit]
            conds = [t.c.id.in_(keep or [-1])]

        q = (select(t.c.id, t.c.full_name, t.c.email, t.c.phone, t.c.location, t.c.current_role,
                    t.c.primary_skills, t.c.secondary_skills, t.c.experience_years, t.c.experience_detail,
                    t.c.education, t.c.file_name, t.c.uploaded_at, t.c.updated_at, t.c.latest_status,
                    t.c.latest_remark, t.c.latest_remark_at, t.c.current_ctc, t.c.expected_ctc, t.c.search_text,
                    t.c.record_text, t.c.uploaded_by, t.c.updated_by)
             .where(and_(*conds) if conds else text("1=1"))
             .order_by(desc(t.c.updated_at))
             .limit(limit))
        with self.engine.connect() as conn:
            rows = [dict(r) for r in conn.execute(q).mappings().all()]

        # ---- rank and explain what matched
        for r in rows:
            r["primary_skills"] = _unpack(r["primary_skills"])
            r["secondary_skills"] = _unpack(r["secondary_skills"])
            prim = {x.lower() for x in r["primary_skills"]}
            sec = {x.lower() for x in r["secondary_skills"]}
            norm = r.pop("search_text") or ""
            rec = r.pop("record_text") or ""
            score, matched = 0, []
            for s_ in skills:
                if s_.lower() in prim:
                    score += 4; matched.append(s_)
                elif s_.lower() in sec:
                    score += 3; matched.append(s_)
                else:
                    how = text_match(norm, s_)
                    if how == "phrase":
                        score += 2; matched.append(f"{s_} (in resume)")
                    elif how == "words":
                        score += 1; matched.append(f"{s_} (words in resume)")
            for term in terms:
                if text_match(norm, term) == "phrase":
                    score += 1
            r["matched_skills"] = matched
            r["match_score"] = score
            r["ai_match"] = all_scores.get(r["id"])
            r["found_by"] = ""
            tier = 0
            if qwords:
                name_norm = normalize_search(r["full_name"])
                if all(word_in(name_norm, w) for w in qwords):
                    r["found_by"], tier = "Name", 3
                elif all(word_in(rec, w) for w in qwords):
                    r["found_by"], tier = "Contact / remarks", 2
                elif r["id"] in literal:
                    r["found_by"], tier = "Words in profile", 1
                else:
                    r["found_by"] = "AI meaning"
            elif query_vec is not None:
                r["found_by"] = "AI meaning"
            r["_rank"] = (tier, (r["ai_match"] or 0) + score * 5, score, r["experience_years"] or 0) \
                if (query_vec is not None or qwords) else (score, r["experience_years"] or 0)
        rows.sort(key=lambda r: r["_rank"], reverse=True)
        return rows

    # ------------------------------------------------------------------- others
    def get_file(self, rid: int):
        t = self.t
        with self.engine.connect() as conn:
            r = conn.execute(select(t.c.file_name, t.c.file_data).where(t.c.id == rid)).first()
        return (r.file_name, r.file_data) if r else (None, None)

    def get_files(self, ids: List[int]):
        t = self.t
        with self.engine.connect() as conn:
            return [(r.id, r.full_name, r.file_name, r.file_data)
                    for r in conn.execute(select(t.c.id, t.c.full_name, t.c.file_name, t.c.file_data)
                                          .where(t.c.id.in_(ids)))]

    def known_skills(self) -> List[str]:
        t = self.t
        found = set()
        with self.engine.connect() as conn:
            for p, s in conn.execute(select(t.c.primary_skills, t.c.secondary_skills)):
                found.update(_unpack(p))
                found.update(_unpack(s))
        return sorted(found, key=str.lower)

    # ------------------------------------------------------------------- AI
    def needs_embedding(self, model_name: str) -> List[Dict]:
        """Resumes without a fingerprint from the current model (saved before v1.4, or model changed)."""
        t = self.t
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(
                select(t.c.id, t.c.current_role, t.c.primary_skills, t.c.secondary_skills, t.c.experience_years,
                       t.c.resume_text)
                .where(or_(t.c.embedding_model.is_(None), t.c.embedding_model != model_name))).mappings()]

    def set_embeddings(self, items, model_name: str):
        """items: iterable of (resume id, fingerprint bytes)."""
        with self.engine.begin() as conn:
            for rid, blob in items:
                conn.execute(self.t.update().where(self.t.c.id == rid)
                             .values(embedding=blob, embedding_model=model_name[:80]))

    def ai_coverage(self, model_name: str):
        t = self.t
        with self.engine.connect() as conn:
            done = conn.execute(select(func.count()).select_from(t)
                                .where(t.c.embedding_model == model_name)).scalar() or 0
        return done, self.count()

    def _all_vectors(self, model_name: str, exclude_statuses=None, exclude_id=None):
        t = self.t
        conds = [t.c.embedding_model == model_name]
        if exclude_statuses:
            conds.append(or_(t.c.latest_status.is_(None), t.c.latest_status.notin_(exclude_statuses)))
        if exclude_id is not None:
            conds.append(t.c.id != exclude_id)
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.c.id, t.c.embedding).where(and_(*conds))).all()
        ids, vecs = [], []
        for rid, blob in rows:
            v = from_blob(blob)
            if v is not None:
                ids.append(rid)
                vecs.append(v)
        return ids, (np.vstack(vecs) if vecs else np.zeros((0, 384), dtype=np.float32))

    def _details(self, ids: List[int]) -> Dict[int, Dict]:
        t = self.t
        if not ids:
            return {}
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.c.id, t.c.full_name, t.c.current_role, t.c.location, t.c.experience_years,
                                       t.c.primary_skills, t.c.secondary_skills, t.c.latest_status,
                                       t.c.latest_remark, t.c.current_ctc, t.c.expected_ctc, t.c.file_name,
                                       t.c.email, t.c.phone, t.c.search_text)
                                .where(t.c.id.in_(ids))).mappings().all()
        out = {}
        for r in rows:
            d = dict(r)
            d["primary_skills"] = _unpack(d["primary_skills"])
            d["secondary_skills"] = _unpack(d["secondary_skills"])
            out[d["id"]] = d
        return out

    def similar(self, rid: int, model_name: str, k: int = 5) -> List[Dict]:
        """The k profiles whose AI fingerprints are closest to this one."""
        t = self.t
        with self.engine.connect() as conn:
            blob = conn.execute(select(t.c.embedding).where(and_(t.c.id == rid,
                                                                 t.c.embedding_model == model_name))).scalar()
        target = from_blob(blob)
        if target is None:
            return []
        ids, mat = self._all_vectors(model_name, exclude_id=rid)
        if not ids:
            return []
        sims = mat @ target
        order = np.argsort(-sims)[:k]
        det = self._details([ids[i] for i in order])
        out = []
        for i in order:
            d = det.get(ids[i])
            if d:
                d.pop("search_text", None)
                d["ai_similarity"] = ai_pct(float(sims[i]))
                out.append(d)
        return out

    def match_jd(self, jd_vec: np.ndarray, jd_skills: List[str], required_years: Optional[float],
                 model_name: str, exclude_statuses=None, top: int = 25, shortlist: int = 150,
                 min_fit: float = 60.0, min_skill_ratio: float = 0.6, min_exp_ratio: Optional[float] = 0.75):
        """
        Rank the whole bank against a Job Description.
        Stage 1 - AI: meaning match between the JD and every resume fingerprint; keep the best `shortlist`.
        Stage 2 - evidence: JD skills found in each shortlisted resume, and experience vs the requirement.
        Overall fit = 50% AI meaning match + 35% skill coverage + 15% experience fit
        (weights re-balanced when the JD lists no skills or no experience).
        Only profiles that QUALIFY are returned:
          - at least `min_skill_ratio` of the JD skills found (skipped when the JD lists no skills),
          - experience >= `min_exp_ratio` x required years (skipped when not stated; unknown experience passes),
          - overall fit >= `min_fit`.
        Returns (qualified rows, {"checked": n, "qualified": k}).
        """
        ids, mat = self._all_vectors(model_name, exclude_statuses=exclude_statuses)
        if not ids:
            return [], {"checked": 0, "qualified": 0}
        sims = mat @ jd_vec
        order = np.argsort(-sims)[:shortlist]
        det = self._details([ids[i] for i in order])
        results = []
        for i in order:
            d = det.get(ids[i])
            if not d:
                continue
            norm = d.pop("search_text") or ""
            have = {x.lower() for x in d["primary_skills"] + d["secondary_skills"]}
            matched = [s_ for s_ in jd_skills if s_.lower() in have or text_match(norm, s_) == "phrase"]
            missing = [s_ for s_ in jd_skills if s_ not in matched]
            ai = ai_pct(float(sims[i]))
            parts = {"ai": (ai, 0.50)}
            if jd_skills:
                parts["skills"] = (len(matched) / len(jd_skills) * 100, 0.35)
            if required_years:
                yrs = d["experience_years"]
                parts["exp"] = (50.0 if yrs is None else min(100.0, yrs / required_years * 100), 0.15)
            wsum = sum(w for _, w in parts.values())
            d["overall_fit"] = sum(v * w for v, w in parts.values()) / wsum
            d["ai_match"] = ai
            d["skills_matched"] = matched
            d["skills_missing"] = missing
            # ---- qualification gates: non-matching profiles are never returned
            if jd_skills and len(matched) < max(1, int(np.ceil(min_skill_ratio * len(jd_skills)))) \
                    and min_skill_ratio > 0:
                continue
            yrs = d["experience_years"]
            if required_years and min_exp_ratio and yrs is not None and yrs < required_years * min_exp_ratio:
                continue
            if d["overall_fit"] < min_fit:
                continue
            results.append(d)
        results.sort(key=lambda d: d["overall_fit"], reverse=True)
        return results[:top], {"checked": len(ids), "qualified": len(results)}

    def recent(self, limit: int = 10) -> List[Dict]:
        """Most recently added resumes, read straight from the database."""
        t = self.t
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(
                select(t.c.full_name, t.c.current_role, t.c.experience_years, t.c.latest_status,
                       t.c.uploaded_by, t.c.uploaded_at, t.c.updated_by, t.c.updated_at).order_by(desc(t.c.uploaded_at), desc(t.c.id)).limit(limit)
            ).mappings()]

    def count(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(self.t)).scalar() or 0

    def delete(self, rid: int):
        with self.engine.begin() as conn:
            conn.execute(self.remarks.delete().where(self.remarks.c.resume_id == rid))
            conn.execute(self.t.delete().where(self.t.c.id == rid))

    # ===================================================================== users (v1.7)
    def user_count(self, active_admins_only: bool = False) -> int:
        u = self.users
        q = select(func.count()).select_from(u)
        if active_admins_only:
            q = q.where(and_(u.c.role == "Admin", u.c.active == 1))
        with self.engine.connect() as conn:
            return conn.execute(q).scalar() or 0

    def get_user(self, email: str) -> Optional[Dict]:
        u = self.users
        with self.engine.connect() as conn:
            row = conn.execute(select(u).where(u.c.email == (email or "").strip().lower())).mappings().first()
        return dict(row) if row else None

    def list_users(self) -> List[Dict]:
        u = self.users
        with self.engine.connect() as conn:
            return [dict(r) for r in conn.execute(
                select(u.c.full_name, u.c.email, u.c.role, u.c.active, u.c.must_change_password, u.c.last_login_at,
                       u.c.locked_until, u.c.created_by, u.c.created_at).order_by(desc(u.c.active), u.c.full_name)
            ).mappings()]

    def create_user(self, email: str, full_name: str, role: str, password: str, created_by: str = "",
                    must_change: bool = True) -> str:
        """Returns '' when created, otherwise the reason it was not."""
        email = (email or "").strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            return "Enter a valid email address."
        if not (full_name or "").strip():
            return "Enter the person's name."
        if role not in ROLES:
            return "Pick a role."
        if self.get_user(email):
            return "This email already has access."
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(self.users.insert().values(
                email=email, full_name=full_name.strip()[:120], role=role, password_hash=hash_password(password),
                must_change_password=1 if must_change else 0, active=1, failed_logins=0,
                created_by=(created_by or "")[:200], created_at=now, updated_at=now))
        return ""

    def update_user(self, email: str, **values):
        values["updated_at"] = _now()
        with self.engine.begin() as conn:
            conn.execute(self.users.update().where(self.users.c.email == email.strip().lower()).values(**values))

    def set_password(self, email: str, password: str, must_change: bool):
        self.update_user(email, password_hash=hash_password(password), must_change_password=1 if must_change else 0,
                         failed_logins=0, locked_until=None)

    def verify_login(self, email: str, password: str):
        """Returns (user, '') on success, or (None, message)."""
        user = self.get_user(email)
        now = _now()
        if not user or not user["active"]:
            if user is None:
                check_password(password, hash_password("timing-equaliser"))   # same effort either way
            return None, "Email or password is not correct, or this account has no access."
        if user["locked_until"] and user["locked_until"] > now:
            mins = max(1, int((user["locked_until"] - now).total_seconds() // 60) + 1)
            return None, f"Too many wrong attempts. This account is locked for {mins} more minute(s)."
        if not check_password(password, user["password_hash"]):
            fails = (user["failed_logins"] or 0) + 1
            if fails >= MAX_FAILED_LOGINS:
                self.update_user(email, failed_logins=0, locked_until=now + timedelta(minutes=LOCK_MINUTES))
                return None, f"Too many wrong attempts. This account is locked for {LOCK_MINUTES} minutes."
            self.update_user(email, failed_logins=fails)
            return None, "Email or password is not correct, or this account has no access."
        self.update_user(email, failed_logins=0, locked_until=None, last_login_at=now)
        user["last_login_at"] = now
        return user, ""

    # ===================================================================== audit log (v1.7)
    def audit(self, user: Optional[Dict], action: str, details: str = "", resume_id: Optional[int] = None):
        user = user or {}
        try:
            with self.engine.begin() as conn:
                conn.execute(self.audit_t.insert().values(
                    at=_now(), user_email=(user.get("email") or "")[:200], user_name=(user.get("full_name") or "")[:120],
                    action=action[:40], resume_id=resume_id, details=(details or "")[:4000]))
        except Exception:
            pass   # never block the user's work because the log could not be written

    def audit_log(self, user_email: str = "", actions: Optional[List[str]] = None, since=None, until=None,
                  limit: int = 2000) -> List[Dict]:
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
            return [dict(r) for r in conn.execute(
                select(a).where(and_(*conds) if conds else text("1=1")).order_by(desc(a.c.at), desc(a.c.id))
                .limit(limit)).mappings()]
