"""CV–JD semantic matcher using vector embeddings (RAG).

Loads the user's CV from `plan.txt`, splits it into semantic chunks (one
per heading/project/skill block), and embeds each chunk with a small
sentence-transformer (`all-MiniLM-L6-v2`). At runtime, `match(email_text)`
embeds the email and retrieves the top-k most similar CV chunks via
cosine similarity. The retrieved chunks become the *grounding* used to
explain why the email is (or isn't) a match — the R + A in RAG.

It also scans the email for known skill keywords from a small lexicon
and reports which ones are *missing* from the CV (set difference). Both
matched-evidence and missing-skills are surfaced together — that's the
"span-level" view referenced in the design.

This module is intentionally self-contained — the agent imports
`get_matcher()` and `match(email_text)` only.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

CV_PATH = Path(__file__).parent / "plan.txt"
EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Skills the user has — used both to chunk the CV (lookup table) and to
# decide whether a JD-mentioned skill is "missing" from the user's
# background. Lowercase, with a few common aliases.
KNOWN_SKILLS = {
    "python": ["python"],
    "c": ["c programming", "c language", " c "],  # avoid matching every 'c'
    "vhdl": ["vhdl"],
    "sql": ["sql"],
    "javascript": ["javascript", "js"],
    "deep learning": ["deep learning", "deep-learning"],
    "machine learning": ["machine learning", "ml"],
    "nlp": ["nlp", "natural language processing"],
    "tensorflow": ["tensorflow", "tf"],
    "pytorch": ["pytorch", "torch"],
    "keras": ["keras"],
    "flask": ["flask"],
    "react": ["react"],
    "streamlit": ["streamlit"],
    "git": ["git", "github", "version control"],
    "linux": ["linux", "unix"],
    "docker": ["docker", "containers"],
    "embedded systems": ["embedded systems", "microcontroller", "microcontrollers"],
    "neuroscience": ["neuroscience", "brain-computer", "bci", "eeg"],
    "bioinformatics": ["bioinformatics", "drug-target", "dti"],
}

# JD-mentioned skills we know about — same vocabulary, used to extract
# requirements from incoming emails.
JD_SKILL_VOCAB = list(KNOWN_SKILLS.keys()) + [
    "ros", "kubernetes", "rust", "go", "java", "c++", "cuda",
    "aws", "gcp", "azure", "spark", "hadoop", "kafka",
    "fpga", "verilog", "assembly", "rtos",
    "computer vision", "reinforcement learning", "graph neural networks",
    "transformers", "huggingface", "langchain",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _chunk_cv(text: str) -> list[str]:
    """Split the CV into semantic chunks. Each top-level section
    (Skills, Interests, Projects, Past Projects, etc.) becomes one chunk;
    bulleted lists inside a section are kept together so the embedding
    captures the section's topic, not just one bullet."""
    # Drop the boilerplate header
    text = re.sub(r"#\s*User Profile.*?\n", "", text, count=1)
    # Split on lines starting with a heading-like word + ':' (Skills:, Interests:, etc.)
    parts = re.split(r"\n(?=[A-Z][\w \-/]+:)", text.strip())
    chunks: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # If a chunk has bullets, keep the heading + all its bullets together
        chunks.append(p)
    # Also chunk the projects line-by-line so each project is its own retrievable unit
    expanded: list[str] = []
    for c in chunks:
        if c.lower().startswith(("past projects:", "projects:")):
            heading, *bullets = c.splitlines()
            for b in bullets:
                b = b.strip(" -")
                if b:
                    expanded.append(f"{heading.strip()} {b}")
        else:
            expanded.append(c)
    return expanded


def _extract_jd_skills(email_text: str) -> list[str]:
    """Return the list of vocabulary skills that appear in the email."""
    txt = _normalize(email_text)
    found: list[str] = []
    for skill in JD_SKILL_VOCAB:
        # Word-boundary match for short skills, substring for multi-word ones
        if " " in skill:
            if skill in txt:
                found.append(skill)
        else:
            if re.search(rf"\b{re.escape(skill)}\b", txt):
                found.append(skill)
    return found


