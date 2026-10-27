"""One HTTP client for every connector.

Written as a single shared client rather than per-connector ``requests.get()``
calls for reasons that are operational, not aesthetic. These endpoints belong to
other people. A retry loop without a bound, a missing timeout, or an ignored
``Retry-After`` is the difference between a well-behaved consumer of a public
API and one that gets an IP blocked — which would silently take out the whole
discovery layer.

So the politeness rules live here, once, and connectors cannot opt out of them:

* every request has a timeout — a hung socket must not stall a collection run
* retries are bounded and only for transient failures; a 404 is an answer, not
  something to ask again
* backoff is exponential with jitter, and an explicit ``Retry-After`` always
  wins over the computed delay
* 4xx other than 408/429 never retries
* connections are reused across a run
* every failure is a typed exception carrying enough context to record in a
  collection run, rather than a bare stack trace
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import user_agent

logger = logging.getLogger("jobhunt.http")

#: Identifies this client to the sources it talks to. A public API operator who
#: wants to contact the author of unusual traffic should be able to.
USER_AGENT = user_agent("0.3")

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3
#: Absolute ceiling on any single backoff, so a hostile Retry-After cannot
#: park a collection run for an hour.
MAX_BACKOFF_SECONDS = 30.0

#: Transient. Worth asking again.
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ConnectorHTTPError(RuntimeError):
    """Base for every failure this client raises."""

    def __init__(self, message: str, *, url: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status

    @property
    def summary(self) -> str:
        return f"{type(self).__name__}: {self}"


class SourceUnavailable(ConnectorHTTPError):
    """The source could not be reached, or failed after every retry.

    Distinct from :class:`SourceNotFound` on purpose: "the server is down" and
    "this board does not exist" must lead to different decisions. The first is
    a transient run failure; the second is a configuration error worth telling
    the user about.
    """


class SourceNotFound(ConnectorHTTPError):
    """404/410 — the board or company identifier does not exist."""


class SourceForbidden(ConnectorHTTPError):
    """401/403 — access is gated.

    For SmartRecruiters this is the expected answer for a company whose public
    feed is not enabled, and is recorded as a capability failure rather than an
    outage.
    """


class RateLimited(ConnectorHTTPError):
    """429 that survived every retry."""

    def __init__(self, message: str, *, url: str, retry_after: float | None = None) -> None:
        super().__init__(message, url=url, status=429)
        self.retry_after = retry_after


class MalformedResponse(ConnectorHTTPError):
    """The response was not the JSON the connector expected."""


@dataclass
class RequestStats:
    """What a run did to a source. Recorded, not just logged."""

    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    total_wait_seconds: float = 0.0
    statuses: dict[int, int] = field(default_factory=dict)

    def record(self, status: int | None) -> None:
        self.requests += 1
        if status is not None:
            self.statuses[status] = self.statuses.get(status, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "total_wait_seconds": round(self.total_wait_seconds, 2),
            "statuses": dict(sorted(self.statuses.items())),
        }


class HttpClient:
    """A polite, bounded JSON client.

    Usable as a context manager so connections are reused for a whole
    collection run and closed deterministically afterwards.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        min_interval: float = 0.0,
        user_agent: str = USER_AGENT,
        transport: httpx.BaseTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        #: Minimum spacing between requests to the same client. Crude but
        #: effective politeness for endpoints that publish no rate limit.
        self.min_interval = min_interval
        self.stats = RequestStats()
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
        )

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- requests --------------------------------------------------------
    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET and parse JSON, retrying only what is worth retrying."""
        return _parse_json(self._get(url, params=params), url)

    def post_json(self, url: str, *, body: dict[str, Any]) -> Any:
        """POST a JSON query and parse the JSON answer.

        For read-only search endpoints that take their query as a body
        (Workday's career-site search). Same timeout, retry, backoff and
        Retry-After rules as a GET — a POST here never changes anything on the
        other side, so retrying a transient failure is safe.
        """
        return _parse_json(self._get(url, json_body=body), url)

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            self._respect_min_interval()
            try:
                if json_body is not None:
                    response = self._client.post(url, params=params, json=json_body)
                else:
                    response = self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                self.stats.record(None)
                if attempt >= self.max_retries:
                    raise SourceUnavailable(
                        f"Timed out after {attempt + 1} attempt(s).", url=url
                    ) from exc
                attempt = self._back_off(attempt, url=url, reason="timeout")
                continue
            except httpx.HTTPError as exc:
                self.stats.record(None)
                if attempt >= self.max_retries:
                    raise SourceUnavailable(f"Transport error: {exc}", url=url) from exc
                attempt = self._back_off(attempt, url=url, reason=str(exc))
                continue

            self.stats.record(response.status_code)
            status = response.status_code

            if status < 400:
                return response

            # A missing board and a gated board are configuration answers, not
            # transient failures. Retrying them wastes the source's capacity
            # and tells the user nothing new.
            if status in (404, 410):
                raise SourceNotFound(f"Not found (HTTP {status}).", url=url, status=status)
            if status in (401, 403):
                raise SourceForbidden(
                    f"Access denied (HTTP {status}). The source may be tier-gated or "
                    f"not publicly enabled for this company.",
                    url=url,
                    status=status,
                )

            if status not in RETRY_STATUS or attempt >= self.max_retries:
                if status == 429:
                    self.stats.rate_limited += 1
                    raise RateLimited(
                        "Rate limited and out of retries.",
                        url=url,
                        retry_after=_retry_after(response),
                    )
                raise SourceUnavailable(f"HTTP {status}.", url=url, status=status)

            if status == 429:
                self.stats.rate_limited += 1
            attempt = self._back_off(
                attempt, url=url, reason=f"HTTP {status}", retry_after=_retry_after(response)
            )

    # -- politeness ------------------------------------------------------
    def _respect_min_interval(self) -> None:
        if not self.min_interval or self._last_request_at is None:
            self._last_request_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _back_off(
        self, attempt: int, *, url: str, reason: str, retry_after: float | None = None
    ) -> int:
        """Sleep, then return the next attempt number."""
        if retry_after is not None:
            # An explicit instruction from the server always wins over our
            # guess — that is the whole point of the header.
            delay = min(retry_after, MAX_BACKOFF_SECONDS)
        else:
            # Jitter matters: without it, several connectors that failed
            # together would retry together, in lockstep, forever.
            delay = min(2.0**attempt + random.uniform(0, 0.5), MAX_BACKOFF_SECONDS)

        self.stats.retries += 1
        self.stats.total_wait_seconds += delay
        logger.info("Retrying %s in %.1fs (%s)", url, delay, reason)
        self._sleep(delay)
        return attempt + 1


def _parse_json(response: httpx.Response, url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise MalformedResponse(
            f"Response was not valid JSON ({len(response.content)} bytes).",
            url=url,
            status=response.status_code,
        ) from exc


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The HTTP-date form is legal but rare on JSON APIs. Falling back to
        # computed backoff is safer than parsing it wrong.
        return None
