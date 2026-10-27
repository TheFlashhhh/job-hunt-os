"""The ingestion → analysis pipeline.

Order of operations, as locked in the architecture:

    raw staging → normalize → dedupe → canonical resolve → deterministic screen
    → eligibility → (LLM only if worthwhile) → evidence match → priority → CRM

Content hashing gates re-analysis: an unchanged job updates ``last_seen`` and
spends nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.evidence.service import get_evidence_bank
from app.models.analysis import (
    EligibilityAssessment,
    EvidenceMatch,
    JobAnalysis,
    JobRequirement,
    LegitimacyAssessment,
    PriorityScore,
    SponsorshipSignal,
)
from app.models.base import utcnow
from app.models.crm import Application, ApplicationStatusHistory
from app.models.enums import (
    AnalysisMode,
    ApplicationStatus,
    CollectionRunStatus,
    DiscoveryChannel,
    DuplicateVerdict,
    LegitimacyStatus,
    NormalizationStatus,
    RequirementKind,
    ScreenStatus,
    SearchLane,
    SourceCapability,
    SourceRole,
)
from app.models.ingestion import (
    CollectionRun,
    Company,
    Job,
    JobSourceAssociation,
    JobVersion,
    RawJobSourceRecord,
)
from app.services import classifiers as cls
from app.services.dedupe import CanonicalJobResolver, DeduplicationService
from app.services.eligibility import assess_eligibility, screen_job
from app.services.functional_screening import apply_to_job as apply_functional_classification
from app.services.jd_parser import parse_requirements
from app.services.matching import EvidenceMatcher, PriorityScorer
from app.services.prescreen import PRESCREEN_VERSION, PrescreenOutcome, PreScreenService
from app.services.text import (
    content_hash,
    description_fingerprint,
    normalize_company,
    normalize_title,
    sanitize_external_text,
)

#: ``Job.discovery_source`` values that came from the user, not from a crawl.
#: Stored uppercased by :meth:`JobPipeline._create_job`.
_USER_SUPPLIED_SOURCES = frozenset({"MANUAL", "MANUAL_IMPORT", "USER_URL"})

PARSER_VERSION = "0.1.0"
LLM_TASK_VERSION = "0.1.0"
SCORING_CONFIG_VERSION = "0.1"


@dataclass
class RawJobInput:
    """One job as received from any channel, before staging."""

    company_name: str
    title: str
    description: str
    location: str | None = None
    url: str | None = None
    external_id: str | None = None
    posted_date: date | None = None
    deadline: date | None = None
    connector: str = "manual"
    discovery_channel: DiscoveryChannel = DiscoveryChannel.C_MANUAL
    source_capability: SourceCapability = SourceCapability.MANUAL
    source_type: str = "MANUAL"
    apify_actor_id: str | None = None
    apify_run_id: str | None = None
    raw_payload: dict[str, Any] | None = None
    is_demo: bool = False


@dataclass
class IngestOutcome:
    job: Job | None
    raw_record: RawJobSourceRecord
    created: bool = False
    unchanged: bool = False
    merged_into_existing: bool = False
    dedupe_verdict: DuplicateVerdict | None = None
    analysis: JobAnalysis | None = None
    screen_status: ScreenStatus | None = None
    messages: list[str] = field(default_factory=list)


class JobPipeline:
    """Runs a raw job all the way to a scored, tracked opportunity."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.bank = get_evidence_bank()
        self.matcher = EvidenceMatcher(self.bank)
        self.scorer = PriorityScorer(self.bank)
        self.dedupe = DeduplicationService(session)
        self.prescreen = PreScreenService()
        self.canonical = CanonicalJobResolver()

    # -- entry point -----------------------------------------------------
    def ingest(
        self,
        payload: RawJobInput,
        *,
        collection_run: CollectionRun | None = None,
        analyze: bool = True,
        force_reanalyze: bool = False,
    ) -> IngestOutcome:
        clean_description, report = sanitize_external_text(payload.description or "")
        clean_title, _ = sanitize_external_text(payload.title or "", max_chars=500)
        clean_company, _ = sanitize_external_text(payload.company_name or "", max_chars=300)

        source_hash = content_hash(
            payload.connector, payload.external_id, clean_company, clean_title, clean_description
        )

        raw = RawJobSourceRecord(
            collection_run_id=collection_run.collection_run_id if collection_run else None,
            source_type=payload.source_type,
            discovery_channel=payload.discovery_channel.value,
            connector=payload.connector,
            source_capability=payload.source_capability.value,
            external_id=payload.external_id,
            source_url=payload.url,
            raw_payload=json.dumps(payload.raw_payload)[:200_000]
            if payload.raw_payload
            else clean_description[:200_000],
            payload_truncated=report.was_truncated,
            source_content_hash=source_hash,
            parser_version=PARSER_VERSION,
            sanitization_report=report.as_dict(),
            # Channel B provenance. Without this an Apify record could not be
            # traced back to the Actor and run that produced it.
            apify_actor_id=payload.apify_actor_id,
            apify_run_id=payload.apify_run_id,
        )
        self.session.add(raw)

        outcome = IngestOutcome(job=None, raw_record=raw)
        if report.suspected_injection:
            outcome.messages.append(
                "Sanitizer flagged instruction-like content in this posting. It was stripped of "
                "hidden characters and is treated strictly as data."
            )

        if not clean_title or not clean_company:
            raw.normalization_status = NormalizationStatus.FAILED.value
            raw.normalization_error = "Missing company or title."
            outcome.messages.append("Rejected: company and title are both required.")
            return outcome

        hint = self.canonical.resolve(payload.url, discovery_connector=payload.connector)
        raw.canonical_url = hint.canonical_url

        # -- dedupe -------------------------------------------------------
        candidate = self.dedupe.find_duplicate(
            company_name=clean_company,
            title=clean_title,
            location=payload.location,
            canonical_url=hint.canonical_url,
            ats_platform=hint.ats_platform,
            ats_org_slug=hint.ats_org_slug,
            ats_job_id=hint.ats_job_id,
            connector=payload.connector,
            source_external_id=payload.external_id,
            description=clean_description,
        )

        if candidate and candidate.verdict in (
            DuplicateVerdict.SAME_JOB,
            DuplicateVerdict.LIKELY_DUPLICATE,
        ):
            job = candidate.job
            outcome.job = job
            outcome.dedupe_verdict = candidate.verdict
            outcome.merged_into_existing = True
            self._attach_source(job, payload, raw, hint)
            self.dedupe.record_decision(
                job.job_id,
                job.job_id,
                candidate.verdict,
                confidence=candidate.confidence,
                signals=candidate.signals,
            )
            job.last_seen = utcnow()

            normalized_hash = self._normalized_hash(clean_company, clean_title, clean_description)
            stale = self._stale_analysis_versions(job)
            if job.normalized_content_hash == normalized_hash and not force_reanalyze and not stale:
                raw.normalization_status = NormalizationStatus.SKIPPED_UNCHANGED.value
                raw.job_id = job.job_id
                # The record stays — it is the trail that says this posting was
                # seen on this run — but the payload does not. Its normalized
                # content is identical to what the job and the earlier raw
                # record already hold, and re-storing it every six hours for
                # every open posting is pure database growth.
                raw.raw_payload = None
                outcome.unchanged = True
                outcome.messages.append(
                    "Content unchanged since last analysis — last_seen updated, no LLM spend."
                )
                return outcome

            if job.normalized_content_hash == normalized_hash and stale:
                # The job text did not change, but the logic that judged it did.
                # A matcher, scoring or Evidence Bank fix must reach jobs already
                # stored, otherwise the UI keeps showing a verdict the current
                # rules would not produce.
                raw.normalization_status = NormalizationStatus.NORMALIZED.value
                raw.job_id = job.job_id
                outcome.messages.append(
                    "Content unchanged, but re-analyzed because " + " and ".join(stale) + "."
                )
                if analyze:
                    outcome.analysis = self._prescreen_then_analyze(job, outcome)
                return outcome

            changed = self._record_version(job, clean_description, normalized_hash)
            outcome.messages.append(f"Content changed: {', '.join(changed) or 'minor edits'}.")
            job.description = clean_description
            job.normalized_content_hash = normalized_hash
            raw.normalization_status = NormalizationStatus.NORMALIZED.value
            raw.job_id = job.job_id
            if analyze:
                outcome.analysis = self._prescreen_then_analyze(job, outcome)
            return outcome

        # -- create a new job ---------------------------------------------
        job = self._create_job(payload, clean_company, clean_title, clean_description, hint, raw)
        outcome.job = job
        outcome.created = True
        raw.normalization_status = NormalizationStatus.NORMALIZED.value
        raw.job_id = job.job_id
        self._attach_source(job, payload, raw, hint)

        if candidate and candidate.verdict in (
            DuplicateVerdict.REPOST,
            DuplicateVerdict.SIMILAR_DISTINCT_ROLE,
        ):
            outcome.dedupe_verdict = candidate.verdict
            if candidate.verdict == DuplicateVerdict.REPOST:
                job.repost_detected = True
                job.reposted_from_job_id = candidate.job.job_id
                outcome.messages.append(
                    f"Detected as a repost of {candidate.job.job_id}; both records retained."
                )
            else:
                outcome.messages.append(
                    f"Similar to {candidate.job.job_id} but treated as a distinct opening."
                )
            self.dedupe.record_decision(
                job.job_id,
                candidate.job.job_id,
                candidate.verdict,
                confidence=candidate.confidence,
                signals=candidate.signals,
            )

        if analyze:
            outcome.analysis = self._prescreen_then_analyze(job, outcome)
        outcome.screen_status = ScreenStatus(job.screen_status)
        return outcome

    def _prescreen_then_analyze(self, job: Job, outcome: IngestOutcome) -> JobAnalysis | None:
        """The expensive-analysis boundary.

        A cheap deterministic prefilter runs first. Only PLAUSIBLE postings earn
        full analysis; UNCERTAIN ones stay reviewable but unanalyzed, and
        IRRELEVANT ones keep their provenance and stop here.

        This is what keeps a 300-posting board from costing 300 LLM calls once a
        provider is configured. While analysis is deterministic it also just
        makes collection markedly faster.
        """
        result = self.prescreen.evaluate(
            title=job.title,
            description=job.description or "",
            company_name=job.company_name,
            location_raw=job.location_raw,
        )
        job.prescreen_outcome = result.outcome.value
        job.prescreen_reasons = result.reason_codes
        job.prescreen_detail = result.detail[:2000]
        job.prescreen_version = result.version

        # Functional classification runs for EVERY ingested job, including ones
        # that stop here. It is cheap (regex over stored text), and it is what
        # the Jobs page filters on — a job with no classification is invisible
        # to the hidden-fit view even when it belongs there.
        apply_functional_classification(job)

        if not result.needs_analysis:
            # Screening still records a verdict so the job is explainable, but
            # no requirement extraction, evidence matching or scoring happens.
            job.screen_status = (
                ScreenStatus.REJECTED.value
                if result.outcome is PrescreenOutcome.IRRELEVANT
                else ScreenStatus.FLAGGED.value
            )
            job.screen_reason = f"{result.outcome.value}: {result.detail}"[:500]
            outcome.messages.append(
                f"{result.outcome.value} — full analysis skipped. {result.detail}"[:200]
            )
            return None

        return self.analyze(job)

    def _stale_analysis_versions(self, job: Job) -> list[str]:
        """Reasons the stored analysis no longer reflects the current rules.

        The content hash alone is not a sufficient cache key: it answers "did the
        posting change?", not "did our judgement of it change?". When the parser,
        the LLM task contract, the Evidence Bank or the scoring config moves
        forward, an unchanged posting still deserves a fresh verdict.
        """
        reasons: list[str] = []
        if job.parser_version != PARSER_VERSION:
            reasons.append(f"the parser moved to {PARSER_VERSION}")
        # A job the prescreen stopped never ran the LLM task, so it has no task
        # version to be stale against. Comparing None to the current version
        # made every one of them "stale" on every poll: ~13k of 15k jobs were
        # re-screened and re-staged with a full payload each sweep, which is
        # most of why the database grew 3 GB → 12 GB in five weeks. What CAN go
        # stale for such a job is the prescreen that stopped it.
        if job.llm_task_version is not None and job.llm_task_version != LLM_TASK_VERSION:
            reasons.append(f"the LLM task contract moved to {LLM_TASK_VERSION}")
        if job.prescreen_version is not None and job.prescreen_version != PRESCREEN_VERSION:
            reasons.append(f"the prescreen moved to {PRESCREEN_VERSION}")
        if job.evidence_bank_version != self.bank.version:
            reasons.append(f"the Evidence Bank moved to v{self.bank.version}")
        if job.scoring_config_version != SCORING_CONFIG_VERSION:
            reasons.append(f"the scoring config moved to v{SCORING_CONFIG_VERSION}")
        return reasons

    # -- job creation ----------------------------------------------------
    def _create_job(
        self,
        payload: RawJobInput,
        company_name: str,
        title: str,
        description: str,
        hint: Any,
        raw: RawJobSourceRecord,
    ) -> Job:
        company = self._get_or_create_company(company_name, payload.is_demo)
        location = cls.parse_location(payload.location or "", description)

        job = Job(
            company_id=company.company_id,
            company_name=company_name,
            normalized_company=normalize_company(company_name),
            title=title,
            normalized_title=normalize_title(title),
            description=description,
            location_raw=payload.location,
            normalized_location=location.normalized,
            location_country=location.country,
            location_state=location.state,
            location_city=location.city,
            canonical_url=hint.canonical_url,
            discovery_source=payload.connector.upper(),
            canonical_source=hint.ats_platform,
            canonical_verification=hint.verification.value,
            ats_platform=hint.ats_platform,
            ats_org_slug=hint.ats_org_slug,
            ats_job_id=hint.ats_job_id,
            posted_date=payload.posted_date,
            posted_date_confidence=("HIGH" if payload.posted_date else "UNKNOWN"),
            deadline=payload.deadline,
            source_content_hash=raw.source_content_hash,
            normalized_content_hash=self._normalized_hash(company_name, title, description),
            description_fingerprint=description_fingerprint(description),
            parser_version=PARSER_VERSION,
            evidence_bank_version=self.bank.version,
            scoring_config_version=SCORING_CONFIG_VERSION,
            is_demo=payload.is_demo,
        )
        self.session.add(job)
        self.session.flush()

        application = Application(
            job_id=job.job_id,
            current_status=ApplicationStatus.DISCOVERED.value,
            is_demo=payload.is_demo,
        )
        self.session.add(application)
        self.session.flush()
        self.session.add(
            ApplicationStatusHistory(
                application_id=application.application_id,
                from_status=None,
                to_status=ApplicationStatus.DISCOVERED.value,
                changed_by="SYSTEM",
                note=f"Discovered via {payload.connector}.",
            )
        )
        return job

    def _get_or_create_company(self, name: str, is_demo: bool) -> Company:
        normalized = normalize_company(name)
        company = (
            self.session.execute(select(Company).where(Company.normalized_name == normalized))
            .scalars()
            .first()
        )
        if company is None:
            company = Company(
                company_name=name, normalized_name=normalized, is_demo=is_demo, on_watchlist=False
            )
            self.session.add(company)
            self.session.flush()
        return company

    def _attach_source(
        self, job: Job, payload: RawJobInput, raw: RawJobSourceRecord, hint: Any
    ) -> None:
        existing = (
            self.session.execute(
                select(JobSourceAssociation).where(
                    JobSourceAssociation.job_id == job.job_id,
                    JobSourceAssociation.connector == payload.connector,
                    JobSourceAssociation.source_external_id == payload.external_id,
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            existing.last_seen = utcnow()
            return

        role = SourceRole.DISCOVERY
        if (
            hint.ats_platform
            and payload.connector.upper() == (hint.ats_platform or "")
            or payload.discovery_channel == DiscoveryChannel.A_ATS_WATCHLIST
            # A URL the user pasted is a link they were looking at. When it
            # resolves to a recognised ATS the importer fetched that ATS's own
            # record, so the association is canonical rather than a sighting.
            or (payload.discovery_channel == DiscoveryChannel.MANUAL_USER_URL and hint.ats_platform)
        ):
            role = SourceRole.CANONICAL

        self.session.add(
            JobSourceAssociation(
                job_id=job.job_id,
                raw_record_id=raw.raw_record_id,
                connector=payload.connector,
                discovery_channel=payload.discovery_channel.value,
                source_external_id=payload.external_id,
                source_url=payload.url,
                role=role.value,
                posted_date_claimed=payload.posted_date,
                posted_date_confidence="HIGH" if payload.posted_date else "UNKNOWN",
            )
        )

    # -- change detection -------------------------------------------------
    @staticmethod
    def _normalized_hash(company: str, title: str, description: str) -> str:
        return content_hash(normalize_company(company), normalize_title(title), description)

    def _record_version(self, job: Job, new_description: str, new_hash: str) -> list[str]:
        """Snapshot the change and classify which downstream steps must re-run."""
        categories: list[str] = []
        if job.description != new_description:
            categories.append("description")
        version = JobVersion(
            job_id=job.job_id,
            normalized_content_hash=new_hash,
            changed_fields=categories,
            change_categories=categories,
            snapshot={"title": job.title, "description_len": len(job.description or "")},
        )
        self.session.add(version)
        return categories

    # -- analysis ---------------------------------------------------------
    def analyze(self, job: Job, *, mode: AnalysisMode | None = None) -> JobAnalysis:
        """Run classification, eligibility, matching and scoring for one job."""
        description = job.description or ""
        title = job.title

        employment_type = cls.classify_employment_type(title, description)
        remote_status = cls.classify_remote_status(title, job.location_raw or "", description)
        location = cls.parse_location(job.location_raw or "", description)
        experience_level = cls.classify_experience_level(title, description, employment_type)
        role_family, role_priority = cls.classify_role_family(title, description)
        lane_result = cls.classify_search_lane(
            title, description, employment_type, experience_level
        )
        lane = SearchLane(job.lane_override) if job.lane_override else lane_result.lane
        sponsorship = cls.extract_sponsorship_signal(description)
        auth_wording = cls.extract_authorization_wording(description)
        schedule = cls.extract_schedule_facts(description)
        years_min, _years_max = cls.extract_years_experience(description)

        job.employment_type = employment_type.value
        job.remote_status = remote_status.value
        job.experience_level = experience_level.value
        job.role_family = role_family.value
        job.role_priority = role_priority.value
        job.search_lane = lane.value
        job.weekly_hours = schedule.weekly_hours
        job.requires_enrollment = schedule.requires_enrollment
        job.academic_credit_required = schedule.academic_credit_required
        job.sponsorship_role_signal = sponsorship.signal.value
        job.sponsorship_quote = sponsorship.quote
        job.authorization_wording_verbatim = auth_wording
        job.location_country = location.country
        job.location_state = location.state
        job.location_city = location.city
        job.normalized_location = location.normalized

        # -- deterministic screen (records, never deletes) -----------------
        screen = screen_job(
            title=title,
            description=description,
            location=location,
            lane=lane,
            employment_type=employment_type,
            experience_level=experience_level,
            role_priority=role_priority,
            remote_status=remote_status,
            deadline=job.deadline,
            sponsorship_signal=sponsorship.signal,
        )
        job.screen_status = screen.status.value
        job.screen_reason = screen.reason

        # -- eligibility ---------------------------------------------------
        eligibility = assess_eligibility(
            lane=lane,
            location=location,
            remote_status=remote_status,
            employment_type=employment_type,
            experience_level=experience_level,
            years_min=years_min,
            requires_enrollment=schedule.requires_enrollment,
            weekly_hours=schedule.weekly_hours,
            deadline=job.deadline,
            start_date=job.start_date,
            end_date=job.end_date,
            sponsorship_signal=sponsorship.signal,
            bank=self.bank,
        )
        job.eligibility_status = eligibility.status.value
        job.authorization_state = eligibility.authorization_state.value

        # Every analysis is synchronous. There used to be a Fall fast path
        # here, because a time-critical co-op deadline justified analyzing in
        # the collection pass rather than a later batch. The search is
        # full-time only now (D-51), so there is nothing to fast-path.
        resolved_mode = mode or AnalysisMode.SYNCHRONOUS

        analysis = JobAnalysis(
            job_id=job.job_id,
            analyzed_hash=job.normalized_content_hash,
            analysis_mode=resolved_mode.value,
            fast_path=False,
            technologies=sorted({k for r in parse_requirements(description) for k in r.keywords})[
                :40
            ],
            years_experience_min=years_min,
            evidence_bank_version=self.bank.version,
            scoring_config_version=SCORING_CONFIG_VERSION,
            parser_version=PARSER_VERSION,
            llm_task_version=LLM_TASK_VERSION,
            deterministic_only=True,
            models_used={},
        )
        self.session.add(analysis)
        self.session.flush()

        self.session.add(
            EligibilityAssessment(
                analysis_id=analysis.analysis_id,
                status=eligibility.status.value,
                reasons=eligibility.reasons,
                checks=eligibility.checks,
                authorization_state=eligibility.authorization_state.value,
                authorization_note=eligibility.authorization_note,
                authorization_wording_verbatim=auth_wording,
                requires_isso_verification=eligibility.requires_isso_verification,
            )
        )
        self.session.add(
            SponsorshipSignal(
                analysis_id=analysis.analysis_id,
                role_signal=sponsorship.signal.value,
                role_signal_quote=sponsorship.quote,
                company_context=(
                    job.company.sponsorship_company_context if job.company else "NOT_CHECKED"
                ),
                company_context_note=(
                    "Historical, company-level context only. It says nothing about this role."
                ),
                explanation=" ".join(sponsorship.all_matches)[:2000] or None,
            )
        )

        legit_status, legit_evidence = self._assess_legitimacy(job)
        job.legitimacy_status = legit_status.value
        self.session.add(
            LegitimacyAssessment(
                analysis_id=analysis.analysis_id,
                status=legit_status.value,
                evidence=legit_evidence,
                explanation=(
                    "Legitimacy is a separate axis from fit. A posting may have high fit and "
                    "uncertain legitimacy at the same time."
                ),
            )
        )

        # -- requirements + evidence matching -------------------------------
        parsed = parse_requirements(description)
        pairs = []
        for req in parsed:
            requirement = JobRequirement(
                analysis_id=analysis.analysis_id,
                text=req.text,
                kind=req.kind.value,
                keywords=req.keywords,
                order_index=req.order_index,
                extraction=req.extraction,
                extraction_confidence=req.confidence,
            )
            self.session.add(requirement)
            self.session.flush()

            result = self.matcher.match(req)
            self.session.add(
                EvidenceMatch(
                    requirement_id=requirement.requirement_id,
                    support_status=result.support_status.value,
                    evidence_ids=result.evidence_ids,
                    skill_ids=result.skill_ids,
                    safe_claim_ids=result.safe_claim_ids,
                    confidence=result.confidence,
                    explanation=result.explanation,
                    transferable_only=result.transferable_only,
                    ownership_gated=result.ownership_gated,
                    evidence_bank_version=self.bank.version,
                )
            )
            pairs.append((req, result))

        # -- priority --------------------------------------------------------
        experience_matches = experience_level not in (
            cls.ExperienceLevel.SENIOR,
            cls.ExperienceLevel.STAFF_PLUS,
        )
        breakdown = self.scorer.score(
            lane=lane,
            eligibility=eligibility.status,
            matches=pairs,
            role_priority=role_priority,
            role_family=role_family,
            location=location,
            remote_status=remote_status,
            experience_matches=experience_matches,
            posted_or_first_seen=job.posted_date or job.first_seen,
            sponsorship_signal=sponsorship.signal,
            weekly_hours=schedule.weekly_hours,
            contacts_at_company=0,
        )
        self.session.add(
            PriorityScore(
                analysis_id=analysis.analysis_id,
                lane=lane.value,
                total=breakdown.total,
                band=breakdown.band,
                dimensions=breakdown.dimensions,
                max_dimensions=breakdown.max_dimensions,
                explanation=breakdown.explanation,
                scoring_config_version=SCORING_CONFIG_VERSION,
            )
        )

        analysis.why_apply = " ".join(breakdown.why_apply) or None
        analysis.risks = breakdown.risks + lane_result.reasons
        analysis.missing_requirements = breakdown.missing
        analysis.strongest_evidence = breakdown.strongest_evidence
        analysis.responsibilities = [
            r.text for r in parsed if r.kind == RequirementKind.RESPONSIBILITY
        ][:12]

        job.last_analyzed_hash = job.normalized_content_hash
        job.parser_version = PARSER_VERSION
        job.llm_task_version = LLM_TASK_VERSION
        job.evidence_bank_version = self.bank.version
        job.scoring_config_version = SCORING_CONFIG_VERSION
        return analysis

    def _assess_legitimacy(self, job: Job) -> tuple[LegitimacyStatus, list[str]]:
        """Evidence-backed only. Never accuses without a concrete signal."""
        evidence: list[str] = []
        if job.canonical_verification in ("VERIFICATION_FAILED",):
            evidence.append("Canonical employer posting could not be verified.")
        if job.repost_detected:
            evidence.append(f"Detected as a repost of {job.reposted_from_job_id}.")
        # A user-supplied record is not "unverified source" — a person read the
        # posting and chose to import it. Flagging those as SOURCE_UNVERIFIED
        # would put a legitimacy warning on every job the user found themselves.
        if job.discovery_source not in _USER_SUPPLIED_SOURCES and not job.canonical_source:
            evidence.append("Discovered via a broad channel with no canonical employer posting.")
            return LegitimacyStatus.SOURCE_UNVERIFIED, evidence
        if len(evidence) >= 2:
            return LegitimacyStatus.LEGITIMACY_UNCERTAIN, evidence
        return LegitimacyStatus.LEGITIMACY_NORMAL, evidence


# ---------------------------------------------------------------------------
# Collection run helpers
# ---------------------------------------------------------------------------


def start_collection_run(
    session: Session,
    *,
    connector: str,
    channel: DiscoveryChannel,
    source: str | None = None,
    lane: str | None = None,
    search_profile: str | None = None,
    scheduled_for: datetime | None = None,
    triggered_by: str = "manual",
) -> CollectionRun:
    run = CollectionRun(
        connector=connector,
        source=source,
        discovery_channel=channel.value,
        lane=lane,
        search_profile=search_profile,
        scheduled_for=scheduled_for,
        last_attempted_run=utcnow(),
        status=CollectionRunStatus.RUNNING.value,
        connector_version=PARSER_VERSION,
        triggered_by=triggered_by,
    )
    session.add(run)
    session.flush()
    return run


def finish_collection_run(
    run: CollectionRun, outcomes: list[IngestOutcome], *, error: str | None = None
) -> None:
    run.completed_at = utcnow()
    run.jobs_seen = len(outcomes)
    run.jobs_new = sum(1 for o in outcomes if o.created)
    run.jobs_changed = sum(1 for o in outcomes if o.merged_into_existing and not o.unchanged)
    run.jobs_unchanged = sum(1 for o in outcomes if o.unchanged)
    run.jobs_failed = sum(1 for o in outcomes if o.job is None)
    if error:
        run.status = CollectionRunStatus.FAILED.value
        run.error_summary = error
    elif run.jobs_failed and run.jobs_failed < len(outcomes):
        run.status = CollectionRunStatus.PARTIAL.value
    else:
        run.status = CollectionRunStatus.COMPLETED.value
        run.last_successful_run = utcnow()
