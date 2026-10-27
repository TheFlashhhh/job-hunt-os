"""Careers URL → provider, board and confidence. No network: connectors are fakes."""

from __future__ import annotations

import pytest

from app.connectors.base import HealthResult, ValidationResult
from app.services.source_detection import board_from_url, detect_source


class _FakeConnector:
    def __init__(self, reachable: bool = True, valid: bool = True) -> None:
        self.reachable = reachable
        self.valid = valid
        self.checked: list[str] = []

    def validate_config(self, config):  # noqa: ANN001
        return ValidationResult.valid() if self.valid else ValidationResult.invalid("bad board")

    def health_check(self, config):  # noqa: ANN001
        self.checked.append(config.ats_identifier)
        return HealthResult(
            reachable=self.reachable,
            status="OK" if self.reachable else "SourceNotFound",
            detail="12 posting(s) visible." if self.reachable else "Not found (HTTP 404).",
            postings_visible=12 if self.reachable else None,
        )

    def close(self) -> None:
        pass


def _factory(connector: _FakeConnector):
    return lambda _ats: connector


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/klaviyo", ("GREENHOUSE", "klaviyo")),
        ("https://job-boards.greenhouse.io/datadog/jobs/123", ("GREENHOUSE", "datadog")),
        ("https://jobs.lever.co/palantir", ("LEVER", "palantir")),
        ("https://jobs.lever.co/palantir/0f1e2d3c-aaaa", ("LEVER", "palantir")),
        ("https://jobs.ashbyhq.com/ramp", ("ASHBY", "ramp")),
        ("https://jobs.smartrecruiters.com/Visa", ("SMARTRECRUITERS", "Visa")),
        (
            "https://capitalone.wd12.myworkdayjobs.com/en-US/Capital_One",
            ("WORKDAY", "capitalone.wd12.myworkdayjobs.com/Capital_One"),
        ),
        ("https://www.acme.com/careers", None),
        ("https://boards.greenhouse.io/embed", None),
    ],
)
def test_board_urls_are_read_from_their_shape(url, expected):
    assert board_from_url(url) == expected


def test_a_board_url_that_answers_is_confirmed():
    connector = _FakeConnector()
    detection = detect_source(
        "https://jobs.lever.co/palantir", company_name="Palantir", connector_factory=_factory(connector)
    )
    assert detection.confirmed
    assert (detection.provider, detection.identifier) == ("LEVER", "palantir")
    assert detection.method == "URL_SHAPE"
    assert detection.postings_visible == 12
    assert connector.checked == ["palantir"]


def test_a_board_url_that_does_not_answer_is_not_confirmed():
    detection = detect_source(
        "https://jobs.lever.co/nobody",
        connector_factory=_factory(_FakeConnector(reachable=False)),
    )
    assert detection.confidence == "UNKNOWN"
    assert "did not answer" in detection.detail


def test_a_careers_page_linking_to_a_board_is_only_likely():
    """A page link is a hint about which board; a human confirms it."""
    page = (
        "<html><a href='https://boards.greenhouse.io/acme'>Open roles</a>"
        "<a href='https://jobs.lever.co/acme-labs'>Labs</a></html>"
    )
    detection = detect_source(
        "https://www.acme.com/careers",
        company_name="Acme",
        connector_factory=_factory(_FakeConnector()),
        page_fetcher=lambda _url: page,
    )
    assert detection.confidence == "LIKELY_REVIEW"
    assert not detection.confirmed
    assert (detection.provider, detection.identifier) == ("GREENHOUSE", "acme")
    assert detection.method == "PAGE_LINK"
    assert detection.alternatives == ["LEVER:acme-labs"]


def test_a_page_with_no_supported_board_says_so_plainly():
    detection = detect_source(
        "https://careers.branded.example/jobs",
        connector_factory=_factory(_FakeConnector()),
        page_fetcher=lambda _url: "<html><a href='https://branded.icims.com/jobs'>Jobs</a></html>",
    )
    assert detection.confidence == "UNKNOWN"
    assert detection.provider is None
    assert "not currently supported for automatic monitoring" in detection.detail


def test_an_unreadable_page_is_unknown_not_an_error():
    from app.services.url_fetch import UrlFetchError

    def refuse(_url):
        raise UrlFetchError("BLOCKED", "refused")

    detection = detect_source(
        "https://careers.example.com",
        connector_factory=_factory(_FakeConnector()),
        page_fetcher=refuse,
    )
    assert detection.confidence == "UNKNOWN"
