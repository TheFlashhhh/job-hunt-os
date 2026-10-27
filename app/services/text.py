"""Sanitization, hashing and text normalization.

All external content is untrusted. Everything entering the pipeline passes
through :func:`sanitize_external_text` before any model or parser sees it.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

#: Hard cap after sanitization (architecture §22).
MAX_CONTENT_CHARS = 20_000

#: Zero-width and bidirectional-override characters used to hide instructions.
_HIDDEN_CHARS = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")

#: Phrases that look like an attempt to instruct the model.
#: Written with ``\s+`` rather than literal spaces on purpose. Job descriptions
#: are line-wrapped by whoever pasted them, and an injection split across a
#: newline ("ignore all previous\ninstructions") is the same attack as one on a
#: single line. Matching literal spaces would let word-wrap defeat detection.
_INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above|earlier)\s+instructions",
        r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|evidence)\s*\w*\s*"
        r"(?:instructions|prompts|bank)?",
        r"you\s+are\s+now\s+(?:a|an)\s+",
        r"new\s+instructions?\s*:",
        r"system\s+prompt",
        r"</?(?:system|assistant|instruction)s?>",
        r"forget\s+(?:everything|all\s+previous)",
        r"do\s+not\s+follow\s+the\s+(?:user|original)",
        r"output\s+(?:only|exactly)\s+(?:the\s+following|the\s+word)",
        r"rate\s+(?:this|the)\s+\w+\s+as\s+(?:a\s+)?perfect",
    )
]


@dataclass
class SanitizationReport:
    original_length: int = 0
    final_length: int = 0
    was_truncated: bool = False
    html_stripped: bool = False
    hidden_chars_removed: int = 0
    suspected_injection: bool = False
    injection_matches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "original_length": self.original_length,
            "final_length": self.final_length,
            "was_truncated": self.was_truncated,
            "html_stripped": self.html_stripped,
            "hidden_chars_removed": self.hidden_chars_removed,
            "suspected_injection": self.suspected_injection,
            "injection_matches": self.injection_matches,
        }


def sanitize_external_text(
    raw: str, *, max_chars: int = MAX_CONTENT_CHARS
) -> tuple[str, SanitizationReport]:
    """Strip HTML, hidden characters and instruction-like content markers.

    Returns the cleaned text plus a report. The report is stored on the raw
    record so an injection attempt is auditable after the fact.
    """
    report = SanitizationReport(original_length=len(raw or ""))
    if not raw:
        return "", report

    text = raw
    if _looks_like_html(text):
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        for tag in soup.find_all(None, attrs={"aria-hidden": "true"}):
            tag.decompose()
        for tag in soup.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
            tag.decompose()
        text = soup.get_text("\n")
        report.html_stripped = True

    before = len(text)
    text = _HIDDEN_CHARS.sub("", text)
    report.hidden_chars_removed = before - len(text)

    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            report.suspected_injection = True
            report.injection_matches.append(match.group(0)[:120])

    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
        report.was_truncated = True

    report.final_length = len(text)
    return text, report


def wrap_untrusted(text: str) -> str:
    """Wrap content in the explicit data boundary used in every prompt."""
    return f"<job_description_untrusted>\n{text}\n</job_description_untrusted>"


def _looks_like_html(text: str) -> bool:
    sample = text[:4000].lower()
    return "<" in sample and any(
        tag in sample
        for tag in ("<p", "<div", "<br", "<li", "<span", "<h1", "<h2", "<ul", "<table")
    )


# ---------------------------------------------------------------------------
# Hashing — load-bearing for cost control
# ---------------------------------------------------------------------------


def content_hash(*parts: Any) -> str:
    """Stable SHA-256 over the semantically meaningful parts of a payload."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(b"\x1f")
        if part is None:
            continue
        hasher.update(str(part).strip().lower().encode("utf-8", errors="replace"))
    return hasher.hexdigest()


