"""Drafting an outreach message. Never sending one.

There is no transport in this file and there is none downstream of it. The
system produces text, stores it, and reminds the user about it; the send happens in
his own inbox or his own LinkedIn session, and he tells the system afterwards.

**Nothing here is invented.** The failure mode a networking assistant falls into
is flattery it cannot support — a shared interest nobody mentioned, a project
the contact never worked on, a mutual connection that does not exist, a claim of
having followed someone's work. Each of those is a small lie that a real person
can catch in one reply, and it costs the relationship the message was meant to
start. So every substituted fact comes from exactly one of four places:

* the CONTACT record, as entered by the user;
* the JOB, as collected;
* the Evidence Bank, through the same safe-claim gate a resume goes through;
* the candidate's own profile in ``candidate.yaml``.

A shared school affiliation is used only when the contact is recorded
as ``BU_ALUM`` — a fact the user entered, not an inference from a profile. When
there is little to go on, the draft says little: an honest cold message is a
better outcome than a warm-sounding false one.

Every draft is editable before it goes anywhere, because the person sending it
is the one accountable for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.evidence.service import EvidenceBankService, get_evidence_bank
from app.models.enums import ContactType
from app.models.ingestion import Job
from app.models.misc import Contact
from app.services.candidate_profile import alma_mater
from app.services.role_targeting import meaningful_tokens

LINKEDIN = "LINKEDIN"
EMAIL = "EMAIL"

#: LinkedIn caps a connection note at 300 characters. A draft that silently
#: exceeds it gets truncated where the platform decides, usually mid-sentence.
LINKEDIN_NOTE_LIMIT = 300

@dataclass
class OutreachDraft:
    channel: str
    subject: str | None
    body: str
    #: Evidence IDs behind any specific claim in the body. Stored with the
    #: interaction so a message can be traced back the way a resume line can.
    evidence_ids: list[str] = field(default_factory=list)
    safe_claim_ids: list[str] = field(default_factory=list)
    #: What the draft deliberately did NOT say, and why. Shown to the user so a
    #: thin message reads as a decision rather than a failure.
    omissions: list[str] = field(default_factory=list)
    over_limit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "subject": self.subject,
            "body": self.body,
            "evidence_ids": self.evidence_ids,
            "safe_claim_ids": self.safe_claim_ids,
            "omissions": self.omissions,
            "over_limit": self.over_limit,
            "character_count": len(self.body),
        }


class OutreachDraftService:
    """Composes a draft from recorded facts. Sends nothing, ever."""

    def __init__(self, bank: EvidenceBankService | None = None) -> None:
        self.bank = bank or get_evidence_bank()

    def draft(
        self, *, contact: Contact, job: Job | None = None, channel: str = LINKEDIN
    ) -> OutreachDraft:
        channel = (channel or LINKEDIN).upper()
        if channel not in (LINKEDIN, EMAIL):
            channel = LINKEDIN
        return self._linkedin(contact, job) if channel == LINKEDIN else self._email(contact, job)

    # -- LinkedIn --------------------------------------------------------
    def _linkedin(self, contact: Contact, job: Job | None) -> OutreachDraft:
        """Short by construction. A connection note has 300 characters.

        Long job titles blow that budget quickly — "Senior Staff Machine
        Learning Engineer, Platform Infrastructure" is 62 of them on its own —
        so the draft is built as a ladder of increasingly compact variants and
        the longest one that FITS is used. Nothing is cut mid-word: LinkedIn
        would do that itself, wherever the character happens to land, and a
        message that ends mid-sentence reads as carelessness.
        """
        omissions: list[str] = []
        first = _first_name(contact.name)
        me = self._my_name()
        opener = self._opener(contact, first, omissions)
        ask = self._ask(contact, job)
        signature = f"\n\n— {me}"

        variants = [
            (self._role_line(contact, job), None),
            (_shorten(self._role_line(contact, job)), None),
            (self._minimal_role_line(job), "Dropped their title to fit the character limit."),
            ("", "Dropped the role sentence entirely to fit the character limit."),
        ]

        body = ""
        note: str | None = None
        for role_line, why in variants:
            candidate = " ".join(part for part in (opener, role_line, ask) if part) + signature
            body, note = candidate, why
            if len(candidate) <= LINKEDIN_NOTE_LIMIT:
                break

        if note:
            omissions.append(f"{note} The email draft has room for the detail.")
        over = len(body) > LINKEDIN_NOTE_LIMIT
        if over:
            omissions.append(
                f"Still {len(body) - LINKEDIN_NOTE_LIMIT} characters over LinkedIn's limit of "
                f"{LINKEDIN_NOTE_LIMIT}. Trim it yourself rather than letting LinkedIn cut it "
                "mid-sentence, or send this as an email instead."
            )

        return OutreachDraft(
            channel=LINKEDIN,
            subject=None,
            body=body,
            omissions=omissions,
            over_limit=over,
        )

    def _minimal_role_line(self, job: Job | None) -> str:
        if job is None:
            return ""
        return f"I'm applying for the {job.title} role at {job.company_name}."

    # -- Email -----------------------------------------------------------
    def _email(self, contact: Contact, job: Job | None) -> OutreachDraft:
        omissions: list[str] = []
        first = _first_name(contact.name)
        me = self._my_name()
        company = (contact.company_name or (job.company_name if job else "") or "").strip()

        opener = self._opener(contact, first, omissions)
        role_line = self._role_line(contact, job)
        claim_text, claim_ids, evidence_ids = self._evidence_sentence(job)
        if not claim_text:
            omissions.append(
                "No Evidence Bank claim matched this role closely enough to cite, so the "
                "draft does not describe specific work. Better than a vague boast."
            )
        ask = self._ask(contact, job, long_form=True)

        paragraphs = [
            f"Hi {first},",
            " ".join(p for p in (opener, role_line) if p),
            claim_text,
            ask,
            f"Thanks for your time,\n{me}",
        ]
        body = "\n\n".join(p for p in paragraphs if p)

        subject = (
            f"{job.title} at {company}" if job and company else (f"{company}" if company else "Hello")
        )
        if job and company:
            subject = f"Question about the {job.title} role at {company}"

        return OutreachDraft(
            channel=EMAIL,
            subject=subject,
            body=body,
            evidence_ids=evidence_ids,
            safe_claim_ids=claim_ids,
            omissions=omissions,
        )

    # -- parts -----------------------------------------------------------
    def _opener(self, contact: Contact, first: str, omissions: list[str]) -> str:
        """The one line most likely to become a lie. It stays on recorded facts."""
        if contact.relationship_type == ContactType.BU_ALUM.value:
            return f"I'm a {alma_mater(self.bank)} MS Data Science student, and saw you're a fellow alum."
        if contact.relationship_type == ContactType.FORMER_COLLEAGUE.value:
            return "We worked together previously, and I wanted to reach out."
        if contact.relationship_type == ContactType.PROFESSOR.value:
            return f"I'm one of your students at {alma_mater(self.bank)}."
        if contact.relationship_type == ContactType.RECRUITER.value:
            return f"I'm an MS Data Science student at {alma_mater(self.bank)} finishing in January 2027."

        if contact.shared_context:
            # Entered by the user, so it is a fact on file rather than an inference.
            return f"I'm an MS Data Science student at {alma_mater(self.bank)}. {contact.shared_context.strip()}"

        omissions.append(
            "No shared context is recorded for this person, so the draft opens as an "
            "honest cold message. Add a real connection to the contact if one exists."
        )
        return f"I'm an MS Data Science student at {alma_mater(self.bank)} finishing in January 2027."

    def _role_line(self, contact: Contact, job: Job | None) -> str:
        if job is None:
            company = (contact.company_name or "").strip()
            return f"I've been following the data work at {company}." if company else ""
        company = (job.company_name or "").strip()
        if contact.role:
            return f"You're working as {contact.role} at {company}, and I'm applying for the {job.title} role there."
        return f"I'm applying for the {job.title} role at {company}."

    def _evidence_sentence(self, job: Job | None) -> tuple[str, list[str], list[str]]:
        """One concrete thing the candidate has actually done, relevant to this role.

        Drawn through ``usable_claim_text``, which is the same ownership gate a
        resume bullet passes: a gated claim renders as its project-level
        alternative or not at all. An outreach message is a claim about the
        candidate exactly like a resume line, so it obeys the same rule.
        """
        if job is None:
            return "", [], []
        family = (job.role_family or "").strip()
        candidates = [
            claim
            for claim in self.bank.safe_claims.values()
            if not family or family in claim.role_families
        ]
        if not candidates:
            # The classifier put this posting in a family no claim carries —
            # a real "Data Engineering Intern" lands in OTHER_ADJACENT today.
            # Ranking the whole cleared set against the posting is a better
            # answer than saying nothing, and weakens nothing: every candidate
            # still passes the same ownership gate, and relevance still decides.
            candidates = list(self.bank.safe_claims.values())
        if not candidates:
            return "", [], []

        # RANKING ONLY, over claims the Bank already cleared. Without it the
        # role family alone decided, and a finance data-science posting drew a
        # smart-camera app-usage bullet: true, cleared, and irrelevant.
        posting = meaningful_tokens(f"{job.title} {job.description or ''}")
        strength_rank = {"STRONG": 0, "SOLID": 1, "MODEST": 2}

        def relevance(claim: Any) -> tuple[int, int, str]:
            overlap = len(meaningful_tokens(claim.text) & posting)
            return (-overlap, strength_rank.get(claim.strength, 3), claim.claim_id)

        ordered = sorted(candidates, key=relevance)
        for claim in ordered:
            text, _downgraded = self.bank.usable_claim_text(claim.claim_id)
            if not text:
                continue
            sentence = text.strip()
            if not sentence.endswith("."):
                sentence += "."
            return (
                f"Most relevant to that team: {sentence[0].lower() + sentence[1:]}",
                [claim.claim_id],
                list(claim.evidence_ids),
            )
        return "", [], []

    def _ask(self, contact: Contact, job: Job | None, *, long_form: bool = False) -> str:
        """One clear, small request. Never a request for a referral outright.

        Asking a stranger to stake their name on you in the first message is
        how the message gets ignored. A referral conversation is tracked
        separately, and starts after a reply.
        """
        if contact.relationship_type == ContactType.RECRUITER.value:
            return (
                "Would you be open to a quick note on whether this req is still active "
                "and what the team is prioritising?"
                if long_form
                else "Is this req still active?"
            )
        if long_form:
            return (
                "Would you have 15 minutes in the next couple of weeks to tell me what "
                "the team actually works on? I'd rather hear it from someone doing the "
                "work than guess from the posting."
            )
        return "Would you be open to a short conversation about the team?"

    def _my_name(self) -> str:
        return str(self.bank.candidate.get("name") or "").strip()


def _first_name(name: str) -> str:
    parts = (name or "").strip().split()
    return parts[0] if parts else "there"


def _shorten(text: str) -> str:
    """First sentence only. Used when a note has to fit inside a platform cap."""
    head, _, _rest = text.partition(". ")
    return head + "." if head and not head.endswith(".") else head
