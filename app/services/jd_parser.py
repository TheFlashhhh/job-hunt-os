"""Deterministic job-description parsing.

Real postings are not tidy. The same role arrives as a clean bullet list from
Greenhouse, as one unbroken paragraph pasted out of LinkedIn, as ALL-CAPS
headings with pipe-delimited fragments from an older ATS, and sometimes as a
soup of HTML entities and stray characters. A parser that only understands
bullets is a parser that returns nothing for half the jobs actually being
applied to.

So extraction runs in three passes, in order of decreasing confidence:

1. **Bulleted lines** inside a recognised section — highest confidence.
2. **Unbulleted lines** inside a requirements-shaped section — an ATS habit:
   the heading says "Qualifications", then each line is a bare fragment.
3. **Sentences anywhere** that are requirement-shaped by their own wording —
   the prose fallback.

Each pass only accepts text that is *already present in the document*. A
sentence is promoted to a requirement because of how it reads, never because
the parser thinks the role probably wants it. There is no generation step here,
and adding one would be a defect: an invented requirement becomes an invented
gap, which becomes pressure to make an untrue claim.

The parser deliberately does not sanitize. Callers pass text that has already
been through :func:`app.services.text.sanitize_external_text`. Injection
attempts that survive as ordinary prose are treated as ordinary prose — inert
text that may be extracted and displayed, never instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.enums import RequirementKind

# ---------------------------------------------------------------------------
# Section headings
# ---------------------------------------------------------------------------

#: Heading → the kind that bullets under it inherit.
_SECTION_HEADS: list[tuple[RequirementKind, re.Pattern[str]]] = [
    # Preference headings are tested first: "Preferred Qualifications" also
    # matches the generic "Qualifications" pattern, and the more specific
    # reading is the correct one.
    (
        RequirementKind.STRONG_PREFERENCE,
        re.compile(
            r"^\W*(?:preferred|desired|additional)\s+(?:qualifications?|skills?|experience)\b"
            r"|^\W*(?:nice[- ]to[- ]haves?|bonus(?:\s+points?)?|pluses?|"
            r"we.?d\s+(?:love|be\s+excited)|even\s+better|icing\s+on\s+the\s+cake)\b",
            re.I,
        ),
    ),
    (
        RequirementKind.MUST_HAVE,
        re.compile(
            r"^\W*(?:what\s+you.?ll?\s+need|requirements?|qualifications?|"
            r"minimum\s+qualifications?|basic\s+qualifications?|required\s+qualifications?|"
            r"must[- ]haves?|you\s+(?:have|bring|will\s+have)|required\s+skills?|"
            r"who\s+you\s+are|what\s+we.?re\s+looking\s+for|skills?\s*(?:&|and)\s*experience|"
            r"experience\s+(?:required|we.?re\s+looking\s+for))\b",
            re.I,
        ),
    ),
    (
        RequirementKind.RESPONSIBILITY,
        re.compile(
            r"^\W*(?:what\s+you.?ll?\s+do|responsibilities|key\s+responsibilities|"
            r"the\s+role|about\s+the\s+role|your\s+impact|day[- ]to[- ]day|"
            r"in\s+this\s+role|duties|essential\s+(?:functions|duties)|"
            r"what\s+you\s+will\s+be\s+doing)\b",
            re.I,
        ),
    ),
]

#: Headings that END a requirements region. Anything after one of these is
#: boilerplate, and boilerplate that reads like a requirement ("must be able to
#: lift 20 lbs", "we are an equal opportunity employer") is noise at best and
#: actively misleading at worst.
_TERMINAL_HEADS = re.compile(
    r"^\W*(?:benefits?|perks?|compensation|salary|pay\s+range|hourly\s+pay|"
    r"equal\s+(?:opportunity|employment)|eeo\b|diversity|accommodations?|"
    r"how\s+to\s+apply|application\s+(?:process|instructions)|next\s+steps|"
    r"legal|privacy|disclaimer)\b",
    re.I,
)

#: Company-blurb headings. These are NOT terminal — they routinely *open* a
#: posting, before any requirement has appeared.
#:
#: Treating them as terminal was a real defect. "About Us" is the first line of
#: an enormous share of real postings, and suppressing from there discarded the
#: whole document: a 9,546-character SharkNinja posting containing 22 clean
#: bullets parsed to ZERO requirements. These now close the current section and
#: let extraction continue.
_INTRO_HEADS = re.compile(
    r"^\W*(?:about\s+(?:us|the\s+(?:company|team|role))|who\s+we\s+are|"
    r"our\s+(?:values|culture|mission|story|team)|why\s+(?:join|work)|"
    r"company\s+(?:overview|description)|overview)\b",
    re.I,
)

_BULLET_RE = re.compile(r"^\s*(?:[-*•·▪◦‣o]|\(?\d{1,2}[.)])\s+(.{4,600})$")

#: A line that is a heading rather than content. Used to close a section when
#: the next heading is one this parser does not recognise by name.
#:
#: The test has to be strict. Inside an ATS "QUALIFICATIONS" block every line is
#: a bare capitalised fragment with no terminal period — "Develop predictive
#: models using Python" looks exactly like a title. Only two signals reliably
#: separate a heading from content in that context: the line is ALL CAPS, or it
#: ends in a colon.
_LOOKS_LIKE_HEADING = re.compile(r"^\W*(?:[A-Z0-9][^a-z]{2,60}|[A-Z][^.!?]{2,60}:)\s*$")

_PREFERRED_INLINE = re.compile(
    r"\b(preferred|nice[- ]to[- ]have|bonus|a\s+plus|ideally|desirable|"
    r"would\s+be\s+(?:great|nice)|familiarity\s+with)\b",
    re.I,
)
_MUST_INLINE = re.compile(
    r"\b(must|required?|requires?|strong|proven|demonstrated|solid|"
    r"expert(?:ise)?|deep\s+(?:experience|knowledge))\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Prose-sentence signals
# ---------------------------------------------------------------------------

#: Phrasings that make a *sentence* a requirement. Deliberately conservative:
#: each pattern describes something the candidate must have or do, not a
#: description of the company, the product, or the perks.
_REQUIREMENT_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    (
        "experience_with",
        re.compile(r"\bexperience\s+(?:with|in|using|building|developing)\b", re.I),
    ),
    ("proficiency", re.compile(r"\b(?:proficien\w+|fluen\w+|command)\s+(?:with|in|of)\b", re.I)),
    ("familiarity", re.compile(r"\b(?:familiarity|comfort(?:able)?)\s+(?:with|using)\b", re.I)),
    ("knowledge_of", re.compile(r"\b(?:knowledge|understanding)\s+of\b", re.I)),
    ("years", re.compile(r"\b\d+\s*\+?\s*(?:-\s*\d+\s*)?years?\b", re.I)),
    ("must", re.compile(r"\b(?:must|should)\s+(?:have|be|possess|bring|demonstrate)\b", re.I)),
    (
        "we_require",
        re.compile(r"\b(?:we\s+(?:require|expect)|is\s+required|are\s+required)\b", re.I),
    ),
    (
        "looking_for",
        re.compile(r"\b(?:looking\s+for|seeking)\s+(?:someone|a\s+candidate|an?\b)", re.I),
    ),
    ("candidate_will", re.compile(r"\b(?:the\s+)?(?:ideal|successful)\s+candidate\b", re.I)),
    ("you_will", re.compile(r"\byou\s+(?:will|'ll)\s+(?:be\s+)?\w+", re.I)),
    ("ability_to", re.compile(r"\bability\s+to\s+\w+", re.I)),
    ("degree", re.compile(r"\b(?:bachelor|master|phd|degree|enrolled|graduat\w+)\b", re.I)),
    ("skilled_in", re.compile(r"\b(?:skilled|versed|hands[- ]on)\s+(?:in|with)\b", re.I)),
]

#: Sentences about the company rather than the candidate. These frequently
#: contain technology names ("our platform is built on Kafka") and would
#: otherwise be mistaken for requirements.
_COMPANY_VOICE = re.compile(
    r"^\W*(?:we\s+(?:are|were|have\s+been|offer|provide|believe|value|celebrate)\b"
    r"|our\s+(?:mission|vision|culture|values|team\s+is|company\s+is)\b"
    r"|founded\s+in\b|headquartered\b|backed\s+by\b"
    r"|the\s+(?:company|organization)\s+(?:is|was|has)\b)",
    re.I,
)

_RESPONSIBILITY_VOICE = re.compile(
    r"\byou.?(?:ll|\s+will)\s+|^\W*(?:build|design|develop|create|analy[sz]e|partner|"
    r"collaborate|own|drive|support|maintain|present|deliver|contribute)\b",
    re.I,
)

_BENEFIT_NOISE = re.compile(
    r"\b(?:equal\s+opportunity|without\s+regard\s+to|401\s*\(?k\)?|dental|vision\s+insurance|"
    r"paid\s+time\s+off|pto\b|parental\s+leave|unlimited\s+vacation|stock\s+options?|"
    r"competitive\s+(?:salary|compensation)|apply\s+(?:now|today)|click\s+here|"
    r"e[- ]?verify|reasonable\s+accommodation)\b",
    re.I,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n{2,}")


@dataclass
class ParsedRequirement:
    text: str
    kind: RequirementKind
    keywords: list[str] = field(default_factory=list)
    order_index: int = 0
    #: How this line was recovered — useful when a JD parses badly and the
    #: reason needs to be visible rather than guessed at.
    extraction: str = "BULLET"
    confidence: float = 1.0


@dataclass
class ParseReport:
    """Why the parse produced what it did."""

    requirements: list[ParsedRequirement] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    bullet_count: int = 0
    line_count: int = 0
    sentence_count: int = 0
    used_prose_fallback: bool = False
    truncated: bool = False

    @property
    def total(self) -> int:
        return len(self.requirements)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_requirements(description: str, *, limit: int = 40) -> list[ParsedRequirement]:
    """Extract requirements and responsibilities from JD text."""
    return parse_description(description, limit=limit).requirements


def parse_description(description: str, *, limit: int = 40) -> ParseReport:
    """Extract requirements and report how they were found."""
    report = ParseReport()
    if not description or not description.strip():
        return report

    lines = _clean_lines(description)
    structured, consumed = _parse_structured(lines, report, limit=limit)

    # The prose pass always runs, over whatever the structured passes did not
    # consume. An earlier design gated it on "did we find enough bullets?",
    # which lost real requirements: a JD with three bullets followed by
    # "Additionally, familiarity with Kafka is a plus" cleared the gate and
    # then dropped the Kafka sentence. Structure and prose are complementary,
    # not alternatives.
    prose = _parse_prose(lines, consumed, report)
    report.used_prose_fallback = bool(prose)

    report.requirements = _finalize(structured + prose, limit=limit, report=report)
    return report


# ---------------------------------------------------------------------------
# Pass 1 and 2 — structured text
# ---------------------------------------------------------------------------


#: A line at least this long with no terminal punctuation was almost certainly
#: wrapped by the editor mid-sentence. ATS list items are shorter than this and
#: end at their own boundary, so they are not joined to their neighbour.
_WRAP_WIDTH = 64


def _clean_lines(description: str) -> list[str]:
    """Split into lines, repairing the common paste artefacts."""
    text = description.replace("\r\n", "\n").replace("\r", "\n")
    # A pasted bullet list often arrives as one line: "• A • B • C".
    text = re.sub(r"\s+([•·▪◦‣])\s+", r"\n\1 ", text)

    # Rejoin hard-wrapped paragraphs. Without this a wrapped sentence inside a
    # "Qualifications" block splits into two fragments, and half a sentence
    # becomes a "requirement".
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        previous = out[-1].strip() if out else ""
        if (
            stripped
            and previous
            and len(previous) >= _WRAP_WIDTH
            and not previous.endswith((".", "!", "?", ":", ";", ",", "-"))
            and not _BULLET_RE.match(line)
            and not _LOOKS_LIKE_HEADING.match(stripped)
        ):
            out[-1] = f"{out[-1]} {stripped}"
            continue
        out.append(line)
    return out


def _parse_structured(
    lines: list[str], report: ParseReport, *, limit: int
) -> tuple[list[ParsedRequirement], set[int]]:
    """Return the structured requirements and the line indices they used."""
    out: list[ParsedRequirement] = []
    consumed: set[int] = set()
    current = RequirementKind.AMBIGUOUS
    in_section = False
    #: Set once a boilerplate heading is reached. Bullets after "Benefits" are
    #: perks, not requirements, and a bullet list is no evidence to the
    #: contrary. Only a new requirements heading reopens extraction.
    suppressed = False

    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            consumed.add(index)
            continue
        report.line_count += 1

        bullet = _BULLET_RE.match(raw)

        # Heading tests run only on non-bullet lines. "- Requirement number 3
        # involving Python" starts with the word "Requirement" and would
        # otherwise be read as a "Requirements" heading, silently swallowing
        # the entire list it belongs to.
        if not bullet:
            if _TERMINAL_HEADS.match(line) and len(line) < 120:
                current = RequirementKind.AMBIGUOUS
                in_section = False
                suppressed = True
                consumed.add(index)
                continue

            if _INTRO_HEADS.match(line) and len(line) < 120:
                # A company blurb, not the end of the posting. Close the current
                # section so its kind cannot leak into what follows, and carry
                # on reading — the requirements are still ahead.
                current = RequirementKind.AMBIGUOUS
                in_section = False
                consumed.add(index)
                continue

            heading = _match_heading(line)
            if heading is not None:
                current = heading
                in_section = True
                suppressed = False
                name = line.strip(" :#*-")
                if name not in report.sections_found:
                    report.sections_found.append(name)
                consumed.add(index)
                continue

        if suppressed:
            # Boilerplate. Consumed so the prose pass does not resurrect it.
            consumed.add(index)
            continue

        if bullet:
            # A bullet is consumed either way: if it was too short or too noisy
            # to be a requirement, re-reading it as prose will not help.
            consumed.add(index)
            text = _normalize(bullet.group(1))
            if _acceptable(text):
                report.bullet_count += 1
                out.append(
                    ParsedRequirement(
                        text=text,
                        kind=_refine_kind(current, text),
                        extraction="BULLET",
                        confidence=1.0,
                    )
                )
            continue

        # Pass 2: a bare line inside a requirements section — the ATS habit of
        # a heading followed by unbulleted fragments. Accepted only when the
        # line behaves like a list item: one sentence, list-item length, and
        # not company marketing. Multi-sentence paragraphs fall through to the
        # prose pass, which reads them sentence by sentence instead of storing
        # a whole paragraph as one "requirement".
        if in_section and not _LOOKS_LIKE_HEADING.match(line):
            text = _normalize(line)
            if _is_list_item(text):
                consumed.add(index)
                out.append(
                    ParsedRequirement(
                        text=text,
                        kind=_refine_kind(current, text),
                        extraction="SECTION_LINE",
                        confidence=0.8,
                    )
                )
            # Not a list item: leave it unconsumed for the prose pass.
            continue

        if in_section and _LOOKS_LIKE_HEADING.match(line):
            # An unrecognised heading closes the current section rather than
            # letting it swallow the rest of the document.
            in_section = False
            current = RequirementKind.AMBIGUOUS
            consumed.add(index)

        if len(out) >= limit * 2:
            break

    return out, consumed


def _match_heading(line: str) -> RequirementKind | None:
    if len(line) > 120:
        return None
    for kind, pattern in _SECTION_HEADS:
        if pattern.match(line):
            return kind
    return None


# ---------------------------------------------------------------------------
# Pass 3 — prose fallback
# ---------------------------------------------------------------------------


def _parse_prose(
    lines: list[str], consumed: set[int], report: ParseReport
) -> list[ParsedRequirement]:
    """Promote requirement-shaped sentences. Extraction only, never invention."""
    out: list[ParsedRequirement] = []
    seen: set[str] = set()

    for chunk in _prose_chunks(lines, consumed):
        for sentence in _SENTENCE_SPLIT.split(chunk):
            text = _normalize(sentence)
            report.sentence_count += 1
            if not _acceptable(text) or len(text) > 400:
                continue
            if _BENEFIT_NOISE.search(text) or _COMPANY_VOICE.match(text):
                continue

            signals = [name for name, pattern in _REQUIREMENT_SIGNALS if pattern.search(text)]
            if not signals:
                continue

            key = text.lower()
            if key in seen:
                continue
            seen.add(key)

            # A sentence can carry several clauses ("You will build pipelines.
            # You need 2 years of Python."). Splitting on semicolons and " and "
            # would fragment technology lists, so the sentence is kept whole —
            # the matcher reads keywords, not grammar.
            out.append(
                ParsedRequirement(
                    text=text,
                    kind=_prose_kind(text, signals),
                    extraction="PROSE",
                    confidence=0.55 + 0.1 * min(len(signals), 3),
                )
            )

    return out


def _prose_chunks(lines: list[str], consumed: set[int]) -> list[str]:
    """Contiguous runs of lines the structured passes did not take."""
    chunks: list[str] = []
    buffer: list[str] = []
    for index, raw in enumerate(lines):
        line = raw.strip()
        if not line or index in consumed:
            if buffer:
                chunks.append(" ".join(buffer))
                buffer = []
            continue
        buffer.append(line)
    if buffer:
        chunks.append(" ".join(buffer))
    return chunks


def _prose_kind(text: str, signals: list[str]) -> RequirementKind:
    if _PREFERRED_INLINE.search(text):
        return RequirementKind.NICE_TO_HAVE
    if _RESPONSIBILITY_VOICE.search(text) and not _MUST_INLINE.search(text):
        return RequirementKind.RESPONSIBILITY
    if _MUST_INLINE.search(text) or {"years", "must", "we_require", "degree"} & set(signals):
        return RequirementKind.MUST_HAVE
    return RequirementKind.AMBIGUOUS


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" .;:•·-–—*")


def _is_list_item(text: str) -> bool:
    """True when a bare line reads like a list entry rather than a paragraph."""
    if not _acceptable(text) or len(text) > 200:
        return False
    if _COMPANY_VOICE.match(text) or _BENEFIT_NOISE.search(text):
        return False
    # More than one sentence means prose. Count only mid-string terminators.
    return len(_SENTENCE_SPLIT.split(text)) == 1


def _acceptable(text: str) -> bool:
    """Reject fragments that cannot carry meaning."""
    if len(text) < 8:
        return False
    letters = sum(ch.isalpha() for ch in text)
    if letters < 6:
        return False
    # Guard against noise: a "sentence" that is mostly punctuation or symbols.
    if letters / len(text) < 0.45:
        return False
    return len(text.split()) >= 2


def _refine_kind(section_kind: RequirementKind, text: str) -> RequirementKind:
    if _PREFERRED_INLINE.search(text):
        return RequirementKind.NICE_TO_HAVE
    if section_kind == RequirementKind.AMBIGUOUS and _MUST_INLINE.search(text):
        return RequirementKind.MUST_HAVE
    return section_kind


def _finalize(
    items: list[ParsedRequirement], *, limit: int, report: ParseReport
) -> list[ParsedRequirement]:
    """De-duplicate, keep the highest-confidence copy, renumber."""
    best: dict[str, ParsedRequirement] = {}
    for item in items:
        key = re.sub(r"[^a-z0-9 ]", "", item.text.lower()).strip()
        existing = best.get(key)
        if existing is None or item.confidence > existing.confidence:
            best[key] = item

    ordered = sorted(best.values(), key=lambda r: (-r.confidence, len(r.text)))
    if len(ordered) > limit:
        report.truncated = True
        ordered = ordered[:limit]

    # Restore document order among the survivors.
    kept = {id(r) for r in ordered}
    final = [r for r in items if id(r) in kept]
    for index, item in enumerate(final):
        item.order_index = index
        item.keywords = extract_keywords(item.text)
    return final


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    {
        "ability",
        "able",
        "across",
        "and",
        "any",
        "are",
        "our",
        "the",
        "with",
        "for",
        "you",
        "your",
        "have",
        "will",
        "that",
        "this",
        "from",
        "into",
        "using",
        "use",
        "work",
        "working",
        "experience",
        "years",
        "year",
        "strong",
        "excellent",
        "good",
        "team",
        "teams",
        "such",
        "role",
        "including",
        "etc",
        "skills",
        "skill",
        "knowledge",
        "understanding",
        "familiarity",
        "plus",
        "preferred",
        "required",
        "requirements",
        "who",
        "what",
        "how",
        "when",
        "where",
        "must",
        "should",
        "can",
        "would",
        "could",
        "their",
        "them",
        "they",
        "she",
        "him",
        "her",
        "his",
        "its",
        "was",
        "were",
        "been",
        "being",
        "has",
        "had",
        "does",
        "did",
        "not",
        "but",
        "all",
        "some",
        "more",
        "most",
        "other",
        "than",
        "then",
        "also",
        "well",
        "very",
        "well-versed",
        "candidate",
        "candidates",
        "applicant",
        "applicants",
        "opportunity",
        "position",
        "job",
        "company",
        "organization",
        "please",
        "apply",
        "new",
        "great",
        "help",
        "make",
        "within",
        "about",
        "over",
        "under",
        "between",
        "each",
        "both",
        "may",
        "one",
        "two",
    }
)


def extract_keywords(text: str, *, limit: int = 25) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}", text)
    out: list[str] = []
    for token in tokens:
        low = token.lower().strip(".-")
        if low in _STOPWORDS or len(low) < 2:
            continue
        if low not in out:
            out.append(low)
    return out[:limit]
