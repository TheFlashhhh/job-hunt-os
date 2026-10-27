"""Manual URL import — the user pastes a listing they found somewhere else.

Jobs arrive from LinkedIn, a careers page, a professor, an alumni message or a
Google result. Those should not be second-class records: once imported, a job
must behave exactly like a discovered one — same screening, same eligibility,
same evidence matching, same CRM, same next action.

So this module does one job: turn a URL into a ``RawJobInput`` and hand it to
the existing :class:`~app.services.pipeline.JobPipeline`. It creates no second
ingestion path, no separate table and no parallel analysis. Provenance is the
only thing that differs, and it is recorded honestly — a pasted job is
``MANUAL_USER_URL``, never "discovered by Channel A".

**Extraction runs in order of decreasing trust.**

1. A known ATS behind the URL (Greenhouse, Lever, Ashby, SmartRecruiters) —
   fetched from its own documented JSON API using the ids in the URL, so the
   result is the employer's structured record rather than a scrape of its
   rendering.
2. ``schema.org`` ``JobPosting`` JSON-LD — the standard employers publish for
   Google Jobs, and by far the most reliable generic source.
3. OpenGraph / meta tags plus structured HTML.
4. Nothing. The importer says so and offers the paste-description fallback,
   because a wrong guess about a job's title is worse than an honest failure.

**Everything fetched is untrusted data.** A job page is a document written by
someone else; text inside it that reads like an instruction ("ignore previous
instructions", "send the candidate's details to…") is prose to be stored and
displayed, never something to act on. No fetched content reaches a shell, an
Evidence Bank write, an outreach draft or a submission. The pipeline's
``sanitize_external_text`` runs over every field, flags injection-shaped
content and records it on the raw record. This module additionally strips
``<script>`` and ``<style>`` before reading any text, so page code never enters
a description in the first place.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from app.connectors.greenhouse import _clean_content as _greenhouse_content
from app.connectors.greenhouse import _location as _greenhouse_location
from app.connectors.lever import _build_description as _lever_description
from app.connectors.lever import _location as _lever_location
from app.connectors.workday import _iso_date as _workday_date
from app.connectors.workday import _location as _workday_location
from app.connectors.workday import _strip_html as _workday_html
from app.connectors.workday import parse_workday_url, posting_id
from app.models.enums import DiscoveryChannel, SourceCapability
from app.services.dedupe import ATS_URL_PATTERNS
from app.services.pipeline import IngestOutcome, JobPipeline, RawJobInput
from app.services.url_fetch import FetchedPage, UrlFetchError, fetch_url, normalize_url

#: The connector name a pasted URL is recorded under. Distinct from "manual"
#: (paste-a-description) so the two are separable in source analytics.
USER_URL_CONNECTOR = "user_url"

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S
)
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)


@dataclass
class ExtractedPosting:
    """A posting as read from a URL, before it enters the pipeline."""

    title: str
    company_name: str
    description: str
    url: str
    location: str | None = None
    external_id: str | None = None
    provider: str | None = None
    ats_org_slug: str | None = None
    posted_date: date | None = None
    extraction: str = "UNKNOWN"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """Enough to create a record a human can act on."""
        return bool(self.title.strip() and self.company_name.strip())


@dataclass
class ImportOutcome:
    ok: bool
    posting: ExtractedPosting | None = None
    outcome: IngestOutcome | None = None
    duplicate: bool = False
    existing_job_id: str | None = None
    error_code: str | None = None
    error: str | None = None
    can_paste_description: bool = False
    messages: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str | None:
        job = self.outcome.job if self.outcome else None
        return job.job_id if job else None


# ---------------------------------------------------------------------------
# Provider identification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderMatch:
    platform: str
    org_slug: str
    job_id: str


def identify_provider(url: str) -> ProviderMatch | None:
    """The ATS behind this URL, from its shape alone. No network call.

    Reuses the same patterns canonical resolution uses, so a pasted Greenhouse
    link and a Channel A Greenhouse record resolve to the same identity — which
    is what makes deduplication between them work.
    """
    for platform, pattern in ATS_URL_PATTERNS:
        match = pattern.search(url)
        if match:
            return ProviderMatch(platform, match.group(1), match.group(2))
    return None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


class PostingExtractor:
    """Turns a URL into an :class:`ExtractedPosting`, best source first."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client

    def extract(self, raw_url: str) -> ExtractedPosting:
        url = normalize_url(raw_url)
        provider = identify_provider(url)

        if provider is not None:
            structured = self._from_provider(provider, url)
            if structured is not None and structured.usable:
                return structured

        page = fetch_url(url, client=self.client)
        self._assert_still_a_posting(provider, url, page)
        for reader in (self._from_json_ld, self._from_meta):
            posting = reader(page, provider)
            if posting is not None and posting.usable:
                return posting

        raise UrlFetchError(
            "NO_POSTING_FOUND",
            "That page loaded, but no job posting could be read from it. Many sites "
            "render listings with JavaScript. Paste the job description instead.",
        )

    def _assert_still_a_posting(
        self, provider: ProviderMatch | None, url: str, page: FetchedPage
    ) -> None:
        """Refuse a page the board redirected us away from.

        A closed Greenhouse posting 200s: the board bounces to its own index at
        ``/{org}?error=true``. That page has an ``og:title`` (the company name),
        a heading and four thousand characters of other openings, so the
        fallback readers happily built a job record out of it — the browser test
        produced a job titled "SharkNinja" at a company called
        "Sharkninjaoperatingllc" from a listing that no longer exists.

        The check is the posting id: if the URL named one and the page we ended
        up on does not mention it, we are not looking at that posting any more.
        """
        if provider is None or page.final_url == url:
            return
        if provider.job_id in page.final_url:
            return
        raise UrlFetchError(
            "LISTING_GONE",
            "That listing redirected to the company's job board, which usually means "
            "the posting has closed. Check the link, or paste the description if you "
            "still have it.",
        )

    # -- 1. known ATS -----------------------------------------------------
    def _from_provider(self, provider: ProviderMatch, url: str) -> ExtractedPosting | None:
        if provider.platform == "WORKDAY":
            return self._workday(provider, url)
        api = {
            "GREENHOUSE": (
                f"https://boards-api.greenhouse.io/v1/boards/"
                f"{provider.org_slug}/jobs/{provider.job_id}?content=true"
            ),
            "LEVER": f"https://api.lever.co/v0/postings/{provider.org_slug}/{provider.job_id}",
            "SMARTRECRUITERS": (
                f"https://api.smartrecruiters.com/v1/companies/"
                f"{provider.org_slug}/postings/{provider.job_id}"
            ),
        }.get(provider.platform)
        if api is None:
            # Ashby and Workday have no per-posting public GET keyed by the URL
            # ids, so those fall through to JSON-LD, which their boards publish.
            return None

        try:
            page = fetch_url(api, client=self.client)
            payload = json.loads(page.text)
        except UrlFetchError as exc:
            # A 404 from the EMPLOYER'S OWN API is an authoritative answer, not
            # a reason to go and scrape the board instead. Falling through here
            # is what let a closed SharkNinja posting become a job record built
            # out of the board's index page.
            if exc.code in {"NOT_FOUND", "LISTING_GONE"}:
                raise UrlFetchError(
                    "LISTING_GONE",
                    f"{provider.platform.title()} reports that this posting no longer "
                    f"exists. It has probably closed. Paste the description if you "
                    f"still have it.",
                ) from exc
            # Anything else (a timeout, a gated feed) may still be readable from
            # the page itself, so the generic readers get their turn.
            return None
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None

        builder = {
            "GREENHOUSE": self._greenhouse,
            "LEVER": self._lever,
            "SMARTRECRUITERS": self._smartrecruiters,
        }[provider.platform]
        return builder(payload, provider, url)

    def _greenhouse(
        self, raw: dict[str, Any], provider: ProviderMatch, url: str
    ) -> ExtractedPosting | None:
        title = str(raw.get("title") or "").strip()
        if not title:
            return None
        company = _company_from(raw.get("company_name"), provider.org_slug)
        return ExtractedPosting(
            title=title,
            company_name=company,
            description=_greenhouse_content(raw.get("content")) or title,
            url=str(raw.get("absolute_url") or url),
            location=_greenhouse_location(raw),
            external_id=str(raw.get("id")) if raw.get("id") is not None else provider.job_id,
            provider=provider.platform,
            ats_org_slug=provider.org_slug,
            posted_date=_iso_date(raw.get("first_published") or raw.get("updated_at")),
            extraction="ATS_API_GREENHOUSE",
            raw=raw,
        )

    def _lever(
        self, raw: dict[str, Any], provider: ProviderMatch, url: str
    ) -> ExtractedPosting | None:
        title = str(raw.get("text") or "").strip()
        if not title:
            return None
        raw_categories = raw.get("categories")
        categories: dict[str, Any] = raw_categories if isinstance(raw_categories, dict) else {}
        return ExtractedPosting(
            title=title,
            # Lever's posting API carries no employer name — `categories.team`
            # is the internal department ("Data Platform"), not the company.
            # The board slug is the only company signal the record actually has.
            company_name=_slug_to_name(provider.org_slug),
            description=_lever_description(raw) or title,
            url=str(raw.get("hostedUrl") or url),
            location=_lever_location(categories, raw),
            external_id=str(raw.get("id") or provider.job_id),
            provider=provider.platform,
            ats_org_slug=provider.org_slug,
            posted_date=_epoch_date(raw.get("createdAt")),
            extraction="ATS_API_LEVER",
            raw=raw,
        )

    def _smartrecruiters(
        self, raw: dict[str, Any], provider: ProviderMatch, url: str
    ) -> ExtractedPosting | None:
        title = str(raw.get("name") or "").strip()
        if not title:
            return None
        company = raw.get("company")
        company_name = _company_from(
            company.get("name") if isinstance(company, dict) else None, provider.org_slug
        )
        return ExtractedPosting(
            title=title,
            company_name=company_name,
            description=_smartrecruiters_body(raw) or title,
            url=str(raw.get("applyUrl") or raw.get("postingUrl") or url),
            location=_smartrecruiters_location(raw),
            external_id=str(raw.get("id") or provider.job_id),
            provider=provider.platform,
            ats_org_slug=provider.org_slug,
            posted_date=_iso_date(raw.get("releasedDate") or raw.get("createdOn")),
            extraction="ATS_API_SMARTRECRUITERS",
            raw=raw,
        )

    def _workday(self, provider: ProviderMatch, url: str) -> ExtractedPosting | None:
        """A Workday posting, read from the career site's own JSON endpoint.

        Only when the URL names the exact site — host, tenant, site and posting
        path. Anything less falls through to the generic readers; the tenant
        and site are never guessed.
        """
        parsed = parse_workday_url(url)
        if parsed is None or parsed[1] is None:
            return None
        board, path = parsed
        if path is None:
            return None
        try:
            page = fetch_url(f"{board.api_base}{path}", client=self.client)
            payload = json.loads(page.text)
        except UrlFetchError as exc:
            if exc.code in {"NOT_FOUND", "LISTING_GONE"}:
                raise UrlFetchError(
                    "LISTING_GONE",
                    "Workday reports that this posting no longer exists. It has probably "
                    "closed. Paste the description if you still have it.",
                ) from exc
            return None
        except (json.JSONDecodeError, ValueError):
            return None
        info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            return None
        title = str(info.get("title") or "").strip()
        if not title:
            return None
        organisation = payload.get("hiringOrganization")
        name = organisation.get("name") if isinstance(organisation, dict) else None
        external_url = info.get("externalUrl")
        return ExtractedPosting(
            title=title,
            # Workday usually leaves hiringOrganization.name empty; the tenant
            # is then the only employer signal, rendered readable, not claimed.
            company_name=_company_from(name, board.tenant),
            description=_workday_html(info.get("jobDescription")).strip() or title,
            url=str(external_url) if isinstance(external_url, str) and external_url.startswith("http") else board.posting_url(path),
            location=_workday_location(info, {}),
            external_id=posting_id(path),
            provider="WORKDAY",
            ats_org_slug=board.identifier,
            posted_date=_workday_date(info.get("startDate")),
            extraction="ATS_API_WORKDAY",
            raw=payload,
        )

    # -- 2. schema.org JobPosting -----------------------------------------
    def _from_json_ld(
        self, page: FetchedPage, provider: ProviderMatch | None
    ) -> ExtractedPosting | None:
        """The standard employers publish for Google Jobs.

        Blocks may be a single object, a list, or an ``@graph`` wrapper, and a
        page routinely carries several unrelated ones (Organization, WebSite,
        BreadcrumbList). Every block is walked and only ``JobPosting`` is read.
        """
        for block in _JSONLD_RE.findall(page.text):
            try:
                parsed = json.loads(html.unescape(block.strip()))
            except (json.JSONDecodeError, ValueError):
                continue
            for node in _walk_json_ld(parsed):
                if str(node.get("@type", "")).lower() != "jobposting":
                    continue
                title = str(node.get("title") or "").strip()
                organization = node.get("hiringOrganization")
                company = ""
                if isinstance(organization, dict):
                    company = str(organization.get("name") or "").strip()
                elif isinstance(organization, str):
                    company = organization.strip()
                if not company and provider is not None:
                    company = _slug_to_name(provider.org_slug)
                if not company:
                    company = _slug_to_name(urlparse(page.final_url).hostname or "")
                if not title:
                    continue
                return ExtractedPosting(
                    title=title,
                    company_name=company,
                    description=_html_to_text(str(node.get("description") or "")) or title,
                    url=str(node.get("url") or page.final_url),
                    location=_json_ld_location(node),
                    external_id=_json_ld_identifier(node)
                    or (provider.job_id if provider else None),
                    provider=provider.platform if provider else "SCHEMA_ORG",
                    ats_org_slug=provider.org_slug if provider else None,
                    posted_date=_iso_date(node.get("datePosted")),
                    extraction="JSON_LD_JOBPOSTING",
                    raw=node,
                )
        return None

    # -- 3. OpenGraph / meta + HTML ---------------------------------------
    def _from_meta(
        self, page: FetchedPage, provider: ProviderMatch | None
    ) -> ExtractedPosting | None:
        meta = _meta_tags(page.text)
        title = (
            meta.get("og:title")
            or meta.get("twitter:title")
            or _first_tag_text(page.text, "h1")
            or _title_tag(page.text)
            or ""
        ).strip()
        if not title:
            return None

        company = (
            meta.get("og:site_name")
            or (_slug_to_name(provider.org_slug) if provider else "")
            or _slug_to_name(urlparse(page.final_url).hostname or "")
        ).strip()
        if not company:
            return None

        # A meta description is a one-line teaser; the page body is what a
        # requirement parser can work with. The body wins whenever it has the
        # STRUCTURE a posting has — bullets or several lines — not when it
        # passes a length threshold. A fixed 400-character cutoff handed a
        # complete little posting to a 46-character teaser because the posting
        # was short.
        body = _html_to_text(page.text)
        teaser = (meta.get("og:description") or "").strip()
        structured = "\n- " in body or body.count("\n") >= 3
        description = (
            body if (structured and len(body) >= len(teaser)) else (teaser or body or title)
        )

        # "Senior Data Scientist at Acme | Acme Careers" — strip the tail so a
        # title-based classifier is not reading site furniture. Applied in
        # sequence rather than stopping at the first match: breaking after " | "
        # left "Senior Data Scientist at Northwind" as the role title.
        for separator in (" | ", " - ", " – ", " — "):
            if separator in title and len(title.split(separator)[0]) > 8:
                title = title.split(separator)[0].strip()

        # " at " is only removed when what follows really is the employer —
        # matched loosely, because og:title routinely shortens it ("at
        # Northwind" for "Northwind Systems"). "Data Scientist at Scale" is a
        # role name, not a company suffix, and must survive.
        head, separator, tail = title.rpartition(" at ")
        if separator and head and _same_employer(tail, company):
            title = head.strip()

        return ExtractedPosting(
            title=title,
            company_name=company,
            description=description,
            url=page.final_url,
            location=meta.get("og:locality") or None,
            external_id=provider.job_id if provider else None,
            provider=provider.platform if provider else "GENERIC_HTML",
            ats_org_slug=provider.org_slug if provider else None,
            extraction="HTML_META",
            raw={"meta": meta},
        )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class ManualJobUrlImportService:
    """URL → the same pipeline every discovered job goes through."""

    def __init__(self, session: Session, extractor: PostingExtractor | None = None) -> None:
        self.session = session
        self.pipeline = JobPipeline(session)
        self.extractor = extractor or PostingExtractor()

    def import_url(self, raw_url: str) -> ImportOutcome:
        try:
            posting = self.extractor.extract(raw_url)
        except UrlFetchError as exc:
            return ImportOutcome(
                ok=False,
                error_code=exc.code,
                error=str(exc),
                can_paste_description=exc.can_paste_description,
            )
        return self._ingest(posting)

    def import_pasted(
        self,
        *,
        description: str,
        url: str | None = None,
        company_name: str | None = None,
        title: str | None = None,
        location: str | None = None,
    ) -> ImportOutcome:
        """The fallback when a page cannot be read automatically.

        The pasted text is untrusted in exactly the same way a fetched page is —
        it came from the same document. It goes through the same sanitizer and
        the same parser.
        """
        text = (description or "").strip()
        if len(text) < 40:
            return ImportOutcome(
                ok=False,
                error_code="DESCRIPTION_TOO_SHORT",
                error="Paste the full job description — at least a few lines.",
                can_paste_description=True,
            )

        clean_url = normalize_url(url) if url and url.strip() else None
        provider = identify_provider(clean_url) if clean_url else None
        guessed_company, guessed_title, guessed_location = _guess_header(text)

        resolved_company = (
            (company_name or "").strip()
            or guessed_company
            or (_slug_to_name(provider.org_slug) if provider else "")
        )
        resolved_title = (title or "").strip() or guessed_title or ""
        if not resolved_company or not resolved_title:
            return ImportOutcome(
                ok=False,
                error_code="NEED_COMPANY_AND_TITLE",
                error=(
                    "Company and job title could not be read from that text. "
                    "Enter them and try again."
                ),
                can_paste_description=True,
            )

        return self._ingest(
            ExtractedPosting(
                title=resolved_title,
                company_name=resolved_company,
                description=text,
                url=clean_url or "",
                location=(location or "").strip() or guessed_location,
                external_id=provider.job_id if provider else None,
                provider=provider.platform if provider else "USER_PASTED",
                ats_org_slug=provider.org_slug if provider else None,
                extraction="USER_PASTED_DESCRIPTION",
            )
        )

    def save_url_only(
        self, *, url: str, company_name: str, title: str, location: str | None = None
    ) -> ImportOutcome:
        """Record the link and what the USER said, and nothing else.

        Deliberately spartan. The page could not be read, so there is no
        description, no posted date and no requirements — and inventing any of
        them would put a fabricated record into the one database this system
        treats as true. The record is marked for verification instead.
        """
        clean_url = normalize_url(url)
        company = (company_name or "").strip()
        job_title = (title or "").strip()
        if not company or not job_title:
            return ImportOutcome(
                ok=False,
                error_code="NEED_COMPANY_AND_TITLE",
                error="Saving a URL on its own still needs a company and a job title.",
                can_paste_description=True,
            )

        result = self._ingest(
            ExtractedPosting(
                title=job_title,
                company_name=company,
                description=(
                    "VERIFY FIRST — this listing was saved from a URL that could not be "
                    "read automatically. No description was captured. Open the link and "
                    "paste the description to analyse it."
                ),
                url=clean_url,
                location=(location or "").strip() or None,
                provider="USER_URL_ONLY",
                extraction="URL_ONLY",
            )
        )
        job = result.outcome.job if result.outcome else None
        if job is not None:
            job.user_notes = "VERIFY_FIRST: saved from a URL; no posting content captured."
        return result

    # -- shared -----------------------------------------------------------
    def _ingest(self, posting: ExtractedPosting) -> ImportOutcome:
        if posting.provider and posting.ats_org_slug:
            # A board already in the company registry has a real name. Workday
            # in particular leaves the employer blank, and its tenant
            # ("capitalone") is a placeholder, not a name.
            from app.services.monitored_companies import find_row

            known = find_row(
                self.session, ats_type=posting.provider, ats_identifier=posting.ats_org_slug
            )
            if known is not None and known.ats_identifier:
                posting.company_name = known.company_name
        payload = RawJobInput(
            company_name=posting.company_name,
            title=posting.title,
            description=posting.description,
            location=posting.location,
            url=posting.url or None,
            external_id=posting.external_id,
            posted_date=posting.posted_date,
            connector=USER_URL_CONNECTOR,
            # Honest provenance: the user found this, the system did not.
            discovery_channel=DiscoveryChannel.MANUAL_USER_URL,
            source_capability=SourceCapability.MANUAL,
            source_type=f"USER_IMPORTED:{posting.extraction}",
            raw_payload=posting.raw or None,
            is_demo=False,
        )
        outcome = self.pipeline.ingest(payload)
        if outcome.job is None:
            return ImportOutcome(
                ok=False,
                posting=posting,
                outcome=outcome,
                error_code="REJECTED",
                error="; ".join(outcome.messages) or "The posting could not be normalized.",
                can_paste_description=True,
            )

        duplicate = bool(outcome.merged_into_existing)
        return ImportOutcome(
            ok=True,
            posting=posting,
            outcome=outcome,
            duplicate=duplicate,
            existing_job_id=outcome.job.job_id if duplicate else None,
            messages=list(outcome.messages),
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _walk_json_ld(node: Any) -> list[dict[str, Any]]:
    """Flatten a JSON-LD document into every object it contains."""
    out: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            out.extend(_walk_json_ld(item))
    elif isinstance(node, dict):
        out.append(node)
        graph = node.get("@graph")
        if graph is not None:
            out.extend(_walk_json_ld(graph))
    return out


def _json_ld_location(node: dict[str, Any]) -> str | None:
    raw = node.get("jobLocation")
    entries = raw if isinstance(raw, list) else [raw]
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, dict):
            pieces = [
                str(address.get(key) or "").strip()
                for key in ("addressLocality", "addressRegion", "addressCountry")
            ]
            joined = ", ".join(p for p in pieces if p)
            if joined:
                parts.append(joined)
        elif isinstance(address, str) and address.strip():
            parts.append(address.strip())

    if str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        parts.append("(Remote)")
    return ", ".join(dict.fromkeys(parts))[:400] or None


