"""
Candidate matching engine for ciliconchip.ai TalentMatch AI (v2).

Key changes from v1
-------------------
1. Skills are now read from the JD itself (not only from a fixed list) and
   normalised through an alias table, so "XCUI", "XCUI Test", "XCUITest" and
   "XCTest/XCUITest" are treated as the same skill. If the JD lists skills and a
   candidate has none of them, the skill score is 0 - never a "free" default.
2. Must-have gates: a candidate who misses most JD skills, or is clearly short on
   required experience, is capped and cannot be shortlisted however well the
   other dimensions score.
3. Experience is calculated from the employment date ranges in the resume
   (education dates excluded, overlapping jobs merged). The "X years" claim in
   the summary is only used when no dates can be found, and a mismatch between
   the two is flagged for the recruiter.
4. Dimensions the JD does not specify (e.g. education) are left out of the
   weighting instead of being given a neutral score that inflates everyone.
5. Education detection no longer mistakes ordinary words ("be", "me", "ms")
   for degrees.
6. Candidate name is read from the top of the resume, not the file name.
"""

import re
from datetime import date
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Skill taxonomy: canonical name -> regex variants (applied to lower-case text)
# Extend this table as new roles come in; anything not listed is still picked
# up from the JD by extract_jd_skills() and matched literally.
# ---------------------------------------------------------------------------
SKILL_ALIASES: Dict[str, List[str]] = {
    # Mobile / iOS
    "XCUITest": [r"xcui\s*-?\s*tests?", r"xcuitest(?:s|ing)?", r"\bxcui\b", r"xcode\s+ui\s+test"],
    "XCTest": [r"\bxctest\b"],
    "Swift": [r"\bswift\b(?!\s*ui)"],
    "SwiftUI": [r"\bswiftui\b"],
    "Objective-C": [r"objective\s*-?\s*c\b", r"\bobj\s*-?\s*c\b"],
    "Xcode": [r"\bxcode\b"],
    "Mobile/Device Automation": [
        r"(?:device|mobile|ios|android)\s+(?:ui\s+|app\s+|test\s+)?automation",
        r"\bappium\b", r"xcuitest", r"xcui\s*-?\s*test", r"\bespresso\b", r"\bdetox\b",
        r"automat\w*\s+(?:on|for)\s+(?:mobile|ios|android|devices?)",
    ],
    "Appium": [r"\bappium\b"],
    "Espresso": [r"\bespresso\b"],
    "Android": [r"\bandroid\b"],
    "iOS": [r"\bios\b"],
    "Kotlin": [r"\bkotlin\b"],
    "Flutter": [r"\bflutter\b"],
    "React Native": [r"react\s*native"],
    # Test automation
    "Selenium": [r"\bselenium\b"],
    "Playwright": [r"\bplaywright\b"],
    "Cypress": [r"\bcypress\b"],
    "TestNG": [r"\btestng\b"],
    "Cucumber/BDD": [r"\bcucumber\b", r"\bbdd\b"],
    "API Testing": [r"api\s+(?:testing|automation|validation)", r"\bpostman\b", r"rest\s*assured"],
    "Postman": [r"\bpostman\b"],
    "Manual Testing": [r"manual\s+testing"],
    "Performance Testing": [r"performance\s+testing", r"\bjmeter\b", r"\bloadrunner\b"],
    "JIRA": [r"\bjira\b"],
    "TestRail": [r"\btestrail\b"],
    # Languages
    "Python": [r"\bpython\b"],
    "Java": [r"\bjava\b(?!\s*script)"],
    "JavaScript": [r"\bjavascript\b", r"\bjs\b"],
    "TypeScript": [r"\btypescript\b"],
    "C#": [r"c\s*#", r"\bc\s*sharp\b"],
    "C++": [r"c\+\+", r"\bcpp\b"],
    "Go": [r"\bgolang\b"],
    "SQL": [r"\bsql\b", r"\bmysql\b", r"\bpostgres(?:ql)?\b", r"\bpl\s*/\s*sql\b"],
    # Data / AI
    "Machine Learning": [r"machine\s+learning", r"\bml\b"],
    "Deep Learning": [r"deep\s+learning"],
    "NLP": [r"\bnlp\b", r"natural\s+language\s+processing"],
    "Computer Vision": [r"computer\s+vision"],
    "LLM": [r"\bllms?\b", r"large\s+language\s+models?"],
    "Generative AI": [r"generative\s+ai", r"\bgen\s*ai\b"],
    "RAG": [r"\brag\b", r"retrieval[\s-]+augmented"],
    "LangChain": [r"\blangchain\b"],
    "PyTorch": [r"\bpytorch\b"],
    "TensorFlow": [r"\btensorflow\b"],
    "Pandas": [r"\bpandas\b"],
    "Spark": [r"\bspark\b", r"\bpyspark\b"],
    "Power BI": [r"power\s*bi"],
    "Tableau": [r"\btableau\b"],
    # Cloud / DevOps
    "AWS": [r"\baws\b", r"amazon\s+web\s+services"],
    "Azure": [r"\bazure\b"],
    "GCP": [r"\bgcp\b", r"google\s+cloud"],
    "Docker": [r"\bdocker\b"],
    "Kubernetes": [r"\bkubernetes\b", r"\bk8s\b"],
    "Terraform": [r"\bterraform\b"],
    "Jenkins": [r"\bjenkins\b"],
    "CI/CD": [r"ci\s*/\s*cd", r"continuous\s+integration", r"\bfastlane\b", r"azure\s+devops\s+pipeline"],
    "Git": [r"\bgit\b", r"\bgithub\b", r"\bgitlab\b"],
    "Linux": [r"\blinux\b", r"\bunix\b"],
    "Microservices": [r"micro\s*-?\s*services?"],
    "REST API": [r"rest(?:ful)?\s*apis?", r"\brestful\b"],
    # Frameworks
    "FastAPI": [r"\bfastapi\b"],
    "Flask": [r"\bflask\b"],
    "Django": [r"\bdjango\b"],
    "Spring Boot": [r"spring\s*boot"],
    "React": [r"\breact(?:\.?js)?\b(?!\s*native)"],
    "Angular": [r"\bangular\b"],
    # Enterprise / domain
    "SAP": [r"\bsap\b"],
    "Salesforce": [r"\bsalesforce\b"],
    "ServiceNow": [r"\bservicenow\b"],
    "Embedded Systems": [r"embedded\s+systems?", r"\bembedded\b"],
    "Semiconductor": [r"semiconductor"],
    "Automotive": [r"\bautomotive\b"],
    "Agile/Scrum": [r"\bagile\b", r"\bscrum\b"],
}

