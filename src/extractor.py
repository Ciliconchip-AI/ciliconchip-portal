"""
Pulls the key details out of a resume: name, email, phone, location, current role,
primary / secondary skills, education and experience.

Everything is rule-based (no paid AI). The app shows the results in an editable table
so TA can correct anything before it is saved.
"""

import re
from typing import Dict, List, Tuple

from .matcher import (
    SKILL_ALIASES, experience_from_dates, extract_candidate_name, extract_education, normalize,
    stated_experience,
)

# Skills on top of the screening app's list, with Ciliconchip's semiconductor focus in mind.
EXTRA_SKILLS: Dict[str, List[str]] = {
    "UIKit": [r"\buikit\b"],
    "Core Data": [r"core\s*data\b"],
    "Pytest": [r"\bpytest\b"],
    "JUnit": [r"\bjunit\b"],
    "Robot Framework": [r"robot\s+framework"],
    "Jest": [r"\bjest\b"],
    "JMeter": [r"\bjmeter\b"],
    "UFT/QTP": [r"\buft\b", r"\bqtp\b"],
    "BrowserStack": [r"browser\s*stack"],
    "Charles Proxy": [r"charles\s+proxy"],
    "Firebase": [r"\bfirebase\b"],
    "Node.js": [r"node\.?js\b"],
    ".NET": [r"(?<!\w)\.net\b", r"\bdotnet\b", r"asp\.net"],
    "PHP": [r"\bphp\b"],
    "Ruby": [r"\bruby\b"],
    "HTML/CSS": [r"\bhtml5?\b", r"\bcss3?\b"],
    "MongoDB": [r"\bmongo\s*db\b"],
    "Oracle": [r"\boracle\b"],
    "Kafka": [r"\bkafka\b"],
    "Airflow": [r"\bairflow\b"],
    "Snowflake": [r"\bsnowflake\b"],
    "Databricks": [r"\bdatabricks\b"],
    "Verilog": [r"\bverilog\b(?!-a)"],
    "SystemVerilog": [r"system\s*verilog"],
    "UVM": [r"\buvm\b"],
    "VHDL": [r"\bvhdl\b"],
    "VLSI": [r"\bvlsi\b"],
    "RTL Design": [r"\brtl\b"],
    "Physical Design": [r"physical\s+design"],
    "DFT": [r"\bdft\b", r"design\s+for\s+test"],
    "Static Timing Analysis": [r"static\s+timing", r"\bprimetime\b"],
    "FPGA": [r"\bfpga\b"],
    "ASIC": [r"\basic\b"],
    "AUTOSAR": [r"\bautosar\b"],
    "RTOS": [r"\brtos\b", r"\bfreertos\b"],
    "Embedded C": [r"embedded\s+c\b"],
}
ALL_SKILLS: Dict[str, List[str]] = {**SKILL_ALIASES, **EXTRA_SKILLS}

