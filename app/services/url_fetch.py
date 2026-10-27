"""Guarded fetching for user-pasted URLs.

The manual importer takes a URL typed by the user and fetches it server-side.
That single sentence describes a server-side request forgery primitive, so the
guards here are not defensive polish — they are the reason the feature is
allowed to exist at all.

What is enforced, in order:

* **Scheme** — ``http`` and ``https`` only. ``file://``, ``ftp://``, ``gopher://``
  and ``data:`` are rejected before anything is resolved.
* **DNS, then the resolved address** — the hostname is resolved and EVERY
  returned address is checked. Checking the hostname string alone is the classic
  bypass: ``localtest.me`` and countless other public names resolve to
  127.0.0.1, and a name can resolve to several addresses of which only one is
  private.
* **Every redirect hop** — redirects are followed manually, one at a time, with
  the same address check applied to each new location. A public host that 302s
  to ``http://169.254.169.254/`` is the metadata-service attack, and a client
  with ``follow_redirects=True`` walks straight into it.
* **Size** — the body is read in chunks and abandoned past the cap, so a
  multi-gigabyte response cannot exhaust memory. ``Content-Length`` is checked
  first but never trusted on its own; a server can lie or omit it.
* **Timeout and hop count** — bounded, so one paste cannot hang a worker.
* **Content type** — HTML, XHTML, JSON and plain text. A PDF or an image is
  refused with a message that offers the paste-description fallback rather than
  an error.

This is deliberately NOT a general fetch helper, and it must not become one. It
has one caller and one purpose: retrieving a job posting the user chose. The
connector layer has its own client (``app/connectors/http.py``) for endpoints
that were configured by the user, not typed at runtime.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

import httpx

from app.config import user_agent

#: RFC 3986 scheme grammar. Used to tell "the user omitted the scheme" from
#: "the user supplied one we refuse".
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

#: Same identification the connectors use. An operator who wants to contact the
#: author of unusual traffic should be able to.
USER_AGENT = user_agent("0.5")

TIMEOUT_SECONDS = 12.0
MAX_REDIRECTS = 4
#: 3 MB. The largest real job page measured in this project was ~380 KB.
MAX_BYTES = 3_000_000

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Content types a job posting can plausibly arrive as.
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "application/json",
    "text/plain",
    "application/ld+json",
)

#: Hostnames that never leave the machine, whatever DNS says.
BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        # AWS/GCP/Azure instance metadata. Reachable by IP, but the names are
        # blocked too so a rewritten resolver cannot help.
        "metadata",
        "metadata.google.internal",
        "instance-data",
    }
)


class UrlFetchError(RuntimeError):
    """A paste that could not be fetched. Carries a machine-readable code."""

    def __init__(self, code: str, message: str, *, can_paste_description: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.can_paste_description = can_paste_description


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    content_type: str
    text: str
    redirects: list[str] = field(default_factory=list)
    truncated: bool = False

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


def normalize_url(raw: str) -> str:
    """Trim, add a scheme when the user omitted it, and validate the shape."""
    candidate = (raw or "").strip()
    if not candidate:
        raise UrlFetchError("EMPTY_URL", "Enter a job listing URL.")
    if len(candidate) > 2000:
        raise UrlFetchError("URL_TOO_LONG", "That URL is too long to be a job listing.")

    # A bare "boards.greenhouse.io/acme/jobs/1" is what people actually paste.
    # Detect the scheme by its grammar, not by "://" — `data:text/html,…` has a
    # scheme and no slashes, and prepending https:// to it produced
    # `https://data:text/html,…`, which parses as a host called "data" on port
    # "text" and turns a rejected scheme into a confusing hostname error.
    if not _SCHEME_RE.match(candidate):
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlFetchError(
            "BAD_SCHEME",
            f"Only http and https URLs can be imported (got {parsed.scheme or 'none'}).",
            can_paste_description=False,
        )
    if not parsed.hostname:
        raise UrlFetchError(
            "BAD_URL", "That does not look like a URL.", can_paste_description=False
        )
    if parsed.username or parsed.password:
        # user:pass@host is a phishing-shaped URL and is never needed for a
        # public job posting.
        raise UrlFetchError(
            "CREDENTIALS_IN_URL",
            "URLs containing credentials are not imported.",
            can_paste_description=False,
        )

    assert_public_host(parsed.hostname)
    return urlunparse(parsed)


def assert_public_host(hostname: str) -> list[str]:
    """Resolve ``hostname`` and require every address to be public.

    Returns the resolved addresses so a caller can log what it actually talked
    to. Raises :class:`UrlFetchError` on the first non-public address — ALL of
    them are checked, because a name that resolves to both a public and a
    private address is exactly the DNS-rebinding shape this is guarding.
    """
    name = hostname.strip().lower().rstrip(".")
    if not name:
        raise UrlFetchError("BAD_URL", "That URL has no host.", can_paste_description=False)
    if name in BLOCKED_HOSTNAMES or name.endswith((".local", ".internal", ".localdomain")):
        raise UrlFetchError(
            "PRIVATE_HOST",
            f"{hostname} is an internal name and cannot be imported.",
            can_paste_description=False,
        )

    # A literal address skips DNS but not the address check.
    literal = _as_ip(name)
    addresses = [str(literal)] if literal else _resolve(name)
    if not addresses:
        raise UrlFetchError("DNS_FAILED", f"Could not resolve {hostname}.")

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not _is_public(ip):
            raise UrlFetchError(
                "PRIVATE_ADDRESS",
                f"{hostname} resolves to {address}, which is not a public address.",
                can_paste_description=False,
            )
    return addresses


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return None


def _resolve(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlFetchError("DNS_FAILED", f"Could not resolve {hostname}: {exc}") from exc
    return sorted({str(info[4][0]) for info in infos})


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Public means public. ``is_global`` alone misses several ranges.

    ``ipaddress.is_global`` is close but not sufficient on its own across
    Python versions and address families, so the specific categories that
    matter here are checked explicitly.
    """
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:127.0.0.1 and friends must be judged as the v4 address.
        if ip.ipv4_mapped is not None:
            return _is_public(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _is_public(ip.sixtofour)
    return bool(getattr(ip, "is_global", True))


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def fetch_url(raw_url: str, *, client: httpx.Client | None = None) -> FetchedPage:
    """Fetch a user-supplied URL under every guard in this module.

    Redirects are followed by hand rather than by httpx, because the guard has
    to run again on each hop. ``follow_redirects=True`` would validate the URL
    the user typed and then quietly request whatever that host pointed at.
    """
    url = normalize_url(raw_url)
    owned = client is None
    session = client or httpx.Client(
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    redirects: list[str] = []
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            response = _request(session, current)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise UrlFetchError("BAD_REDIRECT", "The site redirected with no destination.")
                current = normalize_url(str(httpx.URL(current).join(location)))
                redirects.append(current)
                continue
            return _read(response, url=url, final_url=current, redirects=redirects)
        raise UrlFetchError("TOO_MANY_REDIRECTS", "That URL redirected too many times.")
    finally:
        if owned:
            session.close()


def _request(session: httpx.Client, url: str) -> httpx.Response:
    try:
        return session.send(session.build_request("GET", url), stream=True)
    except httpx.TimeoutException as exc:
        raise UrlFetchError("TIMEOUT", "The site took too long to respond.") from exc
    except httpx.HTTPError as exc:
        raise UrlFetchError("UNREACHABLE", f"Could not reach that URL: {exc}") from exc


def _read(
    response: httpx.Response, *, url: str, final_url: str, redirects: list[str]
) -> FetchedPage:
    try:
        status = response.status_code
        if status == 404:
            raise UrlFetchError(
                "NOT_FOUND", "That listing was not found (404). It may have closed."
            )
        if status in (401, 403):
            raise UrlFetchError(
                "ACCESS_DENIED",
                "That site refused an automated request. Paste the description instead.",
            )
        if status >= 400:
            raise UrlFetchError("HTTP_ERROR", f"The site returned HTTP {status}.")

        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type and not any(content_type.startswith(t) for t in ALLOWED_CONTENT_TYPES):
            raise UrlFetchError(
                "UNSUPPORTED_CONTENT",
                f"That URL returned {content_type}, not a web page. "
                f"If it is a PDF or a document, paste the description instead.",
            )

        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BYTES:
            raise UrlFetchError(
                "TOO_LARGE", f"That page is larger than {MAX_BYTES // 1_000_000} MB."
            )

        # Content-Length is a hint, not a promise. Read with a hard stop.
        chunks: list[bytes] = []
        size = 0
        truncated = False
        for chunk in response.iter_bytes(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                truncated = True
                break

        body = b"".join(chunks)
        encoding = response.encoding or "utf-8"
        try:
            text = body.decode(encoding, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")

        return FetchedPage(
            url=url,
            final_url=final_url,
            status=status,
            content_type=content_type or "text/html",
            text=text,
            redirects=redirects,
            truncated=truncated,
        )
    finally:
        response.close()
