"""Recorded ATS payload shapes.

Hand-built from each vendor's documented response format rather than captured
from a live board, so the suite never depends on the network and no real
company's data lives in the repository. The shapes exercise what actually
breaks connectors in practice: optional fields that are missing, HTML that is
double-escaped, bodies split across arrays, and postings that should be dropped.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Greenhouse — boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
# ---------------------------------------------------------------------------

GREENHOUSE_FALL_JOB: dict[str, Any] = {
    "id": 4455667,
    "title": "Data Scientist, Product",
    "absolute_url": "https://boards.greenhouse.io/fixtureco/jobs/4455667",
    "location": {"name": "Boston, MA"},
    "first_published": "2026-08-01T14:02:00-04:00",
    "updated_at": "2026-08-05T09:15:00-04:00",
    "departments": [{"id": 1, "name": "Data Science"}],
    "offices": [{"id": 9, "name": "Boston"}],
    # Greenhouse returns HTML-escaped HTML. Double-encoded on purpose.
    "content": (
        "&lt;p&gt;Full-time Data Scientist role in Boston, MA, starting early 2027.&lt;/p&gt;"
        "&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;"
        "&lt;li&gt;Master&amp;#39;s degree in Data Science or a related field&lt;/li&gt;"
        "&lt;li&gt;Strong proficiency in Python and SQL&lt;/li&gt;"
        "&lt;li&gt;Experience building and maintaining data pipelines&lt;/li&gt;"
        "&lt;/ul&gt;"
        "&lt;h3&gt;Preferred qualifications&lt;/h3&gt;&lt;ul&gt;"
        "&lt;li&gt;Experience with Kafka or Spark&lt;/li&gt;&lt;/ul&gt;"
    ),
}

GREENHOUSE_FULLTIME_JOB: dict[str, Any] = {
    "id": 8899001,
    "title": "Data Engineer, New Grad 2027",
    "absolute_url": "https://boards.greenhouse.io/fixtureco/jobs/8899001",
    "location": {"name": "Remote (US)"},
    "first_published": "2026-08-03T10:00:00-04:00",
    "content": (
        "&lt;p&gt;We are hiring a new-graduate Data Engineer to start in early 2027. "
        "Remote within the United States. This is a full-time position.&lt;/p&gt;"
        "&lt;h3&gt;Minimum qualifications&lt;/h3&gt;&lt;ul&gt;"
        "&lt;li&gt;Bachelor&amp;#39;s or Master&amp;#39;s degree&lt;/li&gt;"
        "&lt;li&gt;0-2 years of experience&lt;/li&gt;"
        "&lt;li&gt;Proficiency in Python and SQL&lt;/li&gt;"
        "&lt;li&gt;Exposure to Kafka or Spark&lt;/li&gt;&lt;/ul&gt;"
        "&lt;p&gt;We sponsor work visas for qualified candidates.&lt;/p&gt;"
    ),
}

#: Every optional field absent. Boards really do look like this.
GREENHOUSE_MINIMAL_JOB: dict[str, Any] = {
    "id": 1,
    "title": "Data Analyst",
    "absolute_url": "https://boards.greenhouse.io/fixtureco/jobs/1",
}

#: Fully remote roles report "No offices", which is not a location.
GREENHOUSE_NO_OFFICE_JOB: dict[str, Any] = {
    "id": 2,
    "title": "Machine Learning Engineer",
    "absolute_url": "https://boards.greenhouse.io/fixtureco/jobs/2",
    "offices": [{"id": 0, "name": "No offices"}],
    "content": "&lt;p&gt;Python and SQL required.&lt;/p&gt;",
}

GREENHOUSE_UNTITLED_JOB: dict[str, Any] = {"id": 3, "title": "   "}


def greenhouse_payload(*jobs: dict[str, Any]) -> dict[str, Any]:
    return {"jobs": list(jobs), "meta": {"total": len(jobs)}}


# ---------------------------------------------------------------------------
# Lever — api.lever.co/v0/postings/{site}?mode=json
# ---------------------------------------------------------------------------

LEVER_FALL_JOB: dict[str, Any] = {
    "id": "aa11bb22-cc33-dd44-ee55-ff6677889900",
    "text": "Data Scientist, Product",
    "hostedUrl": "https://jobs.lever.co/fixtureco/aa11bb22",
    "applyUrl": "https://jobs.lever.co/fixtureco/aa11bb22/apply",
    "createdAt": 1785312000000,
    "workplaceType": "hybrid",
    "categories": {
        "location": "Cambridge, MA",
        "team": "Data",
        "commitment": "Full-time",
    },
    "descriptionPlain": (
        "Full-time data science role starting early 2027."
    ),
    "lists": [
        {
            "text": "Requirements",
            "content": (
                "<li>Master's degree</li>"
                "<li>Strong proficiency in Python and SQL</li>"
                "<li>Experience with data pipelines</li>"
            ),
        },
        {
            "text": "Preferred qualifications",
            "content": "<li>Familiarity with dbt</li>",
        },
    ],
    "additionalPlain": "We are an equal opportunity employer.",
}

LEVER_FULLTIME_JOB: dict[str, Any] = {
    "id": "bb22cc33-dd44-ee55-ff66-778899001122",
    "text": "Machine Learning Engineer, New Grad",
    "hostedUrl": "https://jobs.lever.co/fixtureco/bb22cc33",
    "createdAt": 1785398400000,
    "workplaceType": "remote",
    "categories": {"location": "Remote - US", "commitment": "Full-time"},
    "descriptionPlain": "New-grad machine learning engineer starting 2027.",
    "lists": [
        {
            "text": "Minimum qualifications",
            "content": "<li>0-2 years of experience</li><li>Python and SQL</li>",
        }
    ],
}

#: No lists, no plain text — only escaped HTML in `description`.
LEVER_HTML_ONLY_JOB: dict[str, Any] = {
    "id": "cc33dd44",
    "text": "Data Analyst",
    "hostedUrl": "https://jobs.lever.co/fixtureco/cc33dd44",
    "description": "<p>Requires <strong>SQL</strong> &amp; Python.</p>",
    "categories": {},
}

LEVER_UNTITLED_JOB: dict[str, Any] = {"id": "dd44", "text": ""}


# ---------------------------------------------------------------------------
# Ashby — api.ashbyhq.com/posting-api/job-board/{board}
# ---------------------------------------------------------------------------

ASHBY_FALL_JOB: dict[str, Any] = {
    "id": "3f2a9c14-7b8e-4d21-9f60-1a2b3c4d5e6f",
    "title": "Data Scientist - Product",
    "location": "Boston, MA",
    "isRemote": False,
    "isListed": True,
    "publishedAt": "2026-08-02T12:00:00.000Z",
    "jobUrl": "https://jobs.ashbyhq.com/fixtureco/3f2a9c14-7b8e-4d21-9f60-1a2b3c4d5e6f",
    "descriptionPlain": (
        "Full-time role starting early 2027.\n\n"
        "Requirements\n"
        "- Master's degree in Data Science\n"
        "- Strong proficiency in Python and SQL\n"
        "- Experience building data pipelines\n"
    ),
    "compensation": {
        "summaryComponents": [
            {
                "minValue": 30,
                "maxValue": 40,
                "currencyCode": "USD",
                "interval": "per hour",
            }
        ]
    },
}

ASHBY_FULLTIME_JOB: dict[str, Any] = {
    "id": "8c1d4e77-2a3b-4c5d-8e9f-0a1b2c3d4e5f",
    "title": "Analytics Engineer, Early Career",
    "location": "New York, NY",
    "isRemote": True,
    "isListed": True,
    "publishedAt": "2026-08-04T12:00:00.000Z",
    "jobUrl": "https://jobs.ashbyhq.com/fixtureco/8c1d4e77-2a3b-4c5d-8e9f-0a1b2c3d4e5f",
    "secondaryLocations": [{"location": "Boston, MA"}],
    "descriptionHtml": (
        "<p>Early-career analytics engineer.</p><ul>"
        "<li>0-2 years of experience</li><li>Strong SQL and Python</li></ul>"
    ),
}

#: Unpublished but still served. Must be dropped, not collected.
ASHBY_UNLISTED_JOB: dict[str, Any] = {
    "id": "9e0f1a22-3b4c-5d6e-7f80-91a2b3c4d5e6",
    "title": "Should Not Appear",
    "isListed": False,
    "descriptionPlain": "Unlisted.",
}


def ashby_payload(*jobs: dict[str, Any]) -> dict[str, Any]:
    return {"jobs": list(jobs), "apiVersion": "1"}


# ---------------------------------------------------------------------------
# SmartRecruiters — api.smartrecruiters.com/v1/companies/{id}/postings
# ---------------------------------------------------------------------------

SR_SUMMARY: dict[str, Any] = {
    "id": "sr-999",
    "name": "Data Scientist, University Graduate",
    "ref": "https://api.smartrecruiters.com/v1/companies/fixtureco/postings/sr-999",
    "releasedDate": "2026-08-01T08:00:00.000Z",
    "location": {"city": "Boston", "region": "MA", "country": "us", "remote": False},
}

SR_DETAIL: dict[str, Any] = {
    "id": "sr-999",
    "name": "Data Scientist, University Graduate",
    "jobAd": {
        "sections": {
            "companyDescription": {
                "title": "Company Description",
                "text": "<p>We build analytics products.</p>",
            },
            "jobDescription": {
                "title": "Job Description",
                "text": "<p>You will build models and dashboards.</p>",
            },
            "qualifications": {
                "title": "Qualifications",
                "text": (
                    "<ul><li>0-2 years of experience</li>"
                    "<li>Proficiency in Python and SQL</li></ul>"
                ),
            },
        }
    },
}


def sr_list_payload(*summaries: dict[str, Any]) -> dict[str, Any]:
    return {
        "offset": 0,
        "limit": 100,
        "totalFound": len(summaries),
        "content": list(summaries),
    }