CITY_ALIASES = {
    "bangalore": "Bengaluru", "gurgaon": "Gurugram", "cochin": "Kochi", "trivandrum": "Thiruvananthapuram",
    "mysore": "Mysuru", "mangalore": "Mangaluru", "vizag": "Visakhapatnam", "secunderabad": "Hyderabad",
    "bombay": "Mumbai", "madras": "Chennai", "calcutta": "Kolkata", "new delhi": "Delhi",
}
CITIES = [
    "Hyderabad", "Secunderabad", "Bengaluru", "Bangalore", "Chennai", "Pune", "Navi Mumbai", "Mumbai", "Thane",
    "New Delhi", "Delhi", "Noida", "Greater Noida", "Gurugram", "Gurgaon", "Faridabad", "Ghaziabad", "Kolkata",
    "Ahmedabad", "Gandhinagar", "Kochi", "Cochin", "Thiruvananthapuram", "Trivandrum", "Coimbatore",
    "Visakhapatnam", "Vizag", "Vijayawada", "Guntur", "Tirupati", "Warangal", "Mysuru", "Mysore", "Mangaluru",
    "Mangalore", "Hubli", "Belagavi", "Indore", "Bhopal", "Jaipur", "Chandigarh", "Mohali", "Nagpur", "Nashik",
    "Aurangabad", "Bhubaneswar", "Lucknow", "Kanpur", "Madurai", "Tiruchirappalli", "Salem", "Goa", "Surat",
    "Vadodara", "Rajkot", "Patna", "Ranchi", "Dehradun", "Bombay", "Madras", "Calcutta",
    "Singapore", "Dubai", "Abu Dhabi", "London", "Dublin", "Berlin", "Munich", "Amsterdam", "Toronto",
    "San Jose", "San Francisco", "Seattle", "Austin", "Dallas", "New York", "Chicago", "Boston", "Sydney",
    "Melbourne", "Kuala Lumpur", "Tokyo", "Seoul",
]
_CITY_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(c) for c in sorted(CITIES, key=len, reverse=True))
                      + r")(?![A-Za-z])", re.IGNORECASE)
_LOCATION_LABEL = re.compile(
    r"(?:current\s+location|present\s+location|preferred\s+location|location|address|based\s+(?:in|at)|"
    r"residing\s+(?:at|in)|city)\s*[:\-–]\s*([^\n|•]{2,80})", re.IGNORECASE)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w/@.])(\+?\d[\d\s\-().]{8,17}\d)(?![\w/])")
ROLE_WORDS = re.compile(
    r"\b(engineer|developer|analyst|tester|test|qa|sdet|lead|manager|consultant|architect|designer|scientist|"
    r"specialist|administrator|intern|associate|programmer|devops|director|head)\b", re.IGNORECASE)


def canonical_city(name: str) -> str:
    key = name.strip().lower()
    if key in CITY_ALIASES:
        return CITY_ALIASES[key]
    for c in CITIES:
        if c.lower() == key:
            return c
    return name.strip().title()


def extract_email(text: str) -> str:
    m = EMAIL_RE.search(text or "")
    return m.group(0).strip(".").lower() if m else ""


def extract_phone(text: str) -> str:
    cands = []
    for m in PHONE_RE.finditer(text or ""):
        raw = m.group(1)
        digits = re.sub(r"\D", "", raw)
        if not 10 <= len(digits) <= 13:
            continue
        if re.fullmatch(r"(19|20)\d{2}(19|20)\d{2}\d*", digits):  # year ranges such as 2018 2022
            continue
        before = text[max(0, m.start() - 25):m.start()].lower()
        labelled = bool(re.search(r"(phone|mobile|mob|cell|contact|tel|ph|call)\W*$", before)) \
            or raw.strip().startswith("+")
        cands.append((not labelled, m.start(), raw, digits))
    if not cands:
        return ""
    cands.sort()
    _, _, raw, d = cands[0]
    if len(d) == 10 and d[0] in "6789":
        return f"+91 {d[:5]} {d[5:]}"
    if len(d) == 12 and d.startswith("91") and d[2] in "6789":
        return f"+91 {d[2:7]} {d[7:]}"
    if len(d) == 11 and d.startswith("0") and d[1] in "6789":
        return f"+91 {d[1:6]} {d[6:]}"
    return re.sub(r"\s+", " ", raw).strip()


