"""
AI layer for the Ciliconchip.ai Resume Bank (v1.4).

Uses the open-source sentence-embedding model all-MiniLM-L6-v2 (the same model the screening app uses)
through fastembed / ONNX Runtime: no PyTorch, no paid API, and no candidate data leaves the app.

The model turns text into a 384-number "meaning fingerprint" (an embedding). Two texts about the same kind
of work get similar fingerprints even when they share no words, which is what powers:
  - AI search in plain English
  - matching a Job Description against the whole bank
  - finding similar profiles
"""

import os
import re
import zlib
from typing import Dict, List, Optional

import numpy as np

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
DIM = 384
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".model_cache")


class Embedder:
    """Wraps the embedding model; encode() returns unit-length float32 vectors."""

    def __init__(self, model, name: str):
        self._model = model
        self.name = name

    def encode(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        vecs = np.array(list(self._model.embed([t or " " for t in texts])), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms


class _HashingModel:
    """
    Test stand-in used ONLY when RB_TEST_EMBEDDER=hashing (the real model can't be downloaded in the
    build sandbox). Bag-of-words hashing with a few synonyms - enough to exercise storage and ranking.
    """
    SYN = {"llm": "language", "llms": "language", "chatbot": "conversational", "chatbots": "conversational",
           "genai": "generative", "nlp": "language", "rag": "retrieval", "ml": "learning"}

    def embed(self, texts):
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for w in re.findall(r"[a-z0-9+#]+", t.lower()):
                for tok in {w, self.SYN.get(w, w)}:
                    v[zlib.crc32(tok.encode()) % DIM] += 1.0
            yield v


def load_embedder() -> Embedder:
    """Load the model (downloads ~90 MB on first start, then cached). Raises if unavailable."""
    if os.environ.get("RB_TEST_EMBEDDER") == "hashing":
        return Embedder(_HashingModel(), "test-hashing")
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=MODEL_NAME, cache_dir=CACHE_DIR)
    return Embedder(model, MODEL_NAME)


# ---------------------------------------------------------------------------
# What gets fingerprinted
# ---------------------------------------------------------------------------
def profile_text(rec: Dict) -> str:
    """
    A compact description of the candidate: role, skills, experience and the opening of the resume
    (usually the summary). The model reads ~250 words, so the most telling parts go first.
    """
    def skills(v):
        if isinstance(v, str):
            return [s for s in v.split("|") if s]
        return list(v or [])
    parts = []
    if rec.get("current_role"):
        parts.append(f"{rec['current_role']}.")
    prim, sec = skills(rec.get("primary_skills")), skills(rec.get("secondary_skills"))
    if prim:
        parts.append("Primary skills: " + ", ".join(prim) + ".")
    if sec:
        parts.append("Other skills: " + ", ".join(sec[:15]) + ".")
    if rec.get("experience_years") is not None:
        parts.append(f"{rec['experience_years']:g} years of experience.")
    body = re.sub(r"\s+", " ", rec.get("resume_text") or "").strip()
    parts.append(body[:1200])
    return " ".join(parts)


def jd_text_for_embedding(jd: str) -> str:
    return re.sub(r"\s+", " ", jd or "").strip()[:2000]


# ---------------------------------------------------------------------------
# Storage helpers and scoring
# ---------------------------------------------------------------------------
def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if not blob or len(blob) != DIM * 4:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def ai_pct(cos: float) -> float:
    """
    Turn cosine similarity into an easy-to-read 0-100 'AI match %'. For this model, unrelated texts sit
    around 0.1-0.2 and strong matches around 0.6-0.7, so that band is stretched to 0-100 (same scale as
    the screening app's Semantic Fit).
    """
    return float(np.clip((cos - 0.15) / (0.65 - 0.15) * 100.0, 0.0, 100.0))


# ---------------------------------------------------------------------------
# Understanding the AI search box
# ---------------------------------------------------------------------------
# Status words people naturally type ("rejected profiles", "candidates on hold") describe the recruitment
# record, not the resume, so they become a status filter instead of being matched against resume meaning.
STATUS_WORDS = {
    "Rejected": [r"\breject(?:ed|ion|s)?\b", r"\bnot selected\b", r"\bdeclined by us\b"],
    "Selected": [r"(?<!not )\bselected\b", r"\boffer(?:ed)? (?:released|made|given|accepted)\b"],
    "On hold": [r"\bon[\s-]?hold\b", r"\bparked\b"],
    "Employee referral": [r"\breferr(?:al|als|ed)\b", r"\bemployee referral\b"],
    "In process": [r"\bin[\s-]?process\b", r"\bin progress\b", r"\binterview(?:s|ing)? (?:going on|ongoing)\b"],
}
FILLER = set("""
share show list give find get fetch display pull bring me us all any the a an of with who whose which that
those these is are was were be been stand stands standing profile profiles candidate candidates resume resumes
cv cvs people person persons please kindly can could you i want need looking for in our from bank database
marked status state currently current latest have has had by employee employees someone
""".split())


def understand_query(query: str):
    """
    Split the AI search text into (statuses, what's left to match by meaning).
    'share me the profile that stands rejected' -> (['Rejected'], '')
    'rejected AI engineers with LLM experience' -> (['Rejected'], 'ai engineers llm experience')
    """
    q = (query or "").lower()
    statuses = []
    for status, pats in STATUS_WORDS.items():
        if any(re.search(p, q) for p in pats):
            statuses.append(status)
            for p in pats:
                q = re.sub(p, " ", q)
    words = [w for w in re.findall(r"[a-z0-9+#.]+", q) if w not in FILLER]
    remainder = " ".join(words) if len(words) >= 1 and len(" ".join(words)) >= 3 else ""
    return statuses, remainder
