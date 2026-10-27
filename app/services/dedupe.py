"""Deduplication and canonical resolution.

Source provenance is **never** destroyed by a merge. One normalized ``Job`` may
carry many ``JobSourceAssociation`` rows — the LinkedIn-discovery +
Greenhouse-canonical case is the design target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import load_yaml_config
from app.models.enums import CanonicalVerification, DecidedBy, DuplicateVerdict
from app.models.ingestion import DedupeDecision, Job
from app.services.text import (
    canonicalize_url,
    description_fingerprint,
    hamming_distance,
    normalize_company,
    normalize_title,
)

DEFAULT_FINGERPRINT_THRESHOLD = 3
DEFAULT_REPOST_GAP_DAYS = 14


@dataclass
class DedupeCandidate:
    job: Job
    verdict: DuplicateVerdict
    confidence: float
    signals: dict[str, Any] = field(default_factory=dict)


def _cfg() -> dict[str, Any]:
    return load_yaml_config("screening_rules.yaml").get("deduplication", {})


def pair_key(job_id_a: str, job_id_b: str) -> str:
    return "::".join(sorted([job_id_a, job_id_b]))


class DeduplicationService:
    """Multi-signal dedupe with four verdicts and permanent user overrides."""

    def __init__(self, session: Session) -> None:
        self.session = session
        cfg = _cfg()
        self.fingerprint_threshold = int(
            cfg.get("fingerprint_hamming_threshold", DEFAULT_FINGERPRINT_THRESHOLD)
        )
        self.repost_gap_days = int(cfg.get("repost_gap_days", DEFAULT_REPOST_GAP_DAYS))

    def find_duplicate(
        self,
        *,
        company_name: str,
        title: str,
        location: str | None,
        canonical_url: str | None,
        ats_platform: str | None = None,
        ats_org_slug: str | None = None,
        ats_job_id: str | None = None,
        connector: str | None = None,
        source_external_id: str | None = None,
        description: str = "",
        exclude_job_id: str | None = None,
    ) -> DedupeCandidate | None:
        """Return the best duplicate candidate, or None."""
        norm_company = normalize_company(company_name)
        norm_title = normalize_title(title)
        canon = canonicalize_url(canonical_url)
        fingerprint = description_fingerprint(description) if description else ""

        # T1 — exact source identity. Idempotency key.
        if connector and source_external_id:
            from app.models.ingestion import JobSourceAssociation

            stmt = (
                select(Job)
                .join(JobSourceAssociation, JobSourceAssociation.job_id == Job.job_id)
                .where(
                    JobSourceAssociation.connector == connector,
                    JobSourceAssociation.source_external_id == source_external_id,
                )
            )
            job = self.session.execute(stmt).scalars().first()
            if job and job.job_id != exclude_job_id:
                return self._decide(
                    job, DuplicateVerdict.SAME_JOB, 1.0, {"tier": "T1_SOURCE_EXTERNAL_ID"}
                )

        # T2 — canonical URL.
        if canon:
            job = (
                self.session.execute(select(Job).where(Job.canonical_url == canon))
                .scalars()
                .first()
            )
            if job and job.job_id != exclude_job_id:
                return self._decide(
                    job, DuplicateVerdict.SAME_JOB, 0.95, {"tier": "T2_CANONICAL_URL"}
                )

        # T3 — ATS identity. The Channel A/B merge case.
        if ats_platform and ats_org_slug and ats_job_id:
            job = (
                self.session.execute(
                    select(Job).where(
                        Job.ats_platform == ats_platform,
                        Job.ats_org_slug == ats_org_slug,
                        Job.ats_job_id == ats_job_id,
                    )
                )
                .scalars()
                .first()
            )
            if job and job.job_id != exclude_job_id:
                return self._decide(
                    job, DuplicateVerdict.SAME_JOB, 0.90, {"tier": "T3_ATS_IDENTITY"}
                )

        # T4 — fuzzy: company + title (+ location) + description fingerprint.
        if not norm_company or not norm_title:
            return None

        # A source that gave this posting its own external ID has already told
        # us it is a distinct opening. Two records from the SAME connector with
        # DIFFERENT valid external IDs are different jobs, and no amount of
        # title or description similarity should override that.
        #
        # Without this guard, Whoop's Ashby board merged twelve postings with
        # twelve distinct UUIDs into one job carrying eleven versions: many of
        # their roles share a title stem and near-identical boilerplate bodies,
        # which is exactly what fuzzy matching keys on.
        #
        # Fuzzy dedupe is NOT disabled globally — it still runs whenever source
        # identity is unavailable, which is the cross-source case it exists for.
        sibling_ids: set[str] = set()
        if connector and source_external_id:
            from app.models.ingestion import JobSourceAssociation

            rows = self.session.execute(
                select(JobSourceAssociation.job_id, JobSourceAssociation.source_external_id).where(
                    JobSourceAssociation.connector == connector,
                    JobSourceAssociation.source_external_id.is_not(None),
                    JobSourceAssociation.source_external_id != source_external_id,
                )
            ).all()
            sibling_ids = {job_id for job_id, _ in rows}

        stmt = select(Job).where(
            Job.normalized_company == norm_company, Job.normalized_title == norm_title
        )
        for job in self.session.execute(stmt).scalars().all():
            if job.job_id == exclude_job_id:
                continue
            if job.job_id in sibling_ids:
                # Same connector, different external ID — the source considers
                # these separate postings, so we do too.
                continue

            # An employer does not list one opening twice under two different
            # ids on its own board. If this record and the candidate both come
            # from the same connector but carry DIFFERENT source external ids,
            # they are different postings and fuzzy similarity must not
            # override that.
            #
            # Found in the first real collection: WHOOP publishes many roles
            # with near-identical boilerplate bodies and titles that normalize
            # to the same string ("Software Engineer II (Fullstack, Business
            # Systems)" vs "(Backend, ...)"). Twelve distinct Ashby UUIDs
            # merged into one job. Source identity is stronger evidence than
            # any text similarity score.
            if (
                connector
                and source_external_id
                and _has_conflicting_source_id(self.session, job, connector, source_external_id)
            ):
                continue
            signals: dict[str, Any] = {"tier": "T4_FUZZY"}
            confidence = 0.6

            same_location = _same_location(job.normalized_location, location)
            signals["same_location"] = same_location
            if same_location:
                confidence += 0.1

            if fingerprint and job.description_fingerprint:
                distance = hamming_distance(fingerprint, job.description_fingerprint)
                signals["fingerprint_distance"] = distance
                if distance <= self.fingerprint_threshold:
                    confidence += 0.2
                elif distance > 16:
                    confidence -= 0.2

            # Repost: prior listing closed/disappeared with a real gap.
            gap_marker = job.disappeared_at or job.closed_at
            if gap_marker is not None:
                from app.models.base import utcnow

                if utcnow() - gap_marker > timedelta(days=self.repost_gap_days):
                    signals["repost_gap_days"] = (utcnow() - gap_marker).days
                    return self._decide(job, DuplicateVerdict.REPOST, confidence, signals)

            if not same_location and location and job.normalized_location:
                return self._decide(
                    job, DuplicateVerdict.SIMILAR_DISTINCT_ROLE, confidence, signals
                )

            verdict = (
                DuplicateVerdict.SAME_JOB
                if confidence >= 0.85
                else DuplicateVerdict.LIKELY_DUPLICATE
            )
            return self._decide(job, verdict, min(confidence, 0.99), signals)

        return None

    def _decide(
        self, job: Job, verdict: DuplicateVerdict, confidence: float, signals: dict[str, Any]
    ) -> DedupeCandidate:
        """Apply any permanent user override before returning a verdict."""
        override = (
            self.session.execute(
                select(DedupeDecision).where(
                    DedupeDecision.job_id_a == job.job_id,
                    DedupeDecision.decided_by == DecidedBy.USER.value,
                )
            )
            .scalars()
            .first()
        )
        if override is not None:
            signals["user_override"] = override.verdict
            return DedupeCandidate(
                job=job, verdict=DuplicateVerdict(override.verdict), confidence=1.0, signals=signals
            )
        return DedupeCandidate(job=job, verdict=verdict, confidence=confidence, signals=signals)

    def record_decision(
        self,
        job_a: str,
        job_b: str,
        verdict: DuplicateVerdict,
        *,
        decided_by: DecidedBy = DecidedBy.SYSTEM,
        confidence: float | None = None,
        signals: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> DedupeDecision:
        key = pair_key(job_a, job_b)
        existing = (
            self.session.execute(select(DedupeDecision).where(DedupeDecision.pair_key == key))
            .scalars()
            .first()
        )
        # A USER verdict is permanent — never overwritten by the system.
        if existing is not None:
            if existing.decided_by == DecidedBy.USER.value and decided_by != DecidedBy.USER:
                return existing
            existing.verdict = verdict.value
            existing.decided_by = decided_by.value
            existing.confidence = confidence
            existing.signals = signals
            existing.note = note
            return existing

        decision = DedupeDecision(
            pair_key=key,
            job_id_a=job_a,
            job_id_b=job_b,
            verdict=verdict.value,
            decided_by=decided_by.value,
            confidence=confidence,
            signals=signals,
            note=note,
        )
        self.session.add(decision)
        return decision


def _same_location(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    na, nb = a.strip().lower(), normalize_title(b) if b else ""
    nb = b.strip().lower()
    if na == nb:
        return True
    a_tokens = set(re.findall(r"[a-z]+", na))
    b_tokens = set(re.findall(r"[a-z]+", nb))
    if not a_tokens or not b_tokens:
        return False
    return len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens)) >= 0.6


# ---------------------------------------------------------------------------
# Canonical resolution
# ---------------------------------------------------------------------------

ATS_URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("GREENHOUSE", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([^/]+)/jobs/(\d+)", re.I)),
    ("GREENHOUSE", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([^/]+)/jobs/(\d+)", re.I)),
    ("LEVER", re.compile(r"jobs\.lever\.co/([^/]+)/([0-9a-f-]{8,})", re.I)),
    # Ashby posting ids are normally UUIDs, but the board also serves
    # slug-style ids. Matching only hex would quietly drop canonical status
    # for those, which understates provenance rather than failing loudly.
    ("ASHBY", re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-zA-Z_-]{8,})", re.I)),
    ("SMARTRECRUITERS", re.compile(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)", re.I)),
    (
        "WORKDAY",
        re.compile(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com/.*?/job/[^/]+/([^/?]+)", re.I),
    ),
    # The other Workday host shape: the tenant is in the path, not the host.
    (
        "WORKDAY",
        re.compile(
            r"wd\d+\.myworkdaysite\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?recruiting/([^/]+)/[^/]+"
            r"/job/[^/]+/([^/?#]+)",
            re.I,
        ),
    ),
]


@dataclass
class CanonicalHint:
    ats_platform: str | None = None
    ats_org_slug: str | None = None
    ats_job_id: str | None = None
    canonical_url: str | None = None
    verification: CanonicalVerification = CanonicalVerification.UNKNOWN


class CanonicalJobResolver:
    """Identifies the employer/ATS posting behind a discovered job URL.

    Slice 1 resolves from URL shape only — no network calls. Slice 3/4 add
    fetching the canonical record through the matching direct connector.
    """

    #: Connectors whose URL came from the user rather than from a crawl. A
    #: pasted Greenhouse link IS the canonical employer posting — the importer
    #: fetched it from Greenhouse's own API — so it verifies the same way a
    #: direct Greenhouse connector record does.
    USER_SUPPLIED = frozenset({"manual", "user_url"})

    def resolve(self, url: str | None, *, discovery_connector: str = "manual") -> CanonicalHint:
        if not url:
            return CanonicalHint(verification=CanonicalVerification.UNKNOWN)

        canon = canonicalize_url(url)
        for platform, pattern in ATS_URL_PATTERNS:
            match = pattern.search(url)
            if match:
                return CanonicalHint(
                    ats_platform=platform,
                    ats_org_slug=match.group(1),
                    ats_job_id=match.group(2),
                    canonical_url=canon,
                    verification=(
                        CanonicalVerification.CANONICAL_VERIFIED
                        if discovery_connector in self.USER_SUPPLIED
                        or discovery_connector == platform.lower()
                        else CanonicalVerification.SOURCE_ONLY
                    ),
                )

        return CanonicalHint(
            canonical_url=canon,
            verification=(
                CanonicalVerification.UNKNOWN
                if discovery_connector in self.USER_SUPPLIED
                else CanonicalVerification.SOURCE_ONLY
            ),
        )


def _has_conflicting_source_id(
    session: Session, job: Job, connector: str, source_external_id: str
) -> bool:
    """True when ``job`` is already known to this connector under a different id.

    Source identity outranks textual similarity. If the same board already gave
    us this job under another id, a new id means a new posting — not a duplicate
    to merge into it.
    """
    from app.models.ingestion import JobSourceAssociation

    rows = (
        session.execute(
            select(JobSourceAssociation).where(
                JobSourceAssociation.job_id == job.job_id,
                JobSourceAssociation.connector == connector,
                JobSourceAssociation.source_external_id.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    return any(row.source_external_id != source_external_id for row in rows)