def _cv_has_skill(skill: str, cv_text_lower: str) -> bool:
    """A skill is in the CV if any of its aliases appears literally."""
    aliases = KNOWN_SKILLS.get(skill, [skill])
    return any(a.strip() in cv_text_lower for a in aliases)


# ── Chunk → topic tagging ───────────────────────────────────────────────────
#
# Each CV chunk is tagged with zero or more topic labels at load time. The
# classifier later takes the union of topics across the chunks RAG retrieves
# for an email — so labels are grounded in *what's in the CV*, not in
# keywords matched on the email body. This is the "use the chunks when
# labeling" requirement.

_CHUNK_TOPIC_RULES: dict[str, list[str]] = {
    "AI/ML": [
        "deep learning", "machine learning", "ml", "ai/ml",
        "tensorflow", "pytorch", "keras", "nlp", "transformer", "cnn",
        "neural network", "artificial intelligence",
    ],
    "Research": [
        "research", "neuroscience", "bioinformatics", "bci", "eeg",
        "brain-computer", "drug-target", "dti", "ongoing",
    ],
    "Software Engineering": [
        "flask", "react", "streamlit", "javascript", "full-stack",
        "fullstack", "web development", "software engineering",
        "software engineer", "python", "node",
    ],
    "Embedded Systems": [
        "embedded", "vhdl", "verilog", "microcontroller", "fpga",
        "hcs12", "assembly", "digital design", "elevator", "rtos",
    ],
    "DevOps": [
        "docker", "git", "linux", "ci/cd", "devops", "containers",
    ],
}


_GENERIC_SECTION_PREFIXES = (
    "skills:", "interests:", "preferred internship", "languages:",
    "degree:", "university:", "year:",
)


def _is_generic(chunk_text: str) -> bool:
    head = chunk_text.split("\n", 1)[0].lower()
    if any(head.startswith(p) for p in _GENERIC_SECTION_PREFIXES):
        return True
    return "this project is a requirment" in chunk_text.lower()


def _raw_chunk_topics(chunk_text: str) -> list[str]:
    """Apply the rule table — every topic whose keywords appear gets in."""
    text = chunk_text.lower()
    out: list[str] = []
    for topic, kws in _CHUNK_TOPIC_RULES.items():
        if any(kw in text for kw in kws):
            out.append(topic)
    return out


def _enrich_for_embedding(chunk: str, topics: list[str]) -> str:
    """Append the topic NAMES (not full keyword lists) to a chunk's
    *embedding* text. The user-visible chunk stays original; this only
    changes what the encoder sees. We use just the topic names so a
    brief project bullet like 'HCS12 Assembly Elevator Control System'
    matches 'embedded systems' queries without dragging in every other
    SE/AI/ML keyword that would crowd out other projects."""
    if not topics:
        return chunk
    return chunk + "\n(Topic: " + ", ".join(topics) + ")"


def _resolve_chunk_topics(chunks: list[str]) -> list[list[str]]:
    """Only concrete project chunks carry topics for label voting.
    Generic sections (Skills, Interests, ...) are tagged empty — they
    still contribute to the *similarity score* (an email is "on topic"
    for the candidate's CV at all), but their topics would be too broad
    and would crowd out the projects' specific signals. Topics not
    covered by any project (e.g. DevOps in this CV) are handled by a
    small body-keyword detector in classifier.py."""
    out: list[list[str]] = []
    for c in chunks:
        out.append([] if _is_generic(c) else _raw_chunk_topics(c))
    return out


def covered_topics(chunks: list[str]) -> set[str]:
    """Set of topics covered by at least one project chunk."""
    return {t for c in chunks for t in (_raw_chunk_topics(c) if not _is_generic(c) else [])}


# ── Matcher ──────────────────────────────────────────────────────────────────

@dataclass
class CVMatch:
    score: float                           # 0..1 — best chunk similarity
    matched: list[dict]                    # [{chunk, similarity, topics}]
    missing_skills: list[str]              # skills in JD but not in CV


