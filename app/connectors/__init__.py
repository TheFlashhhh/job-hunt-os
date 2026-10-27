"""Job source connectors.

The registry is the only place that maps an ATS name onto an implementation.
Nothing downstream of ``collect()`` branches on source type, so adding Slice 4's
``ApifyConnector`` means registering it here and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.connectors.ashby import AshbyConnector
from app.connectors.base import (
    CollectionResult,
    HealthResult,
    JobSourceConnector,
    RateLimitMetadata,
    SourceConfig,
    ValidationResult,
)
from app.connectors.greenhouse import GreenhouseConnector
from app.connectors.http import (
    ConnectorHTTPError,
    HttpClient,
    MalformedResponse,
    RateLimited,
    SourceForbidden,
    SourceNotFound,
    SourceUnavailable,
)
from app.connectors.lever import LeverConnector
from app.connectors.smartrecruiters import SmartRecruitersConnector
from app.connectors.workday import WorkdayConnector

#: ats_type → connector class. Structured public sources only.
CONNECTORS: dict[str, type[JobSourceConnector]] = {
    "GREENHOUSE": GreenhouseConnector,
    "LEVER": LeverConnector,
    "ASHBY": AshbyConnector,
    "SMARTRECRUITERS": SmartRecruitersConnector,
    "WORKDAY": WorkdayConnector,
}


@dataclass(frozen=True)
class ProviderCapability:
    """What a provider can do, stated once, so nothing else assumes it."""

    structured_collection: bool
    #: A single posting can be read back by the ids in its URL (manual import).
    job_lookup: bool
    #: A company on it can be added to the registry and polled.
    company_monitoring: bool
    #: Its listing is complete enough to age postings out.
    disappearance_detection: bool
    #: The endpoint is a documented public API, not just a public one.
    documented_public_api: bool
    note: str = ""


PROVIDER_CAPABILITIES: dict[str, ProviderCapability] = {
    "GREENHOUSE": ProviderCapability(True, True, True, True, True),
    "LEVER": ProviderCapability(True, True, True, True, True),
    "ASHBY": ProviderCapability(
        True, False, True, True, True, note="Single postings import via JSON-LD."
    ),
    "SMARTRECRUITERS": ProviderCapability(
        True, True, True, True, True, note="Public feed is a per-company plan setting."
    ),
    "WORKDAY": ProviderCapability(
        True,
        True,
        True,
        True,
        False,
        note=(
            "The career site's own public JSON endpoint; no login. Not a "
            "documented API. Disappearance only from a complete listing."
        ),
    ),
}

#: Sources that are deliberately NOT implemented, with the reason.
#: Kept in code so a future contributor sees the decision rather than the gap.
DEFERRED_SOURCES: dict[str, str] = {
    "WORKABLE": "Documented API requires an EMPLOYER token — unusable for other companies.",
    "BAMBOOHR": "Documented API requires an EMPLOYER key. Public endpoint is undocumented.",
    "PERSONIO": "Company-enabled XML feed; the API is OAuth. Conditional, later slice.",
    "APIFY_LINKEDIN": "Slice 4 only. Requires explicit approval, budget and Actor review.",
}


def get_connector(ats_type: str, **kwargs: object) -> JobSourceConnector:
    """Instantiate the connector for an ATS type."""
    key = (ats_type or "").strip().upper()
    connector_cls = CONNECTORS.get(key)
    if connector_cls is None:
        reason = DEFERRED_SOURCES.get(key)
        if reason:
            raise ValueError(f"{key} is not implemented in this slice. {reason}")
        raise ValueError(f"Unknown ATS type {ats_type!r}. Known: {', '.join(sorted(CONNECTORS))}.")
    return connector_cls(**kwargs)  # type: ignore[arg-type]


def supported_ats_types() -> list[str]:
    return sorted(CONNECTORS)


__all__ = [
    "CONNECTORS",
    "DEFERRED_SOURCES",
    "PROVIDER_CAPABILITIES",
    "AshbyConnector",
    "CollectionResult",
    "ConnectorHTTPError",
    "GreenhouseConnector",
    "HealthResult",
    "HttpClient",
    "JobSourceConnector",
    "LeverConnector",
    "ProviderCapability",
    "MalformedResponse",
    "RateLimitMetadata",
    "RateLimited",
    "SmartRecruitersConnector",
    "SourceConfig",
    "SourceForbidden",
    "SourceNotFound",
    "SourceUnavailable",
    "ValidationResult",
    "WorkdayConnector",
    "get_connector",
    "supported_ats_types",
]
