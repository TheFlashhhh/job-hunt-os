"""Composing a cover letter from evidence, and from nothing else.

A cover letter is the document where fabrication is easiest and most tempting.
It is prose, it is expected to sound enthusiastic, and every phrase a generator
reaches for — "I've long admired your work", "your culture resonates with me",
"I've followed the team closely" — is a claim about the candidate that no evidence
supports and that a single interview question exposes.

So this obeys the same architecture as the resume, not a looser version of it:

**Sentences about the candidate are safe claims, verbatim.** They come through
``usable_claim_text``, which is the ownership gate — a gated claim renders as
its project-level alternative or not at all. The generator writes CONNECTIVE
TISSUE ("Most relevant to this role:") and never writes an achievement.

**The job description is not evidence.** It decides which of the candidate's real
material leads, exactly as ``role_targeting`` does for a resume. A posting
asking for Tableau cannot put Tableau in a letter, because there is no path
from the posting's words into a sentence about the candidate.

**"Why this company" comes from the posting.** The letter may say what the ROLE
is about, because the posting says so. It may not say anything about admiring,
following, using or having always wanted to work at the company.

**No recipient is invented, no referral is implied, and work authorisation is
never volunteered.** Each of those is opt-in, and each is recorded in the
provenance when the user opts in.

What the letter leaves out, and why, is reported alongside it. A thin letter
with a stated reason is a better artifact than a padded one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.models.ingestion import Job
from app.services.candidate_profile import CandidateProfile, load_candidate_profile
from app.services.role_targeting import RoleTargetProfile, meaningful_tokens

GENERATOR_VERSION = "1.0.0"

#: Two to three stories. One reads thin; four turns the letter into a
#: worse-formatted resume, which is the most common way these go wrong.
MIN_STORIES = 2
MAX_STORIES = 3

#: The band the letter aims for. Not enforced by truncation — a letter is cut
#: by dropping a whole story, never mid-sentence.
TARGET_WORDS_MIN = 250
TARGET_WORDS_MAX = 400

#: Openers that would be lies. Asserted against in tests, and listed here so the
#: prohibition is visible next to the code that could otherwise produce it.
PROHIBITED_FLATTERY = (
    "i have admired",
    "i've admired",
    "i have followed",
    "i've followed",
    "i have always dreamed",
    "i've always dreamed",
    "your culture",
    "resonates with me",
    "i have used your product",
    "i've used your product",
    "i recently spoke with",
    "long-time fan",
    "biggest fan",
)


@dataclass
class EvidenceStory:
    """One paragraph's worth of real work, and why it was chosen."""

    owner: str
    claim_ids: list[str]
    evidence_ids: list[str]
    sentences: list[str]
    #: True when the FIRST sentence came back as a project-level alternative.
    #: The ownership gate's own signal, and what decides whether the paragraph
    #: may say "I" — see `_story_paragraph`.
    ownership_downgraded: bool = False
    #: True when this entry is paid work rather than a project.
    is_experience: bool = False
    #: Ranked, cleared claims this story did not use. Drawn on only to reach the
    #: target length — never to pad, because everything here is already true and
    #: already relevant.
    spare: list[tuple[str, str, list[str]]] = field(default_factory=list)
    #: Plain-language match reason for the "why these examples?" panel. Never
    #: rendered into the letter.
    why: str = ""

    @property
    def text(self) -> str:
        return " ".join(self.sentences)