class CVMatcher:
    """Eager-loaded singleton holding CV chunks + their embeddings."""

    def __init__(self, cv_path: Path = CV_PATH, model_name: str = EMBED_MODEL) -> None:
        cv_text = cv_path.read_text(encoding="utf-8")
        self.cv_text_lower = cv_text.lower()
        self.chunks = _chunk_cv(cv_text)
        self.chunk_topics = _resolve_chunk_topics(self.chunks)
        # 'project' = concrete experience entry; 'generic' = Skills,
        # Interests, etc. The classifier uses kind to prefer projects
        # when both are retrieved (so PyTorch-heavy Skills doesn't drown
        # out the actual AI/ML project).
        self.chunk_kinds = [
            "generic" if _is_generic(c) else "project"
            for c in self.chunks
        ]
        self.model = SentenceTransformer(model_name)
        # Encode each chunk; projects get domain-keyword cues appended
        # to compensate for brief titles ('HCS12 Assembly...' → boosted
        # by embedded-systems cues so it can compete with keyword-rich
        # generic sections like Skills). Generic chunks are NOT
        # enriched — they already cover plenty of keywords on their own
        # and enriching them would crowd out the projects.
        for_embed = [
            _enrich_for_embedding(c, t) if k == "project" else c
            for c, t, k in zip(self.chunks, self.chunk_topics, self.chunk_kinds)
        ]
        self.chunk_embs = self.model.encode(
            for_embed, normalize_embeddings=True, convert_to_numpy=True
        )

    def match(self, email_text: str, top_k: int = 5, threshold: float = 0.25) -> CVMatch:
        if not email_text or not email_text.strip():
            return CVMatch(score=0.0, matched=[], missing_skills=[])

        q_emb = self.model.encode(
            [email_text], normalize_embeddings=True, convert_to_numpy=True
        )[0]
        sims = self.chunk_embs @ q_emb              # cosine, since both are normalized
        order = np.argsort(-sims)[:top_k]
        matched = [
            {
                "chunk": self.chunks[i][:140],
                "similarity": round(float(sims[i]), 3),
                "topics": self.chunk_topics[i],
                "kind": self.chunk_kinds[i],
            }
            for i in order
            if sims[i] >= threshold
        ]
        score = round(float(sims.max()), 3) if len(sims) else 0.0

        jd_skills = _extract_jd_skills(email_text)
        missing = [s for s in jd_skills if not _cv_has_skill(s, self.cv_text_lower)]
        return CVMatch(score=score, matched=matched, missing_skills=missing)


# ── Singleton accessor (deferred load — first call triggers download) ────────

_lock = threading.Lock()
_matcher: CVMatcher | None = None


def get_matcher() -> CVMatcher:
    global _matcher
    if _matcher is None:
        with _lock:
            if _matcher is None:
                _matcher = CVMatcher()
    return _matcher


def match_dict(email_text: str) -> dict:
    """JSON-serializable wrapper used by the agent."""
    m = get_matcher().match(email_text)
    return {
        "score": m.score,
        "matched": m.matched,
        "missing_skills": m.missing_skills,
    }


# ── Self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    samples = [
        ("AI/ML role",
         "We are hiring a deep learning intern. Experience with PyTorch and "
         "TensorFlow is required. Familiarity with NLP and transformers is a plus."),
        ("Embedded role",
         "Embedded firmware intern: write C and VHDL for a microcontroller-based "
         "control system. Experience with FPGAs, Verilog, and RTOS preferred."),
        ("DevOps role",
         "DevOps intern: Docker, Kubernetes, Linux, CI/CD pipelines, AWS deployments."),
        ("Off-target",
         "Marketing intern: copywriting, social media, brand strategy, market research."),
    ]
    m = get_matcher()
    print(f"Loaded CV with {len(m.chunks)} chunks.\n")
    for name, body in samples:
        r = m.match(body)
        print(f"=== {name} ===  score={r.score}")
        for hit in r.matched:
            print(f"  matched ({hit['similarity']:.2f}): {hit['chunk']!r}")
        if r.missing_skills:
            print(f"  missing: {r.missing_skills}")
        print()
