"""
Understanding a plain-English request typed into the AI box (v1.6).

    "Java developers in Hyderabad with 5+ years, expected CTC under 20 LPA, not rejected"
    -> skills Java, location Hyderabad, experience >= 5, expected CTC <= 20, leave out Rejected,
       and "java developers" for the AI meaning match.

Everything is rule-based and runs inside the app: no paid AI service, and no candidate data leaves it.
Whatever is not understood as a filter is still used for the name / word match and the AI meaning match.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .ai import FILLER, STATUS_WORDS
from .extractor import ALL_SKILLS, CITIES, canonical_city

IST = timezone(timedelta(hours=5, minutes=30))

_NUM = r"(\d+(?:\.\d+)?)"
_YRS = r"(?:years?|yrs?|y)\b"
_LPA = r"(?:lpa|lakhs?|lacs?|l\b|lakh per annum)"
_CITY_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(c.lower()) for c in sorted(CITIES, key=len, reverse=True))
                      + r")(?![a-z])")
_NEG = r"(?:not|non|no|exclude|excluding|except|without|other than|leave out|minus)"
EXTRA_FILLER = set("""
experience experienced exp yr yrs year years total overall having around about approx approximately
lpa lakh lakhs ctc salary package expected expecting expectation current currently
how many count number total list show who which what are there do we our
most least highest lowest senior seniormost junior latest newest recent recently oldest
today yesterday week month days last this past since added uploaded
""".split())


SMALL = set("""
and or to at on as it also only just more less came come comes coming got received sent shared submitted
using use used done did does there here into onto within so such any some every each
""".split())


def _fmt(v: float) -> str:
    return f"{v:g}"


def parse_request(query: str, now: Optional[datetime] = None) -> Dict:
    """
    Returns a dict with any of: statuses, exclude_statuses, min_exp, max_exp, min_expected_ctc,
    max_expected_ctc, max_current_ctc, location, skills, uploaded_after, sort, count_question,
    and always: words (what is left for the name / word match), meaning (text for the AI meaning match),
    understood (list of plain-English descriptions of what was taken from the request).
    """
    now = now or datetime.now(IST)
    q = " " + re.sub(r"\s+", " ", (query or "").lower()).strip() + " "
    # phone numbers typed with spaces or dashes ('96764 40530', '+91-96764-40530') become one number
    q = re.sub(r"(?<![\d.])\+?\d[\d\s\-]{8,16}\d(?![\d.])",
               lambda m: " " + re.sub(r"\D", "", m.group(0))[-10:] + " "
               if 10 <= len(re.sub(r"\D", "", m.group(0))) <= 12 else m.group(0), q)
    q = re.sub(r"\bnot selected\b", "rejected", q)      # recruiting usage: 'not selected' = rejected
    out: Dict = {"understood": []}
    said = out["understood"]

    def cut(pattern):
        nonlocal q
        q = re.sub(pattern, " ", q)

    # ---- counting questions / ordering
    if re.search(r"\b(how many|count of|number of|total number)\b", q):
        out["count_question"] = True
        said.append("a count question")
    if re.search(r"\b(most experienced|most\b[a-z/+#. ]{0,30}\bexperience|senior ?most|highest experience|"
                 r"more experienced first)\b", q):
        out["sort"] = "exp_desc"; said.append("most experienced first")
    elif re.search(r"\b(least experienced|least experience|freshers?|lowest experience|junior ?most)\b", q):
        out["sort"] = "exp_asc"; said.append("least experienced first")
    elif re.search(r"\b(latest|newest|most recent|recently added|recent)\b", q):
        out["sort"] = "recent"; said.append("newest first")

    # ---- when added
    after, label = None, ""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    m = re.search(r"\b(?:last|past|previous)\s+(\d+)\s+(days?|weeks?|months?)\b", q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
        after, label = today - timedelta(days=days - 1), f"last {n} {unit}"
        cut(m.re.pattern)
    elif re.search(r"\btoday\b", q):
        after, label = today, "today"
    elif re.search(r"\byesterday\b", q):
        after, label = today - timedelta(days=1), "since yesterday"
    elif re.search(r"\bthis week\b", q):
        after, label = today - timedelta(days=today.weekday()), "this week"
    elif re.search(r"\b(?:last|past) week\b", q):
        after, label = today - timedelta(days=7), "in the last 7 days"
    elif re.search(r"\bthis month\b", q):
        after, label = today.replace(day=1), "this month"
    elif re.search(r"\b(?:last|past) month\b", q):
        after, label = today - timedelta(days=30), "in the last 30 days"
    if after is not None:
        out["uploaded_after"] = after.astimezone(timezone.utc).replace(tzinfo=None)
        said.append(f"added {label}")

    # ---- CTC (before experience, so '20 lpa' is never read as years)
    ctc_kind = lambda span: "current" if re.search(r"\bcurrent|\bpresent|\bdrawing\b", span) else "expected"
    m = re.search(rf"(?:([a-z ]{{0,25}}?)\s)?{_NUM}\s*(?:-|to|and)\s*{_NUM}\s*{_LPA}", q)
    if m:
        kind = ctc_kind(m.group(1) or "")
        lo, hi = float(m.group(2)), float(m.group(3))
        if kind == "expected":
            out["min_expected_ctc"], out["max_expected_ctc"] = lo, hi
        else:
            out["max_current_ctc"] = hi
        said.append(f"{kind} CTC {_fmt(lo)}-{_fmt(hi)} LPA")
        q = q[:m.start(2)] + " " + q[m.end():]
    spans = []
    for m in re.finditer(rf"([a-z ]{{0,30}}?)\b(under|below|less than|upto|up to|within|max(?:imum)?|"
                         rf"not more than|<=?|above|over|more than|at least|min(?:imum)?|>=?)\s*(?:rs\.?\s*)?"
                         rf"{_NUM}(\s*{_LPA})?", q):
        prefix = m.group(1)
        if not m.group(4) and (not re.search(r"\b(ctc|salary|package)\b", prefix)
                               or re.match(rf"\s*{_YRS}", q[m.end():])):
            continue                      # 'under 5 years' is experience, not CTC
        kind = ctc_kind(prefix)
        word, val = m.group(2), float(m.group(3))
        upper = not re.match(r"above|over|more than|at least|min|>", word)
        if kind == "current" and upper:
            out["max_current_ctc"] = val; said.append(f"current CTC up to {_fmt(val)} LPA")
        elif kind == "current":
            said.append(f"(current CTC above {_fmt(val)} LPA is not a filter; ignored)")
        elif upper:
            out["max_expected_ctc"] = val; said.append(f"expected CTC up to {_fmt(val)} LPA")
        else:
            out["min_expected_ctc"] = val; said.append(f"expected CTC from {_fmt(val)} LPA")
        spans.append((m.start(2), m.end()))
    for a, b in reversed(spans):
        q = q[:a] + " " + q[b:]
    q = re.sub(rf"{_NUM}\s*{_LPA}", " ", q)

    # ---- experience
    exp_patterns = [
        (rf"\bbetween\s+{_NUM}\s+(?:and|to)\s+{_NUM}\s*{_YRS}", "range"),
        (rf"\b{_NUM}\s*(?:-|to)\s*{_NUM}\s*{_YRS}", "range"),
        (rf"\b{_NUM}\s*\+\s*(?:{_YRS})?", "min"),
        (rf"\b{_NUM}\s*(?:plus|or more|and above)\s*(?:{_YRS})?", "min"),
        (rf"\b(?:at least|atleast|minimum|min|more than|over|above|greater than)\s+{_NUM}\s*{_YRS}", "min"),
        (rf"\b(?:under|below|less than|upto|up to|maximum|max|not more than|within)\s+{_NUM}\s*{_YRS}", "max"),
        (rf"\b{_NUM}\s*{_YRS}\s*(?:or less|and below|max(?:imum)?)", "max"),
        (rf"\b{_NUM}\s*{_YRS}", "min"),
    ]
    for pat, kind in exp_patterns:
        m = re.search(pat, q)
        if not m:
            continue
        if kind == "range":
            out["min_exp"], out["max_exp"] = float(m.group(1)), float(m.group(2))
            said.append(f"experience {_fmt(out['min_exp'])}-{_fmt(out['max_exp'])} yrs")
        elif kind == "min":
            out["min_exp"] = float(m.group(1)); said.append(f"experience {_fmt(out['min_exp'])}+ yrs")
        else:
            out["max_exp"] = float(m.group(1)); said.append(f"experience up to {_fmt(out['max_exp'])} yrs")
        q = q[:m.start()] + " " + q[m.end():]
        break

    # ---- statuses, including 'not rejected'
    include, exclude = [], []
    # 'exclude rejected and on hold' / 'not rejected or on hold': every status in that clause is left out
    for neg in re.finditer(rf"\b{_NEG}\b", q):
        clause = re.split(r"[,;.]| with | who | having | in ", q[neg.end():])[0]
        for status, pats in STATUS_WORDS.items():
            if any(re.search(p.replace("(?<!not )", ""), clause) for p in pats):
                exclude.append(status)
    for status in exclude:
        for p in STATUS_WORDS[status]:
            q = re.sub(rf"\b{_NEG}\b\s+(?:the\s+|any\s+|those\s+)?(?={p.replace('(?<!not )', '')})", " ", q)
            q = re.sub(p.replace("(?<!not )", ""), " ", q)
    for status, pats in STATUS_WORDS.items():
        if status in exclude:
            continue
        for p in pats:
            core = p.replace("(?<!not )", "")
            neg = re.search(rf"\b{_NEG}\s+(?:the\s+|any\s+|those\s+)?(?:{core})", q)
            if neg:
                exclude.append(status)
                q = q[:neg.start()] + " " + q[neg.end():]
                break
            if re.search(p, q):
                include.append(status)
                q = re.sub(p, " ", q)
                break
    include = [s for s in dict.fromkeys(include) if s not in exclude]
    if include:
        out["statuses"] = include
        said.append("status " + " or ".join(include))
    if exclude:
        out["exclude_statuses"] = list(dict.fromkeys(exclude))
        said.append("leave out " + ", ".join(out["exclude_statuses"]))

    # ---- location
    locs = []
    for m in _CITY_RE.finditer(q):
        locs.append(canonical_city(m.group(1)))
    if locs:
        out["location"] = ",".join(dict.fromkeys(locs))
        said.append("location " + " or ".join(dict.fromkeys(locs)))
        q = _CITY_RE.sub(" ", q)

    # ---- skills (kept in the meaning text too, so the AI still ranks by them)
    meaning_src = q
    skills = []
    for name, pats in ALL_SKILLS.items():
        if name == "Mobile/Device Automation":
            continue
        if any(re.search(p, q) for p in pats):
            skills.append(name)
            for p in pats:
                q = re.sub(p, " ", q)
    if skills:
        out["skills"] = skills
        said.append("skills " + ", ".join(skills))

    # ---- what is left
    stop = FILLER | EXTRA_FILLER | SMALL
    short_num = lambda w: re.fullmatch(r"[\d.]+", w) and len(re.sub(r"\D", "", w)) < 6   # keep phone numbers
    left = [w for w in re.findall(r"[a-z0-9+#.@]+", q) if w not in stop and not short_num(w)]
    out["words"] = " ".join(left) if len(" ".join(left)) >= 2 else ""
    mean = [w for w in re.findall(r"[a-z0-9+#.]+", meaning_src) if w not in stop and not re.fullmatch(r"[\d.]+", w)]
    out["meaning"] = " ".join(mean) if len(" ".join(mean)) >= 3 else ""
    return out
