"""Autonomous label decision for an email — used as a fallback when no
LLM (or Claude Code via to_label.json) is available.

Decision flow per email:

  1. If scam_features.score >= SCAM_THRESHOLD → ["Scam Risk"] only
     (per user rule: Scam Risk is exclusive — no topic labels).
  2. Else if the body doesn't look like an internship offer → no labels.
  3. Else if CV-match score is too low → no labels (off-target role).
  4. Else: walk the topic keyword tables and emit every matching topic.

The keyword tables are tuned against the user's CV (plan.txt). Update
them there when the CV changes — KNOWN_SKILLS in cv_match.py is the
related vocabulary used for retrieval and missing-skill detection.
"""
from __future__ import annotations

# Thresholds — calibrated against the live test set on 2026-04-26.
SCAM_THRESHOLD = 0.5            # 0.5+ heuristic score → label as scam only
CV_MATCH_THRESHOLD = 0.30       # below this, the email isn't a real CV fit
INTERNSHIP_TERMS = ("internship", "intern ", "intern,", "intern.", "interns")

# Topic → keyword list. A topic fires when ANY of its keywords appears in
# the subject + body. Keep matches lowercase and surrounded by spaces or
# punctuation where ambiguity is possible (e.g., 'ml' would over-match).
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "AI/ML": [
        "ai/ml", "machine learning", "deep learning", "neural network",
        "pytorch", "tensorflow", "keras", "nlp", "natural language",
        "computer vision", "transformer", " ml ", " ai ", "artificial intelligence",
        "huggingface", "llm",
    ],
    "Research": [
        "research", "neuroscience", "bioinformatics", "publication",
        " phd ", "lab ", "research-oriented", "research-focused",
        "drug-target", "brain-computer", "eeg", " bci ", "computational biology",
    ],
    "Software Engineering": [
        "software engineer", "software development", "software developer",
        "full-stack", "fullstack", "full stack", "backend", "back-end",
        "frontend", "front-end", "web development", "flask", "react",
        "django", "node.js", "node ", "rest api", "fastapi",
    ],
    "Embedded Systems": [
        "embedded", "microcontroller", "vhdl", "fpga", "verilog",
        " rtos ", "firmware", "digital design", "low-level",
        "real-time system", "hardware design",
    ],
    "DevOps": [
        "devops", "docker", "kubernetes", "k8s", "ci/cd", "ci-cd",
        "infrastructure", "deployment", "linux administration",
        "ansible", "terraform", " aws ", " gcp ", " azure ",
        "site reliability", " sre ",
    ],
}


def _looks_like_internship(text: str) -> bool:
    return any(term in text for term in INTERNSHIP_TERMS)


def classify_email(email: dict) -> list[str]:
    """Decide labels for a single email dict (fields: subject, body,
    scam_features, cv_match). Returns a list of label names — possibly
    empty when the email isn't a relevant internship for this user."""
    scam = email.get("scam_features") or {}
    cv = email.get("cv_match") or {}
    subject = (email.get("subject") or "").lower()
    body = (email.get("body") or "").lower()
    text = f" {subject} {body} "

    # Rule 1: scam-first, exclusive
    if (scam.get("score") or 0) >= SCAM_THRESHOLD:
        return ["Scam Risk"]

    # Rule 2: must look like an internship to get a topic label at all
    if not _looks_like_internship(text):
        return []

    # Rule 3: avoid labelling roles the CV clearly doesn't match
    if (cv.get("score") or 0) < CV_MATCH_THRESHOLD:
        return []

    # Rule 4: emit every topic whose keywords appear
    labels: list[str] = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            labels.append(topic)
    return labels


# ── Self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    samples = [
        ("clean AI/ML", {
            "subject": "Hello",
            "body": "We are offering internships in Deep learning. So candidates with such skill are welcomed.",
            "scam_features": {"score": 0.0},
            "cv_match": {"score": 0.59},
        }),
        ("buried scam", {
            "subject": "Open Internship Opportunities",
            "body": "Dear Candidate, we offer an internship across AI/ML and DevOps. To proceed, submit a one-time enrollment charge of $60.",
            "scam_features": {"score": 0.8},
            "cv_match": {"score": 0.4},
        }),
        ("DevOps role", {
            "subject": "DevOps internship",
            "body": "Looking for a DevOps intern with Docker, Linux, CI/CD experience.",
            "scam_features": {"score": 0.0},
            "cv_match": {"score": 0.42},
        }),
        ("off-target", {
            "subject": "Marketing internship",
            "body": "We offer a marketing internship. Skills: copywriting, brand strategy.",
            "scam_features": {"score": 0.0},
            "cv_match": {"score": 0.20},
        }),
    ]
    for name, e in samples:
        print(f"{name:>20s}: {classify_email(e)}")
