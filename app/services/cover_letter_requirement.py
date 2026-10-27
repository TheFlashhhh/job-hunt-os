"""Does this posting actually ask for a cover letter?

The temptation is to answer "probably" and let the user sort it out. That is
worse than useless in both directions: a false REQUIRED sends the candidate writing a
letter nobody wanted and, if readiness gates on it, blocks an application that
was ready; a missed REQUIRED gets an application discarded unread.

So the bar for REQUIRED is that **the posting says so**. Four facts about the
world that are NOT evidence of a requirement, and each of which produced a false
positive in an earlier draft of this parser:

* Greenhouse and Lever render a cover-letter upload field on every posting.
* "Additional documents" and "supporting materials" are ATS furniture.
* Another posting from the same employer asked for one.
* The employer's careers page mentions cover letters somewhere.

None of those is the posting asking. Only the posting's own words count, and
only when they are about a cover letter specifically.

Everything here reads the stored description. Nothing fetches, and nothing
guesses at what an ATS form might contain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

COVER_LETTER_REQUIRED = "COVER_LETTER_REQUIRED"
COVER_LETTER_OPTIONAL = "COVER_LETTER_OPTIONAL"
COVER_LETTER_NOT_MENTIONED = "COVER_LETTER_NOT_MENTIONED"
UNKNOWN = "UNKNOWN"

#: Any mention at all. Nothing is classified without one of these nearby.
_MENTION = re.compile(r"cover[\s\-]?letters?|covering letters?|letters? of (?:interest|intent)", re.I)

#: Wording that means the posting is asking for one. Deliberately narrow: each
#: pattern has to put a requiring verb next to the words "cover letter".
_REQUIRED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"cover[\s\-]?letters?\s+(?:is|are)\s+required", re.I), "'cover letter is required'"),
    (re.compile(r"required?:?\s*(?:a\s+)?cover[\s\-]?letters?", re.I), "'required: cover letter'"),
    (
        re.compile(r"(?:please\s+)?(?:submit|include|attach|provide|send|upload)\s+(?:a\s+|your\s+)?cover[\s\-]?letters?", re.I),
        "asks you to submit a cover letter",
    ),
    (
        re.compile(r"applications?\s+(?:received\s+)?without\s+(?:a\s+)?cover[\s\-]?letters?", re.I),
        "'applications without a cover letter…'",
    ),
    (
        re.compile(r"(?:must|need to|should)\s+(?:include|submit|attach|provide)\s+(?:a\s+|your\s+)?cover[\s\-]?letters?", re.I),
        "'must include a cover letter'",
    ),
    (
        re.compile(r"cover[\s\-]?letters?\s+(?:must|should)\s+be\s+(?:included|submitted|attached)", re.I),
        "'cover letter must be included'",
    ),
    (
        re.compile(r"(?:resume|cv)\s+and\s+cover[\s\-]?letters?\s+(?:are\s+)?required", re.I),
        "'resume and cover letter required'",
    ),
)

#: Wording that offers one without demanding it. Checked BEFORE required, because
#: "you may submit a cover letter" also matches "submit a cover letter".
_OPTIONAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # "Cover letter optional." with no verb is how a lot of postings put it,
    # so the copula is optional in the pattern too.
    (
        re.compile(r"cover[\s\-]?letters?\s*[:–—-]?\s*(?:is|are)?\s*optional", re.I),
        "'cover letter optional'",
    ),
    (re.compile(r"optional:?\s*(?:a\s+)?cover[\s\-]?letters?", re.I), "'optional cover letter'"),
    (
        re.compile(r"(?:you\s+)?(?:may|can|are welcome to|are invited to)\s+(?:also\s+)?(?:submit|include|attach|provide|send|upload)\s+(?:a\s+|your\s+)?cover[\s\-]?letters?", re.I),
        "'you may include a cover letter'",
    ),
    (
        re.compile(r"feel free to\s+(?:submit|include|attach|share|send)\s+(?:a\s+|your\s+)?cover[\s\-]?letters?", re.I),
        "'feel free to submit a cover letter'",
    ),
    (
        re.compile(r"cover[\s\-]?letters?\s+(?:are\s+)?(?:welcome|encouraged|appreciated)", re.I),
        "'cover letters are welcome'",
    ),
    (
        re.compile(r"(?:if you (?:would like|wish|choose)|should you wish)[^.]{0,60}cover[\s\-]?letters?", re.I),
        "'if you would like to include a cover letter'",
    ),
    (
        re.compile(r"cover[\s\-]?letters?\s*\(optional\)", re.I),
        "'cover letter (optional)'",
    ),
)

#: Phrases that mention cover letters without saying anything about THIS
#: application — boilerplate, privacy notices, and ATS field labels.
_NON_COMMITTAL = (
    re.compile(r"(?:additional|supporting)\s+(?:documents?|materials?)", re.I),
    re.compile(r"cover[\s\-]?letters?\s*(?:field|upload|attachment)", re.I),
    re.compile(r"(?:resume|cv)\s*/\s*cover[\s\-]?letters?\s*(?:upload|field)?", re.I),
)


@dataclass
class CoverLetterRequirement:
    """What the posting says, and the words it says it with."""

    status: str
    #: The sentence the verdict came from, verbatim. A user who disagrees with
    #: the classification can see exactly what it read.
    evidence: str = ""
    reason: str = ""
    matched_phrases: list[str] = field(default_factory=list)

    @property
    def required(self) -> bool:
        return self.status == COVER_LETTER_REQUIRED

    @property
    def label(self) -> str:
        return {
            COVER_LETTER_REQUIRED: "Required",
            COVER_LETTER_OPTIONAL: "Optional",
            COVER_LETTER_NOT_MENTIONED: "Not mentioned",
        }.get(self.status, "Unknown")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "label": self.label,
            "required": self.required,
            "evidence": self.evidence,
            "reason": self.reason,
            "matched_phrases": self.matched_phrases,
        }


def detect_cover_letter_requirement(description: str | None) -> CoverLetterRequirement:
    """Classify from the stored posting text alone."""
    text = (description or "").strip()
    if not text:
        return CoverLetterRequirement(
            status=UNKNOWN,
            reason="No description is stored for this posting, so it cannot be read.",
        )

    if not _MENTION.search(text):
        return CoverLetterRequirement(
            status=COVER_LETTER_NOT_MENTIONED,
            reason="The posting does not mention a cover letter.",
        )

    # Optional first: "you may submit a cover letter" contains "submit a cover
    # letter", so checking required first would call every offer a demand.
    for pattern, phrase in _OPTIONAL_PATTERNS:
        match = pattern.search(text)
        if match:
            return CoverLetterRequirement(
                status=COVER_LETTER_OPTIONAL,
                evidence=_sentence_around(text, match.start()),
                reason=f"The posting offers a cover letter — {phrase}.",
                matched_phrases=[phrase],
            )

    for pattern, phrase in _REQUIRED_PATTERNS:
        match = pattern.search(text)
        if match:
            sentence = _sentence_around(text, match.start())
            # A requiring phrase inside an offer sentence is still an offer.
            if any(optional.search(sentence) for optional, _ in _OPTIONAL_PATTERNS):
                continue
            return CoverLetterRequirement(
                status=COVER_LETTER_REQUIRED,
                evidence=sentence,
                reason=f"The posting asks for a cover letter — {phrase}.",
                matched_phrases=[phrase],
            )

    mention = _MENTION.search(text)
    sentence = _sentence_around(text, mention.start()) if mention else ""
    if any(pattern.search(sentence) for pattern in _NON_COMMITTAL):
        return CoverLetterRequirement(
            status=COVER_LETTER_NOT_MENTIONED,
            evidence=sentence,
            reason=(
                "A cover letter appears only as an upload field or among "
                "'additional documents', which is not the posting asking for one."
            ),
        )

    return CoverLetterRequirement(
        status=UNKNOWN,
        evidence=sentence,
        reason=(
            "The posting mentions a cover letter, but not in wording that says "
            "whether it is required or optional."
        ),
    )


def _sentence_around(text: str, index: int) -> str:
    """The sentence containing an offset, so a verdict can be checked."""
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    end_candidates = [pos for pos in (text.find(".", index), text.find("\n", index)) if pos != -1]
    end = min(end_candidates) + 1 if end_candidates else len(text)
    return " ".join(text[start:end].split()).strip()