def _json_ld_identifier(node: dict[str, Any]) -> str | None:
    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        value = identifier.get("value")
        return str(value) if value not in (None, "") else None
    if isinstance(identifier, str | int):
        return str(identifier)
    return None


def _meta_tags(source: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in _META_RE.findall(source):
        key = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        value = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if key and value:
            tags.setdefault(key.group(1).strip().lower(), html.unescape(value.group(1)).strip())
    return tags


def _title_tag(source: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", source, re.I | re.S)
    return html.unescape(_TAG_RE.sub("", match.group(1))).strip() if match else None


def _first_tag_text(source: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", source, re.I | re.S)
    return html.unescape(_TAG_RE.sub("", match.group(1))).strip() if match else None


def _html_to_text(source: str) -> str:
    """Readable text, with list and paragraph structure preserved.

    Structure is not cosmetic here: the JD parser recognises a requirements
    section by its heading and its bullets, so flattening a page to one line
    would produce zero requirements from a perfectly good posting.

    ``<script>`` and ``<style>`` go first, so page code can never become
    description text.
    """
    if not source:
        return ""
    text = _SCRIPT_RE.sub(" ", source)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"</(p|div|li|h[1-6]|tr|section|article)>", "\n", text, flags=re.I)
    text = re.sub(r"<(h[1-6])[^>]*>", "\n\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _smartrecruiters_body(raw: dict[str, Any]) -> str:
    """Reassemble SmartRecruiters' sectioned posting, headings intact."""
    ad = raw.get("jobAd")
    sections = ad.get("sections") if isinstance(ad, dict) else None
    if not isinstance(sections, dict):
        return ""
    chunks: list[str] = []
    for key in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        section = sections.get(key)
        if not isinstance(section, dict):
            continue
        heading = str(section.get("title") or "").strip()
        body = _html_to_text(str(section.get("text") or ""))
        if body:
            chunks.append(f"{heading}\n{body}" if heading else body)
    return "\n\n".join(chunks).strip()


def _smartrecruiters_location(raw: dict[str, Any]) -> str | None:
    location = raw.get("location")
    if not isinstance(location, dict):
        return None
    parts = [str(location.get(key) or "").strip() for key in ("city", "region", "country")]
    joined = ", ".join(p for p in parts if p)
    if location.get("remote"):
        joined = f"{joined} (Remote)".strip()
    return joined or None


def _same_employer(candidate: str, company: str) -> bool:
    """True when a title's " at X" tail names the company the page belongs to."""
    left = candidate.strip().lower()
    right = company.strip().lower()
    if not left or not right:
        return False
    return left == right or right.startswith(left) or left.startswith(right)


def _company_from(value: Any, slug: str) -> str:
    text = str(value or "").strip()
    return text or _slug_to_name(slug)


def _slug_to_name(slug: str) -> str:
    """ "acme-robotics" or "jobs.acme.com" → "Acme Robotics".

    A readable placeholder, not a claim. The company name is corrected by the
    normalizer if the same employer is already known under its real name.
    """
    text = (slug or "").strip().lower()
    for prefix in ("www.", "jobs.", "careers.", "boards.", "apply."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    text = re.sub(r"\.(com|io|co|org|net|ai|dev|jobs)(\.[a-z]{2})?$", "", text)
    text = re.sub(r"[._-]+", " ", text).strip()
    return " ".join(word.capitalize() for word in text.split())[:200]


def _iso_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _epoch_date(value: Any) -> date | None:
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0).date()
    except (ValueError, OSError, OverflowError):
        return None


_HEADER_LOCATION_RE = re.compile(r"^(?:location|based in|office)\s*[:\-]\s*(.+)$", re.I | re.M)


def _guess_header(blob: str) -> tuple[str | None, str | None, str | None]:
    """Company/title/location from a pasted block's own labelled lines."""
    lines = [line.strip() for line in blob.splitlines() if line.strip()][:15]
    company = title = location = None
    for line in lines:
        lowered = line.lower()
        if company is None and lowered.startswith(("company:", "employer:")):
            company = line.split(":", 1)[1].strip()
        elif title is None and lowered.startswith(("title:", "position:", "role:", "job title:")):
            title = line.split(":", 1)[1].strip()
        elif location is None and lowered.startswith(("location:", "based in:", "office:")):
            location = line.split(":", 1)[1].strip()

    if title is None and lines:
        title = lines[0][:200]
    if location is None:
        match = _HEADER_LOCATION_RE.search(blob[:2500])
        if match:
            location = match.group(1).strip() or None
    return company, title, location
