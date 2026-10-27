"""Why didn't automated discovery find this job?

The user pastes a URL, the job lands in the system, and the obvious question is
the one the product could not answer: *the engine collects 8,000 jobs — why not
this one?* Without an answer the honest conclusion is "discovery is unreliable",
which is both demoralising and usually wrong. Most misses have a specific,
boring cause, and several of them have a one-click fix.

**Every reason is derived from stored provenance and collection history.** If
the recorded facts do not support any reason, the answer is ``UNKNOWN`` and it
says so. A plausible-sounding explanation invented to fill the field would be
worse than no explanation: it would be trusted.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.base import as_utc
from app.models.enums import CollectionRunStatus, DiscoveryChannel, ScreenStatus
from app.models.ingestion import CollectionRun, Job, JobSourceAssociation
from app.services.monitored_companies import RegistryEntry, is_monitored, registry_entries
from app.services.url_import import identify_provider
from app.services.watchlist import AUTOMATABLE_ATS, WatchlistLoadResult, load_watchlist

#: Sources that mean "a human put this here", as opposed to a connector.
MANUAL_SOURCES = {"MANUAL", "MANUAL_IMPORT", "MANUAL_USER_URL", "USER_URL", "USER_ADDED"}

DISCOVERED_AUTOMATICALLY = "DISCOVERED_AUTOMATICALLY"
COLLECTED_BUT_SCREENED_OUT = "COLLECTED_BUT_SCREENED_OUT"
DEDUPED_TO_EXISTING_JOB = "DEDUPED_TO_EXISTING_JOB"
SUPPORTED_ATS_BUT_NOT_MONITORED = "SUPPORTED_ATS_BUT_NOT_MONITORED"
COMPANY_NOT_MONITORED = "COMPANY_NOT_MONITORED"
UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
POSTED_AFTER_LAST_SUCCESSFUL_RUN = "POSTED_AFTER_LAST_SUCCESSFUL_RUN"
SOURCE_COLLECTION_FAILED = "SOURCE_COLLECTION_FAILED"
BROAD_DISCOVERY_NOT_ACTIVE = "BROAD_DISCOVERY_NOT_ACTIVE"
UNKNOWN = "UNKNOWN"


@dataclass
class MonitorOffer:
    """A concrete fix, when one exists. Never acted on without a click."""

    company_name: str
    ats_type: str
    ats_identifier: str
    careers_url: str | None = None


@dataclass
class WhyMissed:
    reason: str
    headline: str
    detail: str
    #: Everything the answer was derived from, so a surprising verdict can be
    #: checked rather than believed.
    evidence: dict[str, Any] = field(default_factory=dict)
    monitor_offer: MonitorOffer | None = None
    secondary_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "headline": self.headline,
            "detail": self.detail,
            "evidence": self.evidence,
            "secondary_reasons": self.secondary_reasons,
            "monitor_offer": (
                {
                    "company_name": self.monitor_offer.company_name,
                    "ats_type": self.monitor_offer.ats_type,
                    "ats_identifier": self.monitor_offer.ats_identifier,
                    "careers_url": self.monitor_offer.careers_url,
                }
                if self.monitor_offer
                else None
            ),
        }


class WhyMissedService:
    def __init__(
        self, session: Session, *, watchlist: WatchlistLoadResult | None = None
    ) -> None:
        self.session = session
        self.watchlist = watchlist or load_watchlist()

    def explain(self, job: Job) -> WhyMissed:
        platform, slug = self._board(job)
        evidence: dict[str, Any] = {
            "discovery_source": job.discovery_source,
            "canonical_source": job.canonical_source,
            "ats_platform": platform,
            "ats_org_slug": slug,
            "posted_date": job.posted_date.isoformat() if job.posted_date else None,
            "first_seen": _iso(as_utc(job.first_seen)),
            "screen_status": job.screen_status,
        }

        if not self._is_manual(job):
            return WhyMissed(
                reason=DISCOVERED_AUTOMATICALLY,
                headline="This job was found by automated collection.",
                detail=(
                    f"It arrived from {job.canonical_source or job.discovery_source} rather than "
                    "from a pasted link, so there is nothing to explain."
                ),
                evidence=evidence,
            )

        # A job can be BOTH pasted and collected: dedupe merges the two into
        # one row. That is a different story from a miss, and it is the story
        # the source associations tell.
        automated = self._automated_associations(job)
        evidence["automated_source_records"] = len(automated)
        if automated:
            if job.screen_status in (ScreenStatus.REJECTED.value, ScreenStatus.ARCHIVED.value):
                return WhyMissed(
                    reason=COLLECTED_BUT_SCREENED_OUT,
                    headline="It was collected — and then screened out.",
                    detail=(
                        "Automated collection did find this posting. Screening set it to "
                        f"{job.screen_status}"
                        + (f": {job.screen_reason}" if job.screen_reason else ".")
                        + " Pasting the link brought it back into view."
                    ),
                    evidence=evidence,
                )
            return WhyMissed(
                reason=DEDUPED_TO_EXISTING_JOB,
                headline="It was already here under a different link.",
                detail=(
                    "Automated collection had already stored this posting, and the link you "
                    "pasted resolved to the same job rather than creating a second copy."
                ),
                evidence=evidence,
            )

        held = self._held_in_registry(job, platform, slug, evidence)
        if held is not None:
            return held

        monitored = is_monitored(
            self.session,
            ats_type=platform,
            ats_identifier=slug,
            company_name=job.company_name,
            watchlist=self.watchlist,
        )
        evidence["monitored"] = monitored

        supported = (platform or "").upper() in AUTOMATABLE_ATS and bool(slug)

        if monitored and supported:
            return self._monitored_but_missed(job, platform or "", slug or "", evidence)

        if supported:
            return WhyMissed(
                reason=SUPPORTED_ATS_BUT_NOT_MONITORED,
                headline=f"{job.company_name} uses {_title(platform)}, which is supported.",
                detail=(
                    f"{job.company_name} uses {_title(platform)}, which Job Hunt OS collects "
                    f"from — but {job.company_name} was not in your monitored company set when "
                    "this opening was published. Monitoring the board covers every future "
                    "posting from this employer."
                ),
                evidence=evidence,
                monitor_offer=MonitorOffer(
                    company_name=job.company_name,
                    ats_type=(platform or "").upper(),
                    ats_identifier=slug or "",
                    careers_url=job.canonical_url,
                ),
                secondary_reasons=self._broad_discovery_note(),
            )

        if (platform or "").upper() in AUTOMATABLE_ATS and not monitored:
            # Supported provider, but the link does not identify the exact
            # board (a Workday URL without its site, say). There is nothing to
            # offer a click on, and saying "unsupported" would be false.
            return WhyMissed(
                reason=SUPPORTED_ATS_BUT_NOT_MONITORED,
                headline=f"{job.company_name} uses {_title(platform)}, which is supported.",
                detail=(
                    f"{job.company_name} uses {_title(platform)}, which Job Hunt OS collects "
                    f"from, but {job.company_name} was not monitored when this posting "
                    "appeared — and this link does not identify the exact career site, so "
                    "there is no board to add from here. Add the company's careers URL on "
                    "the Sources page."
                ),
                evidence=evidence,
                secondary_reasons=self._broad_discovery_note(),
            )

        if monitored:
            return WhyMissed(
                reason=UNSUPPORTED_SOURCE,
                headline=f"{job.company_name} is on your watchlist, but this source is not pollable.",
                detail=(
                    f"{job.company_name} is a company you track, but this posting is hosted on "
                    f"{_source_label(platform, job)}, which has no connector. Postings from this "
                    "employer reach the system by link until one exists."
                ),
                evidence=evidence,
                secondary_reasons=self._broad_discovery_note(),
            )

        if platform:
            return WhyMissed(
                reason=UNSUPPORTED_SOURCE,
                headline=f"{_source_label(platform, job)} is not a source this system collects.",
                detail=(
                    f"This posting is hosted on {_source_label(platform, job)}. Job Hunt OS polls "
                    f"{', '.join(sorted(AUTOMATABLE_ATS))} boards; nothing else has a connector, "
                    "so a posting there only arrives when you paste it."
                ),
                evidence=evidence,
                secondary_reasons=self._broad_discovery_note(),
            )

        return WhyMissed(
            reason=COMPANY_NOT_MONITORED,
            headline=f"{job.company_name} is not in your monitored company set.",
            detail=(
                f"Nothing polls {job.company_name}, and the link did not identify a supported "
                "job board, so there is no board to add. Structured collection only covers "
                "companies you monitor."
            ),
            evidence=evidence,
            secondary_reasons=self._broad_discovery_note(),
        )

    # -- in the registry, but not polled ----------------------------------
    def _held_in_registry(
        self, job: Job, platform: str | None, slug: str | None, evidence: dict[str, Any]
    ) -> WhyMissed | None:
        """The employer is in the company registry in a state that is not
        polled — paused, on trial, manual-only, or awaiting verification.

        Each says something different and true about why nothing collected
        this posting, so each gets its own answer rather than "not monitored".
        """
        entry = self._registry_entry(job, platform, slug)
        if entry is None or entry.monitoring_status == "ACTIVE":
            return None
        evidence["registry_status"] = entry.monitoring_status
        evidence["registry_source"] = entry.registry_source
        name = job.company_name
        board = _title(entry.ats_type) if entry.collectable else None
        if entry.monitoring_status == "PAUSED":
            return WhyMissed(
                reason=COMPANY_NOT_MONITORED,
                headline=f"{name} is in your company registry, but paused.",
                detail=(
                    f"Monitoring of {name}"
                    + (f" on {board}" if board else "")
                    + " was paused, so structured collection skipped its board. Resume it on "
                    "the Sources page to cover its future postings."
                ),
                evidence=evidence,
            )
        if entry.monitoring_status == "EXPERIMENTAL":
            return WhyMissed(
                reason=COMPANY_NOT_MONITORED,
                headline=f"{name} is on trial, not yet in the regular sweep.",
                detail=(
                    f"{name}'s {board or 'board'} is configured but still a canary: it is "
                    "collected only when run by name, not on the six-hourly refresh, until it "
                    "is promoted."
                ),
                evidence=evidence,
            )
        if entry.monitoring_status == "MANUAL_ONLY":
            return WhyMissed(
                reason=UNSUPPORTED_SOURCE,
                headline=f"{name} is tracked as manual-only.",
                detail=(
                    f"{name} is in your company registry, but its careers site is not a source "
                    "Job Hunt OS can poll, so its postings arrive only when you paste them."
                ),
                evidence=evidence,
                secondary_reasons=self._broad_discovery_note(),
            )
        return WhyMissed(
            reason=COMPANY_NOT_MONITORED,
            headline=f"{name} is in the registry, awaiting a verified careers source.",
            detail=(
                f"{name} was added to the company registry, but its careers source has not "
                "been confirmed, and nothing is polled on a guess. Add its job-board URL on the "
                "Sources page to start monitoring it."
            ),
            evidence=evidence,
            secondary_reasons=self._broad_discovery_note(),
        )

    def _registry_entry(
        self, job: Job, platform: str | None, slug: str | None
    ) -> RegistryEntry | None:
        from app.services.text import normalize_company

        name = normalize_company(job.company_name or "")
        for entry in registry_entries(self.session, watchlist=self.watchlist):
            same_board = bool(slug) and (entry.ats_type, (entry.ats_identifier or "").lower()) == (
                (platform or "").upper(),
                (slug or "").lower(),
            )
            if same_board or (name and normalize_company(entry.company_name) == name):
                return entry
        return None

    # -- monitored, and still missed -------------------------------------
    def _monitored_but_missed(
        self, job: Job, platform: str, slug: str, evidence: dict[str, Any]
    ) -> WhyMissed:
        runs = self._runs_for(job.company_name, platform)
        success_stamps = _stamps(r.last_successful_run for r in runs)
        attempt_stamps = _stamps(r.started_at for r in runs)
        last_success = max(success_stamps) if success_stamps else None
        last_attempt = max(attempt_stamps) if attempt_stamps else None
        failed = [r for r in runs if r.status == CollectionRunStatus.FAILED.value]
        evidence["company_last_success"] = _iso(last_success)
        evidence["company_last_attempt"] = _iso(last_attempt)
        evidence["company_runs"] = len(runs)

        published = self._published_at(job)
        evidence["published_reference"] = _iso(published)

        if last_success is None:
            detail = (
                f"{job.company_name} is monitored on {_title(platform)}, but no collection of "
                "that board has ever succeeded"
            )
            if failed:
                detail += f" — the last attempt failed: {failed[0].error_summary or 'no detail recorded'}"
            return WhyMissed(
                reason=SOURCE_COLLECTION_FAILED,
                headline=f"{job.company_name} is monitored, but its board has never collected.",
                detail=detail + ".",
                evidence=evidence,
            )

        if published is not None and published > last_success:
            return WhyMissed(
                reason=POSTED_AFTER_LAST_SUCCESSFUL_RUN,
                headline="It was published after the last successful collection.",
                detail=(
                    f"{job.company_name} is monitored on {_title(platform)}. The last successful "
                    f"collection of that board was {last_success:%Y-%m-%d %H:%M} UTC, and this "
                    "posting is newer. The next collection would have found it."
                ),
                evidence=evidence,
            )

        if failed and last_attempt is not None and (
            last_success is None or last_attempt > last_success
        ):
            return WhyMissed(
                reason=SOURCE_COLLECTION_FAILED,
                headline=f"The last poll of {job.company_name}'s board failed.",
                detail=(
                    f"{job.company_name} is monitored, but the most recent attempt did not "
                    f"succeed: {failed[0].error_summary or 'no detail recorded'}. A failed poll "
                    "collects nothing and is never treated as evidence that a job is gone."
                ),
                evidence=evidence,
            )

        return WhyMissed(
            reason=UNKNOWN,
            headline="No recorded reason explains this one.",
            detail=(
                f"{job.company_name} is monitored on {_title(platform)}, that board last "
                f"collected successfully at {last_success:%Y-%m-%d %H:%M} UTC, and this posting "
                "is not newer than that run. The provenance on file does not say why it was "
                "missed, so the system will not guess. Worth checking the board by hand."
            ),
            evidence=evidence,
        )

    # -- helpers ---------------------------------------------------------
    def _is_manual(self, job: Job) -> bool:
        return (job.discovery_source or "").upper() in MANUAL_SOURCES

    def _board(self, job: Job) -> tuple[str | None, str | None]:
        """The ATS behind this posting: what was stored, else what the URL says.

        For Workday the board is the exact career site — host, tenant and site —
        read from the posting's own URL. The stored org slug is only the tenant,
        and a tenant alone is not a board anyone can poll; when the URL does not
        name the site, the board is unknown rather than guessed.
        """
        if (job.ats_platform or "").upper() == "WORKDAY":
            from app.connectors.workday import parse_workday_url

            for url in (job.canonical_url, getattr(job, "source_url", None)):
                parsed = parse_workday_url(str(url)) if url else None
                if parsed:
                    return "WORKDAY", parsed[0].identifier
            return "WORKDAY", None
        if job.ats_platform:
            return job.ats_platform.upper(), job.ats_org_slug
        for url in (job.canonical_url, getattr(job, "source_url", None)):
            if not url:
                continue
            match = identify_provider(str(url))
            if match:
                return match.platform, match.org_slug
        return None, None

    def _automated_associations(self, job: Job) -> list[JobSourceAssociation]:
        rows = (
            self.session.execute(
                select(JobSourceAssociation).where(JobSourceAssociation.job_id == job.job_id)
            )
            .scalars()
            .all()
        )
        return [
            row
            for row in rows
            if (row.discovery_channel or "") == DiscoveryChannel.A_ATS_WATCHLIST.value
        ]

    def _runs_for(self, company_name: str, platform: str) -> list[CollectionRun]:
        rows = (
            self.session.execute(
                select(CollectionRun).where(
                    CollectionRun.is_fixture.is_(False),
                    CollectionRun.connector == platform.lower(),
                )
            )
            .scalars()
            .all()
        )
        normalized = _normalize(company_name)
        return [r for r in rows if _normalize(r.company_name or "") == normalized]

    def _published_at(self, job: Job) -> datetime | None:
        """When the posting became visible, best available evidence first.

        ``posted_date`` when the source stated one; otherwise the moment the
        system first saw it, which is a lower bound rather than a claim about
        the posting's own age. The two are never conflated elsewhere and are
        not conflated here — the field is named for what it is.
        """
        first_seen = as_utc(job.first_seen)
        if job.posted_date is not None:
            return datetime.combine(job.posted_date, datetime.min.time()).replace(
                tzinfo=first_seen.tzinfo if first_seen else UTC
            )
        return first_seen

    def _broad_discovery_note(self) -> list[str]:
        from app.services.collection_status import CollectionStatusService

        state = CollectionStatusService(self.session, watchlist=self.watchlist)._broad_discovery()
        if state.get("state") == "ACTIVE":
            return []
        return [BROAD_DISCOVERY_NOT_ACTIVE]


def _stamps(values: Iterable[datetime | None]) -> list[datetime]:
    return [stamp for stamp in (as_utc(v) for v in values) if stamp is not None]


def _normalize(name: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in name or "")
    return " ".join(cleaned.split())


def _title(platform: str | None) -> str:
    mapping = {
        "GREENHOUSE": "Greenhouse",
        "LEVER": "Lever",
        "ASHBY": "Ashby",
        "SMARTRECRUITERS": "SmartRecruiters",
        "WORKDAY": "Workday",
    }
    return mapping.get((platform or "").upper(), (platform or "an unknown ATS").title())


def _source_label(platform: str | None, job: Job) -> str:
    if platform:
        return _title(platform)
    host = ""
    if job.canonical_url:
        from urllib.parse import urlparse

        host = urlparse(job.canonical_url).hostname or ""
    return host or "an unrecognised source"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