def description_fingerprint(description: str, *, token_limit: int = 500) -> str:
    """SimHash-style fingerprint over the first N tokens, boilerplate stripped.

    EEO statements, benefits blocks and legal footers are near-identical across
    a company's postings and would otherwise produce false duplicate matches.
    """
    cleaned = strip_boilerplate(description)
    tokens = re.findall(r"[a-z0-9]+", cleaned.lower())[:token_limit]
    if not tokens:
        return ""

    bits = [0] * 64
    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest()[:16], 16)
        for i in range(64):
            bits[i] += 1 if (h >> i) & 1 else -1
    value = 0
    for i, bit in enumerate(bits):
        if bit > 0:
            value |= 1 << i
    return f"{value:016x}"


def hamming_distance(a: str, b: str) -> int:
    """Bit distance between two fingerprints. 64 when either is missing."""
    if not a or not b:
        return 64
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


_BOILERPLATE_MARKERS = [
    "equal opportunity employer",
    "equal employment opportunity",
    "we are an equal",
    "without regard to race",
    "e-verify",
    "reasonable accommodation",
    "affirmative action",
    "benefits include",
    "our benefits",
    "pay transparency",
    "privacy policy",
    "applicants with disabilities",
    "criminal histories",
]


def strip_boilerplate(text: str) -> str:
    """Truncate at the first legal/benefits boilerplate marker."""
    lowered = text.lower()
    cut = len(text)
    for marker in _BOILERPLATE_MARKERS:
        idx = lowered.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut]


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_COMPANY_SUFFIXES = re.compile(
    r"\b(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|co|co\.|"
    r"gmbh|plc|sa|nv|ag|pbc|holdings|group|technologies|technology|labs|labdotcom)\b",
    re.IGNORECASE,
)


def normalize_company(name: str) -> str:
    """Lowercase, strip punctuation and legal suffixes for dedupe matching."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).lower()
    text = re.sub(r"[^\w\s&-]", " ", text)
    text = _COMPANY_SUFFIXES.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_TITLE_NOISE = re.compile(
    r"\b(?:remote|hybrid|on-?site|full[- ]?time|part[- ]?time|contract|"
    r"\d{4}|f/m/d|m/f/d|w/m/d|us|usa|united states)\b",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    """Normalize a job title for dedupe: lowercase, drop noise and req IDs."""
    if not title:
        return ""
    text = title.lower()
    text = re.sub(r"\(([^)]*)\)", " ", text)  # drop parentheticals
    text = re.sub(r"[\[\]\-–—_,|/]+", " ", text)
    text = re.sub(r"\breq(?:uisition)?\s*#?\s*\w+\b", " ", text)
    text = _TITLE_NOISE.sub(" ", text)
    text = re.sub(r"[^\w\s+#.]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonicalize_url(url: str | None) -> str | None:
    """Strip tracking parameters and normalize form for dedupe."""
    if not url:
        return None
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url

    # Identity parameters are the job's primary key on career sites that embed
    # the ATS rather than redirect to it. They MUST survive canonicalization.
    #
    # This is not hypothetical. Toast's Greenhouse board returns
    # `careers.toasttab.com/jobs?gh_jid=8038744`. Dropping `gh_jid` because it
    # starts with the `gh_` tracking prefix reduced all 319 of their distinct
    # postings to one URL, and tier-2 dedupe then merged them into a SINGLE job
    # carrying 275 versions. `gh_src` is tracking; `gh_jid` is the job.
    keep_exact = {
        "gh_jid",
        "jobid",
        "job_id",
        "jid",
        "id",
        "posting",
        "postingid",
        "requisitionid",
        "req",
        "reqid",
        "vacancyid",
    }
    drop_prefixes = ("utm_", "gh_", "lever-")
    drop_exact = {"ref", "source", "src", "trk", "refid", "recruiter", "hl", "gclid", "fbclid"}
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() in keep_exact
        or (not k.lower().startswith(drop_prefixes) and k.lower() not in drop_exact)
    ]
    # Stable ordering so two spellings of the same URL canonicalize identically.
    kept.sort()

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, urlencode(kept), ""))