SOFT_SKILLS: Dict[str, List[str]] = {
    "Communication": [r"communicat\w+"],
    "Stakeholder Management": [r"stakeholder"],
    "Leadership": [r"\blead(?:ership|ing)?\b"],
    "Problem Solving": [r"problem[\s-]+solving"],
    "Teamwork": [r"team\s*(?:work|player)"],
}

# Phrases in the JD that introduce skills, e.g. "Proficiency in Swift, Xcode".
SKILL_TRIGGERS = (
    r"proficien\w*\s+(?:in|with)|experience\s+(?:in|with|on)|expertise\s+(?:in|with)|"
    r"knowledge\s+(?:of|in)|hands[\s-]*on\s+(?:with|in|on)?|skills?\s*(?:in|:)|"
    r"familiar\w*\s+with|working\s+with|exposure\s+to|using|strong\s+in|must\s+have\s*:?"
)
GENERIC_WORDS = {
    "framework", "frameworks", "tool", "tools", "tests", "test", "testing", "the", "a", "an",
    "good", "strong", "solid", "excellent", "concepts", "technologies", "technology", "etc",
    "platform", "platforms", "language", "languages", "similar", "related", "various",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_RE = rf"(?:{MONTH_RE}\.?\s*[,']?\s*(\d{{4}}|\d{{2}}\b)|(\d{{1,2}})\s*[/\-.]\s*(\d{{4}})|((?:19|20)\d{{2}}))"
END_WORDS = r"(present|current(?:ly)?|now|till\s+date|to\s+date|date|ongoing|today)"
RANGE_RE = re.compile(
    rf"{DATE_RE}\s*(?:-|–|—|to|till|until)\s*(?:{DATE_RE}|{END_WORDS})", re.IGNORECASE
)

SECTION_HEADERS = {
    "education": ["education", "academic", "academics", "qualification", "qualifications",
                  "educational qualification", "educational details", "academic profile",
                  "certifications", "certificates", "certification", "training", "courses"],
    "experience": ["experience", "professional experience", "work experience", "employment",
                   "employment history", "work history", "career history", "project experience",
                   "projects", "key project highlights", "professional summary", "summary",
                   "profile", "skills", "technical skills", "core skills"],
}
DEGREE_HINT = re.compile(
    r"\b(b\.?\s?tech|m\.?\s?tech|b\.\s?e\b|m\.\s?e\b|bachelor|master|degree|university|college|"
    r"institute|school|cgpa|gpa|percentage|ssc|hsc|intermediate|10th|12th|diploma|mba|mca|bca|"
    r"b\.?\s?sc|m\.?\s?sc|ph\.?d)\b",
    re.IGNORECASE,
)

DEGREE_PATTERNS = [
    ("PhD", r"\b(ph\.?\s?d|doctorate|doctoral)\b"),
    ("Master's", r"(\bm\.?\s?tech\b|\bm\.\s?e\.?\b|\bm\.\s?s\.?\b|\bmaster(?:'s|s)?\s+(?:of|in|degree)\b|\bmba\b|"
                 r"\bm\.?\s?sc\b|\bmca\b|post[\s-]?graduat)"),
    ("Bachelor's", r"(\bb\.?\s?tech\b|\bb\.\s?e\.?\b|\bb\.\s?s\.?\b|\bbachelors?\b|\bbachelor's\b|"
                   r"\bb\.?\s?sc\b|\bbca\b|\bb\.?\s?com\b|graduat(?:e|ion)\b)"),
]
EDU_RANK = {"Not detected": 0, "Bachelor's": 1, "Master's": 2, "PhD": 3}

NAME_STOPWORDS = {
    "resume", "curriculum", "vitae", "cv", "profile", "summary", "engineer", "developer",
    "analyst", "tester", "testing", "quality", "assurance", "automation", "senior", "junior",
    "lead", "manager", "consultant", "architect", "india", "hyderabad", "bangalore",
    "bengaluru", "chennai", "pune", "email", "phone", "mobile", "contact", "address",
    "professional", "experience", "education", "skills", "objective", "ios", "android",
    "qa", "sdet", "software", "technical", "data", "scientist", "linkedin", "github",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("all-MiniLM-L6-v2")


ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", ZERO_WIDTH.sub("", text or "").lower()).strip()


def _has_any(patterns: List[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _canonical(phrase: str) -> Optional[str]:
    """Map a free-text skill phrase from the JD onto a canonical skill name."""
    p = normalize(phrase)
    for name, pats in SKILL_ALIASES.items():
        for pat in pats:
            if re.fullmatch(r"(?:\w+\s+){0,2}?" + pat + r"(?:\s+\w+){0,2}?", p):
                return name
    return None


def _literal_pattern(phrase: str) -> str:
    words = [re.escape(w) for w in normalize(phrase).split()]
    return r"(?<!\w)" + r"[\s\-]*".join(words) + r"(?!\w)"


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
def extract_jd_skills(jd_text: str) -> Tuple[List[Dict], List[str]]:
    """
    Returns (technical_skills, soft_skills).
    Each technical skill is {"name": str, "patterns": [regex,...]}.
    """
    t = normalize(jd_text)
    found: Dict[str, List[str]] = {}

    # 1) Every known skill mentioned anywhere in the JD
    for name, pats in SKILL_ALIASES.items():
        if _has_any(pats, t):
            found[name] = pats

    # 2) Unknown skills introduced by trigger phrases ("Proficiency in X, Y and Z")
    for line in re.split(r"[\n•●▪◦•;]|(?<=\.)\s", jd_text or ""):
        line_l = line.lower()
        for m in re.finditer(SKILL_TRIGGERS, line_l):
            tail = line_l[m.end():]
            tail = re.split(r"[.(:]", tail)[0]
            for chunk in re.split(r",|/|\band\b|\bor\b|\busing\b|\bwith\b|&", tail):
                words = [w for w in re.findall(r"[a-z0-9+#.\-]+", chunk) if w not in GENERIC_WORDS]
                if not words or len(words) > 4:
                    continue
                phrase = " ".join(words).strip(" .-")
                if len(phrase) < 2 or re.search(r"\d+\s*\+?\s*(years?|yrs?)", phrase):
                    continue
                if any(_has_any(p, phrase) for p in SOFT_SKILLS.values()):
                    continue
                if phrase in {"experience", "years", "year", "office", "client", "location"}:
                    continue
                canon = _canonical(phrase)
                if canon:
                    found.setdefault(canon, SKILL_ALIASES[canon])
                elif not any(_has_any(p, phrase) for p in found.values()):
                    found.setdefault(phrase.title(), [_literal_pattern(phrase)])

    # "Mobile/Device Automation" is implied by XCUITest/Appium; keep it only if the
    # JD actually talks about device/mobile automation as a capability.
    if "Mobile/Device Automation" in found and not re.search(
        r"(device|mobile|ios|android)\s+(?:ui\s+|app\s+|test\s+)?automation", t
    ):
        found.pop("Mobile/Device Automation")

    skills = [{"name": n, "patterns": p} for n, p in found.items()]
    soft = [name for name, pats in SOFT_SKILLS.items() if _has_any(pats, t)]
    return skills, soft


def score_skills(jd_skills: List[Dict], resume_text: str):
    t = normalize(resume_text)
    matched = [s["name"] for s in jd_skills if _has_any(s["patterns"], t)]
    missing = [s["name"] for s in jd_skills if s["name"] not in matched]
    if not jd_skills:
        return None, matched, missing
    return len(matched) / len(jd_skills) * 100, matched, missing


def extract_resume_skills(resume_text: str) -> List[str]:
    t = normalize(resume_text)
    return [n for n, p in SKILL_ALIASES.items() if _has_any(p, t)]


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------
def _section_of(line: str, current: str) -> str:
    clean = re.sub(r"[^a-z ]", "", line.lower()).strip()
    if not clean or len(clean) > 40:
        return current
    for section, heads in SECTION_HEADERS.items():
        if clean in heads:
            return section
    return current


def _parse_date(month_word, year2, mm, yyyy_num, year_only, is_end=False) -> Optional[Tuple[int, int]]:
    if month_word and year2:
        y = int(year2)
        if y < 100:
            y += 2000 if y < 50 else 1900
        return y, MONTHS[month_word[:3].lower()]
    if mm and yyyy_num:
        m = int(mm)
        if 1 <= m <= 12:
            return int(yyyy_num), m
        return None
    if year_only:
        return int(year_only), (12 if is_end else 1)
    return None


def experience_from_dates(resume_text: str, today: Optional[date] = None) -> Tuple[Optional[float], List[str]]:
    """Total years of employment from date ranges, excluding education and merging overlaps."""
    today = today or date.today()
    now_idx = today.year * 12 + today.month
    lines = (resume_text or "").splitlines()
    section = "other"
    intervals, spans = [], []

    for i, line in enumerate(lines):
        section = _section_of(line, section)
        if section == "education":
            continue
        context = " ".join(lines[max(0, i - 2): i + 1])
        for m in RANGE_RE.finditer(line):
            g = m.groups()
            start = _parse_date(g[0], g[1], g[2], g[3], g[4])
            end_word = g[10]
            end = (today.year, today.month) if end_word else _parse_date(g[5], g[6], g[7], g[8], g[9], True)
            if not start or not end:
                continue
            # Year-only ranges right next to a degree are study periods, not jobs.
            if DEGREE_HINT.search(context) and not re.search(
                r"(engineer|developer|analyst|tester|consultant|intern|associate|lead|manager|qa)",
                context, re.IGNORECASE,
            ):
                continue
            s_idx = start[0] * 12 + start[1]
            e_idx = min(end[0] * 12 + end[1], now_idx)
            if s_idx > now_idx or e_idx < s_idx or (e_idx - s_idx) > 45 * 12 or start[0] < 1970:
                continue
            # Month-to-month ranges are inclusive (Feb-Aug = 7 months)
            intervals.append((s_idx, e_idx + 1 if (g[0] or g[2]) else e_idx))
            spans.append(m.group(0).strip())

    if not intervals:
        return None, []
    intervals.sort()
    merged = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    months = sum(e - s for s, e in merged)
    return round(months / 12, 1), spans


def stated_experience(text: str) -> Optional[float]:
    """Years claimed in the text, e.g. '7.6 years of total IT experience', '3 months experience'."""
    t = normalize(text)
    vals = []
    for m in re.finditer(
        r"(\d+(?:\.\d+)?)\s*\+?\s*(years?|yrs?|months?)\s*(?:of\s+)?(?:[a-z/&\-]+\s+){0,4}?(?:experience|exp\b)",
        t,
    ):
        v = float(m.group(1))
        vals.append(v / 12 if m.group(2).startswith("month") else v)
    for m in re.finditer(r"experience\s*(?:of|:)?\s*(\d+(?:\.\d+)?)\s*\+?\s*(years?|yrs?|months?)", t):
        v = float(m.group(1))
        vals.append(v / 12 if m.group(2).startswith("month") else v)
    vals = [v for v in vals if 0 < v < 45]
    return round(max(vals), 1) if vals else None


def required_experience(jd_text: str) -> Optional[float]:
    t = normalize(jd_text)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|-\s*\d+|to\s*\d+)?\s*(?:years?|yrs?)", t)
    return float(m.group(1)) if m else None


def assess_experience(jd_text: str, resume_text: str) -> Dict:
    req = required_experience(jd_text)
    from_dates, spans = experience_from_dates(resume_text)
    claimed = stated_experience(resume_text)

    used = from_dates if from_dates is not None else claimed
    source = "employment dates" if from_dates is not None else ("resume summary" if claimed is not None else "not found")
    flag = None
    if from_dates is not None and claimed is not None and abs(claimed - from_dates) >= 1.0:
        flag = (f"Resume claims {claimed:g} yrs but dated roles add up to {from_dates:g} yrs "
                f"- verify in screening.")

    if req is None:
        score = None
    elif used is None:
        score = 30.0
        flag = flag or "Experience could not be determined from the resume - verify manually."
    elif used >= req:
        score = 100.0
    else:
        score = max(0.0, used / req * 100)

    return {"required": req, "years": used, "from_dates": from_dates, "claimed": claimed,
            "source": source, "score": score, "flag": flag, "spans": spans}


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------
def extract_education(text: str) -> str:
    t = normalize(text)
    for label, pattern in DEGREE_PATTERNS:
        if re.search(pattern, t):
            return label
    return "Not detected"


def education_score(jd_text: str, resume_text: str):
    jd_edu = extract_education(jd_text)
    cv_edu = extract_education(resume_text)
    if jd_edu == "Not detected":
        return None, cv_edu
    if EDU_RANK[cv_edu] >= EDU_RANK[jd_edu]:
        return 100.0, cv_edu
    if EDU_RANK[cv_edu] == EDU_RANK[jd_edu] - 1:
        return 60.0, cv_edu
    return 20.0, cv_edu


# ---------------------------------------------------------------------------
# Responsibilities & semantic similarity
# ---------------------------------------------------------------------------
RESP_STOP = {
    "about", "these", "those", "their", "there", "which", "where", "while", "should", "would",
    "could", "using", "years", "experience", "responsible", "responsibilities", "candidate",
    "position", "company", "working", "location", "office", "client", "skills", "skill",
    "proficiency", "knowledge", "strong", "excellent", "required", "preferred", "ability",
    "hyderabad", "bangalore", "bengaluru", "chennai", "mumbai", "remote", "hybrid", "days",
    "communication", "including", "within", "across", "other", "based", "plus", "least",
    "framework", "good", "must", "having", "minimum", "maximum", "years", "level",
}


def responsibility_score(jd_text: str, resume_text: str):
    jd_words = set(re.findall(r"\b[a-z][a-z\-]{4,}\b", normalize(jd_text))) - RESP_STOP
    cv_words = set(re.findall(r"\b[a-z][a-z\-]{4,}\b", normalize(resume_text)))
    if len(jd_words) < 5:
        return None  # JD too short to judge responsibilities separately
    return float(np.clip(len(jd_words & cv_words) / len(jd_words) * 100, 0, 100))


def semantic_score(jd_text: str, resume_text: str):
    try:
        from sklearn.metrics.pairwise import cosine_similarity
        model = get_model()
        emb = model.encode([jd_text[:8000], resume_text[:8000]], normalize_embeddings=True)
        cos = float(cosine_similarity([emb[0]], [emb[1]])[0][0])
    except Exception:
        return None
    # MiniLM cosine for unrelated docs is ~0.1-0.2 and for close matches ~0.6-0.7.
    # Stretch that band to 0-100 instead of the old (cos+1)/2 which gave everyone 55-80.
    return float(np.clip((cos - 0.15) / (0.65 - 0.15) * 100, 0, 100))


# ---------------------------------------------------------------------------
# Candidate name
# ---------------------------------------------------------------------------
def _clean_filename(filename: str) -> str:
    stem = re.sub(r"\.[a-z0-9]+$", "", filename, flags=re.IGNORECASE)
    stem = re.sub(r"\[.*?\]|\(.*?\)", " ", stem)
    stem = re.sub(r"(?i)\b(naukri|resume|cv|profile|updated|final|latest|quess|ciliconchip)\b", " ", stem)
    stem = re.sub(r"\d+(?:\.\d+)?\s*(?:years?|yrs?|y|m)\b", " ", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[_\-.\d]+", " ", stem)
    words = [w for w in stem.split() if w.isalpha() and w.lower() not in NAME_STOPWORDS]
    return " ".join(w.capitalize() for w in words[:4]) or filename


def _looks_like_name(line: str) -> bool:
    if re.search(r"[@\d|:/\\]|www|http", line):
        return False
    words = line.replace(".", ". ").split()
    if not 1 <= len(words) <= 5:
        return False
    if not all(re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", w) for w in words):
        return False
    if any(w.lower().strip(".") in NAME_STOPWORDS for w in words):
        return False
    letters = sum(len(w.strip(".")) for w in words)
    return letters >= 4


def _format_name(line: str) -> str:
    parts = [p for p in re.split(r"\s+", line.strip()) if p]
    out = []
    for p in parts:
        if p.isupper() or p.islower():
            p = "-".join(x.capitalize() for x in p.split("-"))
        out.append(p)
    return " ".join(out)


def extract_candidate_name(resume_text: str, filename: str) -> str:
    lines = [l.strip() for l in (resume_text or "").splitlines() if l.strip()]
    for line in lines[:12]:
        m = re.match(r"(?i)^\s*(?:name|candidate name|full name)\s*[:\-]\s*(.+)$", line)
        if m and _looks_like_name(m.group(1).strip()):
            return _format_name(m.group(1))
    for line in lines[:8]:
        first = re.split(r"\s{2,}|\s[|•–-]\s", line)[0].strip()
        if _looks_like_name(first) and len(first.split()) >= 2:
            return _format_name(first)
    for line in lines[:3]:
        if _looks_like_name(line):
            return _format_name(line)
    return _clean_filename(filename)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
WEIGHTS = {"skills": 0.45, "experience": 0.25, "semantic": 0.10, "responsibilities": 0.10,
           "education": 0.05, "domain": 0.05}

# Domain fit (kept from the earlier version, but only scored when the JD names a domain)
DOMAINS = {
    "Semiconductor": ["semiconductor", "chip", "asic", "eda", "vlsi", "silicon"],
    "Automotive": ["automotive", "adas", "autosar", "ecu"],
    "CRM": ["crm", "customer relationship", "salesforce", "dynamics 365"],
    "Finance": ["banking", "finance", "fintech", "payments"],
    "Healthcare": ["healthcare", "medical", "clinical"],
    "Retail": ["retail", "ecommerce", "e-commerce"],
    "Supply Chain": ["supply chain", "inventory", "procurement", "logistics"],
    "Mobile / iOS": ["ios", "iphone", "ipad", "xcode", "xcui", "swift", "mobile app"],
    "Android": ["android", "kotlin", "espresso"],
}


def domain_score(jd_text: str, resume_text: str):
    """Returns (score or None, label). None when the JD doesn't point to a specific domain."""
    jl, rl = normalize(jd_text), normalize(resume_text)
    hits = []
    for name, words in DOMAINS.items():
        in_jd = [w for w in words if re.search(r"(?<!\w)" + re.escape(w), jl)]
        if in_jd:
            found = sum(bool(re.search(r"(?<!\w)" + re.escape(w), rl)) for w in in_jd)
            hits.append((found / len(in_jd) * 100, name))
    if not hits:
        return None, "Not specified in JD"
    return max(hits)
MIN_SKILL_COVERAGE = 60.0      # % of JD skills required to be shortlist-eligible
MIN_EXPERIENCE_RATIO = 0.85    # candidate must have >= 85% of required years


def analyze_candidate(jd_text: str, resume_text: str, filename: str, jd_skills=None) -> Dict:
    if jd_skills is None:
        jd_skills, _ = extract_jd_skills(jd_text)

    skills, matched, missing = score_skills(jd_skills, resume_text)
    exp = assess_experience(jd_text, resume_text)
    edu, edu_label = education_score(jd_text, resume_text)
    resp = responsibility_score(jd_text, resume_text)
    sem = semantic_score(jd_text, resume_text)
    dom, dom_label = domain_score(jd_text, resume_text)

    dims = {"skills": skills, "experience": exp["score"], "semantic": sem,
            "responsibilities": resp, "education": edu, "domain": dom}
    used = {k: v for k, v in dims.items() if v is not None}
    total_w = sum(WEIGHTS[k] for k in used) or 1.0
    overall = sum(v * WEIGHTS[k] for k, v in used.items()) / total_w

    # Must-have gates ----------------------------------------------------------
    gates = []
    if skills is not None and skills < MIN_SKILL_COVERAGE:
        gates.append(f"Missing core JD skills: {', '.join(missing[:6])}")
        overall = min(overall, 25.0 if skills == 0 else 55.0)
    if exp["required"] is not None and exp["years"] is not None and exp["years"] < exp["required"] * MIN_EXPERIENCE_RATIO:
        gates.append(f"Experience {exp['years']:g} yrs vs {exp['required']:g}+ yrs required")
        overall = min(overall, 60.0 if exp["years"] >= exp["required"] * 0.5 else 40.0)

    overall = float(np.clip(overall, 0, 100))
    name = extract_candidate_name(resume_text, filename)
    other_skills = [s for s in extract_resume_skills(resume_text) if s not in matched][:12]

    strengths, gaps_list = [], []
    if skills is not None and skills >= 80:
        strengths.append(f"Covers {len(matched)} of {len(jd_skills)} required skills")
    if exp["score"] is not None and exp["score"] >= 100:
        strengths.append(f"{exp['years']:g} yrs experience meets the {exp['required']:g}+ yrs asked")
    if dom is not None and dom >= 70:
        strengths.append(f"Relevant {dom_label} background detected")
    if edu is not None and edu >= 80:
        strengths.append("Education appears aligned")
    gaps_list += gates
    if missing and not any(g.startswith("Missing core") for g in gates):
        gaps_list.append("Missing/weak required skills: " + ", ".join(missing[:8]))
    if exp["flag"]:
        gaps_list.append(exp["flag"])
    if dom is not None and dom < 70:
        gaps_list.append(f"{dom_label} domain relevance is limited or not clearly demonstrated")

    return {
        "candidate": name,
        "file_name": filename,
        "overall": overall,
        "skills": skills,
        "experience": exp["score"],
        "responsibilities": resp,
        "education": edu,
        "semantic": sem,
        "education_label": edu_label,
        "matched_skills": matched,
        "missing_skills": missing,
        "other_skills": other_skills,
        "experience_years": exp["years"],
        "experience_required": exp["required"],
        "experience_source": exp["source"],
        "experience_claimed": exp["claimed"],
        "experience_flag": exp["flag"],
        "gates": gates,
        "eligible": not gates,
        "domain": dom,
        "domain_label": dom_label,
        "required_years": exp["required"],
        "strengths": strengths,
        "gaps": gaps_list,
        "summary": build_summary(overall, matched, missing, exp, gates),
        "resume_text": resume_text,
    }


def unreadable_candidate(filename: str, resume_text: str) -> Dict:
    return {
        "candidate": _clean_filename(filename), "file_name": filename, "overall": 0.0,
        "skills": 0.0, "experience": None, "responsibilities": None, "education": None,
        "semantic": None, "education_label": "Not detected", "matched_skills": [],
        "missing_skills": [], "other_skills": [], "experience_years": None,
        "experience_required": None, "experience_source": "not found", "experience_claimed": None,
        "experience_flag": None, "gates": ["Resume could not be read (scanned image or protected file?)"],
        "eligible": False, "summary": "Resume text could not be extracted. Review the file manually.",
        "domain": None, "domain_label": "Not specified in JD", "required_years": None, "strengths": [],
        "gaps": ["Resume could not be read (scanned image or protected file?)"],
        "resume_text": resume_text,
    }


def recommendation(overall: float, eligible: bool, threshold: float) -> str:
    if eligible and overall >= max(85, threshold):
        return "Strong Match"
    if eligible and overall >= threshold:
        return "Good Match"
    if overall >= 60:
        return "Consider"
    return "Low Match"


def build_summary(overall, matched, missing, exp, gates):
    parts = []
    parts.append(f"Matches {len(matched)} of {len(matched) + len(missing)} JD skills"
                 + (f" ({', '.join(matched[:6])})." if matched else "."))
    if missing:
        parts.append(f"Gaps: {', '.join(missing[:6])}.")
    if exp["years"] is not None:
        req = f" vs {exp['required']:g}+ required" if exp["required"] is not None else ""
        parts.append(f"Experience {exp['years']:g} yrs (from {exp['source']}){req}.")
    if exp["flag"]:
        parts.append(exp["flag"])
    if gates:
        parts.append("Not eligible for auto-shortlist: " + "; ".join(gates) + ".")
    parts.append("Use the screening questions to validate depth rather than relying on the score alone.")
    return " ".join(parts)
