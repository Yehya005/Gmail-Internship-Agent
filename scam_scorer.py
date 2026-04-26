"""Heuristic scam-risk scorer for inbox emails.

Pure deterministic features — no LLM call. The output is consumed both by
Claude Code (the LLM in this scaffold) when deciding whether to apply the
'Scam Risk' label, and by the eventual UI for explanation.

Each heuristic returns 0 or 1 (or a small float) and a human-readable
reason string. The final score is a weighted sum, clipped to [0, 1].

The weights are deliberately simple — calibration against a real labelled
set is future work and would belong in a separate evaluation script.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

# ── Lexicons ─────────────────────────────────────────────────────────────────

# Free-mail domains that are suspicious when the message claims to be from
# a corporate recruiter. Not exhaustive; expand as needed.
FREE_MAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "yahoo.co.uk",
    "aol.com", "icloud.com", "live.com", "protonmail.com", "mail.com",
}

# TLDs frequently used for throwaway and scam domains. .com/.org/.edu/.io
# are not in this set.
SUSPICIOUS_TLDS = {
    "xyz", "top", "click", "link", "info", "biz", "tk", "ml", "ga", "cf",
    "country", "zip", "review", "loan", "men", "racing",
}

# URL shorteners — legitimate, but legitimate recruiters rarely use them.
URL_SHORTENERS = {
    "bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly",
    "shorte.st", "rebrand.ly", "cutt.ly",
}

# Phrases that almost never appear in a legitimate internship offer. Each
# match contributes one reason; multiple matches saturate at the cap.
PAYMENT_PHRASES = [
    "training fee", "registration fee", "registration deposit",
    "processing fee", "visa fee", "background check fee",
    "verification fee", "verification charge", "eligibility fee",
    "enrollment fee", "enrollment charge", "onboarding fee",
    "onboarding charge", "one-time charge", "one-time fee",
    "refundable fee", "reimbursed upon completion", "fully reimbursed",
    "adjusted in your first", "adjusted in the first stipend",
    "send money", "wire transfer", "western union", "moneygram",
    "bitcoin", "btc", "crypto payment", "gift card",
    "advance payment", "upfront payment", "deposit required",
    "as deposit", "as a deposit",
    "pay a fee", "pay the fee", "pay a deposit",
    "purchase a kit", "purchase equipment",
]

# Recruiters who skip interviews / mention "no interview" are almost always
# either auto-applies (irrelevant) or scams. Interviews are universal in
# legitimate internship hiring.
NO_INTERVIEW_PHRASES = [
    "no interview required", "no prior interview", "no prior screening",
    "fast-track onboarding", "fast track onboarding",
    "skip the interview process",
]

# Pressure / urgency cues.
URGENCY_PHRASES = [
    "urgent", "act now", "limited spots", "limited slots",
    "seats are limited", "rolling basis", "at the earliest",
    "as soon as possible", "respond within 24 hours",
    "respond immediately", "register today", "register now",
    "last chance", "today only", "expires today", "don't miss",
]

# Regex: any "$<amount>" mention next to a fee/charge/payment word.
# Catches "$60 enrollment charge", "100$ verification charge", etc.
DOLLAR_FEE_RE = re.compile(
    r"(?:\$\s*\d+|\d+\s*\$)\s*(?:\w+\s+){0,2}(?:fee|charge|payment|deposit|stipend\s+adjustment)",
    re.IGNORECASE,
)

# Hyperbolic compensation cues (real internships rarely promise these).
HYPE_PHRASES = [
    "guaranteed placement", "guaranteed job", "guaranteed income",
    "earn $", "make $", "high pay", "no experience required",
    "work from home and earn", "weekly pay guaranteed",
]

# Common impersonal greetings.
GENERIC_GREETINGS = [
    "dear candidate", "dear applicant", "dear sir/madam",
    "dear user", "dear recipient", "to whom it may concern",
    "hello dear", "dear friend",
]

URL_RE = re.compile(r"https?://[^\s<>()\"]+", re.IGNORECASE)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _domain_of(email_or_addr: str) -> str:
    """Pull the domain out of an email or from an address-like string."""
    if not email_or_addr:
        return ""
    m = re.search(r"[\w.+-]+@([\w.-]+)", email_or_addr.lower())
    return m.group(1) if m else ""


def _tld(domain: str) -> str:
    return domain.rsplit(".", 1)[-1] if "." in domain else ""


def _claims_corporate(body: str, sender_name: str) -> bool:
    """Heuristic: body or display name mentions a known corporate brand."""
    big_brands = (
        "google", "microsoft", "amazon", "meta", "apple", "netflix",
        "linkedin", "uber", "tesla", "nvidia", "openai", "anthropic",
        "ibm", "oracle", "intel", "samsung",
    )
    blob = f"{sender_name} {body[:300]}".lower()
    return any(b in blob for b in big_brands)


# ── Scorer ───────────────────────────────────────────────────────────────────

@dataclass
class ScamFeatures:
    score: float                    # 0..1
    reasons: list[str]              # human-readable, one per triggered rule
    flags: dict[str, bool | int]    # raw boolean/int features for inspection


def score_email(email: dict) -> ScamFeatures:
    """Compute scam-risk features for a single inbox email dict.

    Expected input keys: 'subject', 'sender', 'body'. Missing keys are
    treated as empty strings.
    """
    subject = (email.get("subject") or "").lower()
    sender = email.get("sender") or ""
    body = (email.get("body") or "").lower()

    flags: dict[str, bool | int] = {}
    reasons: list[str] = []
    points = 0.0  # accumulator; each weighted reason adds to this

    # 1. Sender domain analysis
    domain = _domain_of(sender)
    flags["sender_domain"] = domain
    if domain and domain in FREE_MAIL_DOMAINS and _claims_corporate(body, sender):
        reasons.append(
            f"Free-mail domain '{domain}' but message claims to be from a corporate brand"
        )
        points += 0.30
        flags["free_mail_corporate_claim"] = True
    if domain and _tld(domain) in SUSPICIOUS_TLDS:
        reasons.append(f"Sender uses suspicious TLD '.{_tld(domain)}'")
        points += 0.20
        flags["suspicious_sender_tld"] = True

    # 2. Payment / financial keywords (any match is a strong signal)
    pay_hits = [p for p in PAYMENT_PHRASES if p in body or p in subject]
    flags["payment_phrase_count"] = len(pay_hits)
    if pay_hits:
        sample = ", ".join(f"'{p}'" for p in pay_hits[:2])
        reasons.append(f"Asks for upfront payment: {sample}")
        points += 0.40  # very strong signal — saturate near scam regardless

    # 2b. Generic "$<amount> charge/fee/..." pattern, even without our
    # lexicon. This is a smoking gun: real internships never ask for a
    # specific dollar amount tied to a deposit / fee / charge / payment.
    # Weighted high enough that a single match crosses SCAM_THRESHOLD on
    # its own — combined with even one other cue it pushes well above.
    dollar_hits = DOLLAR_FEE_RE.findall(email.get("body") or "")
    flags["dollar_fee_hits"] = len(dollar_hits)
    if dollar_hits:
        reasons.append(f"Money amount tied to a fee/charge: '{dollar_hits[0]}'")
        points += 0.55

    # 2c. "No interview required" / fast-track signals — almost never legit
    no_int = [p for p in NO_INTERVIEW_PHRASES if p in body]
    flags["no_interview_hits"] = len(no_int)
    if no_int:
        reasons.append(f"Skips standard hiring steps: '{no_int[0]}'")
        points += 0.20

    # 3. Generic greeting
    if any(g in body for g in GENERIC_GREETINGS):
        reasons.append("Generic, impersonal greeting (no recipient name)")
        points += 0.10
        flags["generic_greeting"] = True

    # 4. URL analysis
    urls = URL_RE.findall(email.get("body") or "")
    flags["url_count"] = len(urls)
    suspicious_urls: list[str] = []
    for u in urls:
        try:
            host = urlparse(u).hostname or ""
        except Exception:
            host = ""
        host = host.lower()
        if any(s == host or host.endswith("." + s) for s in URL_SHORTENERS):
            suspicious_urls.append(u)
        elif _tld(host) in SUSPICIOUS_TLDS:
            suspicious_urls.append(u)
    flags["suspicious_url_count"] = len(suspicious_urls)
    if suspicious_urls:
        reasons.append(
            f"{len(suspicious_urls)} suspicious URL(s) "
            f"(shortener or risky TLD): {suspicious_urls[0]}"
        )
        points += 0.20
    if len(urls) >= 4:
        reasons.append(f"Unusually many links ({len(urls)}) for a recruiter email")
        points += 0.05

    # 5. Urgency / pressure cues
    urgency_hits = [p for p in URGENCY_PHRASES if p in body or p in subject]
    flags["urgency_hits"] = len(urgency_hits)
    if urgency_hits:
        reasons.append(f"Pressure language: '{urgency_hits[0]}'")
        points += 0.10

    # 6. Hyperbolic comp claims
    hype_hits = [p for p in HYPE_PHRASES if p in body]
    flags["hype_hits"] = len(hype_hits)
    if hype_hits:
        reasons.append(f"Unrealistic claim: '{hype_hits[0]}'")
        points += 0.15

    # 7. ALL-CAPS subject (whole subject in caps + length > 6)
    raw_subject = email.get("subject") or ""
    if len(raw_subject) > 6 and raw_subject == raw_subject.upper() and any(c.isalpha() for c in raw_subject):
        reasons.append("Subject is fully capitalized")
        points += 0.05
        flags["all_caps_subject"] = True

    score = min(1.0, round(points, 3))
    return ScamFeatures(score=score, reasons=reasons, flags=flags)


def score_email_dict(email: dict) -> dict:
    """JSON-serializable wrapper around score_email."""
    f = score_email(email)
    return {"score": f.score, "reasons": f.reasons, "flags": f.flags}


# ── Self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    samples = [
        {
            "subject": "Internship for Yehya",
            "sender": "Mazloum, Ali <amazloum@email.sc.edu>",
            "body": "We are looking for someone with a programming background. We are offering Internships!",
        },
        {
            "subject": "URGENT: Internship Opportunity at Google",
            "sender": "Google Recruiter <careers@globaltech-internship.xyz>",
            "body": (
                "Dear candidate, We at Google are offering paid internships. "
                "A small training fee of $200 is required to confirm your slot. "
                "Send via wire transfer or bitcoin. Apply within 24 hours! "
                "Visit https://bit.ly/abc123 to register. Earn $500/day guaranteed."
            ),
        },
        {
            "subject": "AI Research Intern role at our lab",
            "sender": "Dr. Smith <jsmith@university.edu>",
            "body": (
                "Hi Yehya, I came across your work on EEG seizure detection. "
                "We have an opening for an AI research intern at our lab — "
                "details at https://lab.university.edu/jobs."
            ),
        },
    ]
    for e in samples:
        f = score_email(e)
        print(f"\n{e['subject']!r}\n  score={f.score}")
        for r in f.reasons:
            print(f"    - {r}")