def extract_location(text: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    # 1) an explicit "Location: ..." / "Address: ..." line
    for m in _LOCATION_LABEL.finditer(text or ""):
        value = m.group(1).strip(" ,.;")
        city = _CITY_RE.search(value)
        if city:
            return canonical_city(city.group(1))
        if 2 <= len(value) <= 40 and not re.search(r"\d{4,}|@", value):
            return value.split(",")[0].strip().title()
    # 2) a known city near the top (contact block), then anywhere in the first part
    for window in (lines[:15], lines[:60]):
        m = _CITY_RE.search("\n".join(window))
        if m:
            return canonical_city(m.group(1))
    # 3) country only
    if re.search(r"\bindia\b", "\n".join(lines[:10]), re.IGNORECASE):
        return "India"
    return ""


def extract_current_role(text: str, name: str) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()][:8]
    for line in lines:
        if name and name.lower() in line.lower():
            continue
        if "@" in line or re.search(r"\d{6,}", line):
            continue
        first = re.split(r"\s[|•]\s|\s{2,}", line)[0].strip(" -–|")
        if ROLE_WORDS.search(first) and 3 <= len(first) <= 70:
            return first
    return ""


EDUCATION_HEADS = {
    "education", "academic", "academics", "academic profile", "academic details", "academic qualification",
    "academic qualifications", "educational qualification", "educational qualifications", "educational details",
    "qualification", "qualifications", "education details",
}
OTHER_HEADS = {
    "experience", "work experience", "professional experience", "employment", "employment history",
    "work history", "projects", "project experience", "key projects", "skills", "technical skills",
    "core skills", "key skills", "certifications", "certification", "certificates", "summary",
    "professional summary", "profile", "profile summary", "objective", "career objective", "achievements",
    "awards", "personal details", "personal information", "declaration", "languages", "tools",
    "responsibilities", "key responsibilities", "internships", "trainings", "training",
}


def _without_education(lines: List[str]) -> List[str]:
    """Drop the education section so a degree title (e.g. 'M.Tech VLSI') isn't read as a work skill."""
    kept, in_edu = [], False
    for line in lines:
        head = re.sub(r"[^a-z ]", "", line.lower()).strip()
        if head in EDUCATION_HEADS:
            in_edu = True
            continue
        if head in OTHER_HEADS:
            in_edu = False
        if not in_edu:
            kept.append(line)
    return kept


def classify_skills(text: str) -> Tuple[List[str], List[str]]:
    """
    Primary skills: named in the headline at the top of the resume, or used repeatedly through it.
    Secondary skills: everything else found (listed once, e.g. only in a skills table).
    """
    lines = _without_education([l for l in (text or "").splitlines() if l.strip()])
    headline = normalize(" ".join(lines[:5]))
    full = normalize("\n".join(lines))
    scored = []
    for name, pats in ALL_SKILLS.items():
        count = sum(len(re.findall(p, full)) for p in pats)
        if not count:
            continue
        in_headline = any(re.search(p, headline) for p in pats)
        scored.append((count + (6 if in_headline else 0), count, in_headline, name))
    scored.sort(key=lambda s: (-s[0], s[3]))

    primary = [n for score, count, in_headline, n in scored if in_headline or count >= 2][:10]
    if len(primary) < 3:
        primary += [n for *_, n in scored if n not in primary][:3 - len(primary)]
    secondary = [n for *_, n in scored if n not in primary]
    return primary, secondary


def extract_profile(text: str, filename: str) -> Dict:
    name = extract_candidate_name(text, filename)
    primary, secondary = classify_skills(text)
    from_dates, spans = experience_from_dates(text)
    claimed = stated_experience(text)
    years = from_dates if from_dates is not None else claimed
    source = "employment dates" if from_dates is not None else ("resume summary" if claimed is not None else "")
    note = ""
    if from_dates is not None and claimed is not None and abs(claimed - from_dates) >= 1.0:
        note = f"Resume claims {claimed:g} yrs; dated roles add up to {from_dates:g} yrs"
    return {
        "full_name": name,
        "email": extract_email(text),
        "phone": extract_phone(text),
        "location": extract_location(text),
        "current_role": extract_current_role(text, name),
        "primary_skills": primary,
        "secondary_skills": secondary,
        "experience_years": years,
        "experience_source": source,
        "experience_detail": "; ".join(spans[:8]),
        "experience_note": note,
        "education": extract_education(text),
    }