@dataclass
class CoverLetter:
    salutation: str
    paragraphs: list[str]
    stories: list[EvidenceStory] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    safe_claim_ids: list[str] = field(default_factory=list)
    omissions: list[str] = field(default_factory=list)
    role_archetype: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def body(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    def as_dict(self) -> dict[str, Any]:
        return {
            "salutation": self.salutation,
            "body": self.body,
            "paragraphs": self.paragraphs,
            "word_count": self.word_count,
            "role_archetype": self.role_archetype,
            "evidence_ids": self.evidence_ids,
            "safe_claim_ids": self.safe_claim_ids,
            "omissions": self.omissions,
            "why_these_examples": [
                {"example": s.owner, "why": s.why, "evidence_ids": s.evidence_ids}
                for s in self.stories
            ],
            "provenance": self.provenance,
        }


class CoverLetterService:
    """Selects evidence and assembles paragraphs. Writes no achievements."""

    def __init__(self, bank: EvidenceBankService | None = None) -> None:
        self.bank = bank or get_evidence_bank()

    def generate(
        self,
        *,
        job: Job,
        target: RoleTargetProfile | None = None,
        recipient: str | None = None,
        referral_contact: str | None = None,
        include_work_authorization: bool = False,
        work_authorization_text: str | None = None,
        extra_context: str | None = None,
    ) -> CoverLetter:
        profile = load_candidate_profile(self.bank)
        omissions: list[str] = []

        salutation = self._salutation(job, recipient, omissions)
        stories = self._select_stories(job, target, omissions)

        paragraphs = [
            self._opening(
                job, target, profile, stories, referral_contact, extra_context, omissions
            ),
            *[self._story_paragraph(story, index) for index, story in enumerate(stories)],
            self._closing(job),
        ]
        if include_work_authorization:
            # Opt-in only, and the user approves the exact wording. Nothing here
            # composes a claim about status on its own.
            approved = (work_authorization_text or "").strip()
            if approved:
                paragraphs.insert(len(paragraphs) - 1, approved)
                omissions.append(
                    "Work authorisation wording was included because you asked for it, "
                    "in exactly the words you supplied."
                )
        else:
            omissions.append(
                "No work-authorisation or visa wording is included. Add it only if the "
                "employer asks."
            )

        paragraphs = [p for p in paragraphs if p]
        letter = CoverLetter(
            salutation=salutation,
            paragraphs=paragraphs,
            stories=stories,
            evidence_ids=sorted({e for s in stories for e in s.evidence_ids}),
            safe_claim_ids=[c for s in stories for c in s.claim_ids],
            omissions=omissions,
            role_archetype=target.role_archetype if target else "",
            provenance={
                "generator_version": GENERATOR_VERSION,
                "evidence_bank_version": self.bank.version,
                "role_archetype": target.role_archetype if target else None,
                "resume_base": target.resume_base if target else None,
                "recipient_supplied": bool(recipient),
                "referral_named": bool(referral_contact),
                "work_authorization_included": bool(include_work_authorization),
                "extra_context_supplied": bool(extra_context),
                "generated_on": date.today().isoformat(),
            },
        )
        self._extend_to_band(letter)
        self._trim_to_band(letter)
        return letter

    # -- salutation ------------------------------------------------------
    def _salutation(self, job: Job, recipient: str | None, omissions: list[str]) -> str:
        """Never invent a name.

        A wrong name is worse than no name, and a generator with a company name
        and a job title has everything it needs to guess one convincingly.
        """
        name = (recipient or "").strip()
        if name:
            return f"Dear {name},"
        company = (job.company_name or "").strip()
        omissions.append(
            "Addressed to the hiring team. No hiring manager is recorded for this job, "
            "and a guessed name is worse than none."
        )
        return f"Dear {company} Hiring Team," if company else "Dear Hiring Team,"

    # -- evidence --------------------------------------------------------
    def _select_stories(
        self, job: Job, target: RoleTargetProfile | None, omissions: list[str]
    ) -> list[EvidenceStory]:
        """Two or three entries whose claims best answer THIS posting.

        Ranked the same way a resume ranks bullets: by overlap with the
        posting's own vocabulary, over claims the Evidence Bank already
        cleared. Selection cannot reach anything the Bank has not cleared, so
        the worst case is a less relevant true story.
        """
        from app.services.resume_base import ResumeBaseBuilder

        builder = ResumeBaseBuilder(self.bank)
        posting = meaningful_tokens(f"{job.title} {job.description or ''}")
        required = set(target.required_tokens) if target else set()
        families = set(target.claim_role_families) if target else set()

        # Education is not a story. Its claims are owned by the institution, so
        # grouping by owner put the university forward as an example and
        # rendered "On <University>, I graduate coursework in Deep
        # Learning…" — ungrammatical, and a restatement of the opening, which
        # already names the degree. Coursework in prose to match keywords is
        # exactly what a cover letter should not do.
        institutions = {
            str(e.get("institution") or "").strip()
            for e in self.bank.education
            if e.get("institution")
        }

        by_owner: dict[str, list[Any]] = {}
        for claim in self.bank.safe_claims.values():
            owner = builder._claim_owner(claim.claim_id)
            if not owner or owner in institutions:
                continue
            text, downgraded = self.bank.usable_claim_text(claim.claim_id)
            if not text:
                # Gated with no permitted wording. There is no sentence the
                # ownership rule allows, so there is no sentence.
                continue
            by_owner.setdefault(owner, []).append((claim, text, downgraded))

        scored: list[tuple[float, str, list[Any]]] = []
        for owner, claims in by_owner.items():
            ranked = sorted(
                claims,
                key=lambda row: -self._claim_score(row[1], row[0], posting, required, families),
            )
            top = ranked[:2]
            total = sum(
                self._claim_score(text, claim, posting, required, families)
                for claim, text, _ in top
            )
            scored.append((total, owner, top))

        scored.sort(key=lambda row: -row[0])
        chosen = [row for row in scored if row[0] > 0][:MAX_STORIES]
        if len(chosen) < MIN_STORIES:
            chosen = scored[:MIN_STORIES]

        experience_names = {
            str(e.get("organization") or e.get("company") or "").strip()
            for e in self.bank.experiences.values()
        }
        stories = [
            EvidenceStory(
                owner=owner,
                claim_ids=[c.claim_id for c, _, _ in claims],
                evidence_ids=sorted({e for c, _, _ in claims for e in c.evidence_ids}),
                sentences=[self._as_sentence(text) for _, text, _ in claims],
                ownership_downgraded=bool(claims[0][2]),
                is_experience=owner in experience_names,
                spare=[
                    (c.claim_id, self._as_sentence(text), list(c.evidence_ids))
                    for c, text, _ in by_owner[owner]
                    if c.claim_id not in {used.claim_id for used, _, _ in claims}
                ][:2],
                why=self._why(claims, posting, required),
            )
            for _score, owner, claims in chosen
            if claims
        ]
        if len(stories) < MIN_STORIES:
            omissions.append(
                "Fewer examples than usual: the Evidence Bank did not hold enough "
                "cleared, relevant claims for this posting."
            )
        return stories

    def _claim_score(
        self,
        text: str,
        claim: Any,
        posting: set[str],
        required: set[str],
        families: set[str],
    ) -> float:
        """Relevance to this posting. Ranking only — it clears nothing."""
        tokens = meaningful_tokens(text)
        score = float(len(tokens & posting))
        # A requirement the employer stated is worth more than incidental prose.
        score += 2.0 * len(tokens & required)
        if families and set(claim.role_families) & families:
            score += 2.0
        score += {"STRONG": 1.0, "SOLID": 0.5}.get(claim.strength, 0.0)
        return score

    def _why(self, claims: list[Any], posting: set[str], required: set[str]) -> str:
        """Plain language for the internal panel. Never rendered into the letter."""
        overlap: set[str] = set()
        for _claim, text, _downgraded in claims:
            tokens = meaningful_tokens(text)
            overlap |= tokens & (required or posting)
        terms = sorted(overlap, key=len, reverse=True)[:4]
        return "Matched " + ", ".join(terms) if terms else "Closest available match"

    # -- paragraphs ------------------------------------------------------
    def _opening(
        self,
        job: Job,
        target: RoleTargetProfile | None,
        profile: CandidateProfile,
        stories: list[EvidenceStory],
        referral_contact: str | None,
        extra_context: str | None,
        omissions: list[str],
    ) -> str:
        """Role, fit, and nothing about admiring the company.

        The only company-specific statement is what the POSTING says the role
        is about, which is quotable because the employer wrote it.
        """
        degree = self._degree_phrase()
        role = (job.title or "the role").strip()
        company = (job.company_name or "your team").strip()

        opening = f"I'm applying for the {role} position at {company}. {degree}"

        focus = self._role_focus(target, stories)
        if focus:
            opening += (
                f" The posting's emphasis on {focus} lines up closely with what I have "
                f"actually been building."
            )
        else:
            omissions.append(
                "The opening does not restate the posting's priorities: none of them is "
                "both emphasised there and demonstrated by the evidence selected here."
            )

        if referral_contact:
            # Only reached when the caller confirmed a REFERRED state and asked
            # for the name. Never inferred from a contact merely replying.
            opening += f" {referral_contact.strip()} suggested I apply."

        if extra_context:
            opening += f" {extra_context.strip()}"
        return opening

    def _story_paragraph(self, story: EvidenceStory, index: int) -> str:
        """Connective tissue plus verbatim safe claims.

        Two rules, both learned from getting it wrong:

        **No temporal claim.** An earlier draft opened the second paragraph with
        "More recently, on OpenText" — and OpenText is the OLDER of the two. The
        generator does not know the ordering, so it does not assert one.

        **"I" only where the ownership gate allows it.** A claim that came back
        DOWNGRADED is a project-level alternative: "The project modelled…".
        Prefixing "I" to that would convert a deliberately project-level
        statement into a first-person one, which is exactly what
        `project_level_alternative` exists to prevent. Those sentences stand on
        their own, unmodified.
        """
        preposition = "At" if story.is_experience else "On"
        # Prose form of the entry's name. "Delta3 - Patient-Specific Treatment
        # Effects" is the canonical name and belongs on a resume line; mid
        # sentence it reads as a filename. The part before the dash is the same
        # name, shortened — never a different one.
        name = _prose_name(story.owner)
        sentences = list(story.sentences)
        if not sentences:
            return ""

        first, rest = sentences[0], " ".join(sentences[1:])
        if story.ownership_downgraded:
            # Project-level wording. Left exactly as the Bank permits it.
            body = f"{preposition} {name}: {first}"
        else:
            body = f"{preposition} {name}, I {first[0].lower() + first[1:]}"
        return f"{body} {rest}".strip()

    def _closing(self, job: Job) -> str:
        company = (job.company_name or "your team").strip()
        return (
            f"I'd welcome the chance to talk about how this experience fits what "
            f"{company} needs in this role. Thank you for your time and consideration."
        )

    # -- helpers ---------------------------------------------------------
    def _degree_phrase(self) -> str:
        education = self.bank.education
        if not education:
            return ""
        first = education[0]
        degree = str(first.get("degree") or "").strip()
        institution = str(first.get("institution") or "").strip()
        completion = str(first.get("conferral_expected") or "").strip()
        when = f", graduating {_pretty_month(completion)}" if completion else ""
        return f"I'm completing {_article(degree)} {degree} at {institution}{when}." if degree else ""

    def _role_focus(
        self, target: RoleTargetProfile | None, stories: list[EvidenceStory]
    ) -> str:
        """What the posting emphasises — restricted to things the candidate can back.

        This sentence used to name whatever the posting stressed, which put
        **"airflow"** into a letter for a Confluent role: true about the
        posting, and immediately followed by "lines up closely with what I have
        actually been building", which made it a claim about the candidate that no
        evidence supports. A JD keyword reaching an applicant-facing document
        through a sentence *about the JD* is the same leak by a longer route.

        So a term survives only if it is BOTH something the posting emphasises
        AND something the selected evidence actually demonstrates. If nothing
        survives, the sentence is dropped rather than softened.
        """
        if target is None or not stories:
            return ""

        supported = set()
        for story in stories:
            supported |= meaningful_tokens(story.text)

        # Authored competency labels read as English; raw JD tokens do not.
        # "treatment, boosting and causal" was the old output.
        candidates = [*target.primary_competencies, *target.secondary_competencies]
        posting = set(target.jd_tokens) | set(target.required_tokens)

        kept: list[str] = []
        for term in candidates:
            tokens = meaningful_tokens(term)
            # ALL of the term's meaningful words must be demonstrated, not just
            # one of them: "analytics data products" passed on the strength of
            # the word "analytics" alone, which is not the same capability.
            if not tokens or not (tokens & posting) or not tokens.issubset(supported):
                continue
            label = term.strip()
            if label and label.lower() not in {k.lower() for k in kept}:
                kept.append(label)
            if len(kept) == 3:
                break

        if not kept:
            return ""
        readable = [k if k.isupper() or "/" in k else k[0].lower() + k[1:] for k in kept]
        if len(readable) == 1:
            return readable[0]
        return ", ".join(readable[:-1]) + f" and {readable[-1]}"

    def _as_sentence(self, text: str) -> str:
        sentence = text.strip()
        if sentence and not sentence.endswith((".", "!", "?")):
            sentence += "."
        return sentence

    def _extend_to_band(self, letter: CoverLetter) -> None:
        """Add one more real sentence when the letter reads thin.

        Only ever a claim that was already selected, ranked and cleared — the
        next one down for an entry already in the letter. Nothing is invented to
        reach a word count, which is the whole point of having a floor rather
        than a generator that writes to length.
        """
        guard = 0
        while letter.word_count < TARGET_WORDS_MIN and guard < 6:
            guard += 1
            candidate = next((s for s in letter.stories if s.spare), None)
            if candidate is None:
                letter.omissions.append(
                    "The letter is shorter than the usual 250–400 words: the Evidence "
                    "Bank held nothing further that was both cleared and relevant here."
                )
                return
            claim_id, sentence, evidence_ids = candidate.spare.pop(0)
            candidate.sentences.append(sentence)
            candidate.claim_ids.append(claim_id)
            candidate.evidence_ids = sorted(set(candidate.evidence_ids) | set(evidence_ids))
            index = letter.stories.index(candidate)
            letter.paragraphs[index + 1] = self._story_paragraph(candidate, index)
            letter.evidence_ids = sorted({e for s in letter.stories for e in s.evidence_ids})
            letter.safe_claim_ids = [c for s in letter.stories for c in s.claim_ids]

    def _trim_to_band(self, letter: CoverLetter) -> None:
        """Drop a WHOLE story if the letter runs long. Never cut mid-sentence.

        A letter that ends mid-thought reads as carelessness, and the resume
        already carries the detail — losing one example costs less than losing
        the reader.
        """
        while letter.word_count > TARGET_WORDS_MAX and len(letter.stories) > MIN_STORIES:
            dropped = letter.stories.pop()
            # Story paragraphs sit between the opening and the closing.
            letter.paragraphs.pop(len(letter.stories) + 1)
            letter.evidence_ids = sorted({e for s in letter.stories for e in s.evidence_ids})
            letter.safe_claim_ids = [c for s in letter.stories for c in s.claim_ids]
            letter.omissions.append(
                f"Left out the {dropped.owner} example to keep the letter to one page."
            )


def _prose_name(name: str) -> str:
    """The short form of an entry's name, for use inside a sentence."""
    for separator in (" - ", " — ", " – ", ": "):
        if separator in name:
            return name.split(separator)[0].strip()
    return name.strip()


def _article(phrase: str) -> str:
    """"a Master of Science", "an MS". By sound, not by spelling."""
    first = (phrase or "").strip().split(" ")[0]
    if not first:
        return "a"
    # An initialism is read letter by letter, so "MS" takes "an".
    if first.isupper() and len(first) <= 4:
        return "an" if first[0].upper() in "AEFHILMNORSX" else "a"
    return "an" if first[0].lower() in "aeiou" else "a"


def _pretty_month(value: str) -> str:
    """`2027-01` → `January 2027`. Anything else is returned unchanged."""
    parts = value.split("-")
    if len(parts) != 2 or not parts[1].isdigit():
        return value
    months = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    index = int(parts[1])
    return f"{months[index - 1]} {parts[0]}" if 1 <= index <= 12 else value
