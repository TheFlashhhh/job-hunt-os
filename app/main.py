"""FastAPI application entry point.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.evidence.service import EvidenceBankError, get_evidence_bank
from app.web.api_collection import api_collection
from app.web.api_cover_letter import api_cover_letter
from app.web.api_people import api_people
from app.web.api_slice2 import api2
from app.web.api_slice3 import api3
from app.web.api_slice5 import api5
from app.web.api_slice51 import api51
from app.web.routes import api, router

logger = logging.getLogger("jobhunt")


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.jobhunt_log_level)

    app = FastAPI(
        title="Job Hunt OS",
        version="0.1.0",
        description=(
            "Evidence-grounded career decision-and-execution system. "
            "The Career Evidence Bank in data/*.yaml is the authoritative source of truth."
        ),
    )

    # Browser calls normally arrive same-origin through the Next.js rewrite, so
    # no preflight is issued and this middleware is not involved. It exists as a
    # narrow fallback for hitting the API directly from a dev frontend.
    #
    # Explicit origins only. `allow_origins=["*"]` would be wrong here: this API
    # serves one person's real job-search data from their own machine, and
    # `allow_credentials` with a wildcard is exactly the combination that turns
    # any visited page into a reader of localhost.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
        max_age=600,
    )

    @app.on_event("startup")
    def _validate_evidence_bank() -> None:
        """Fail fast if the Evidence Bank is missing or violates an invariant."""
        try:
            bank = get_evidence_bank()
        except EvidenceBankError:
            logger.exception("Evidence Bank failed to load — refusing to start")
            raise
        logger.info(
            "Evidence Bank v%s loaded: %s evidence, %s safe claims, %s prohibited claims",
            bank.version,
            len(bank.evidence),
            len(bank.safe_claims),
            len(bank.prohibited_claims),
        )
        if not settings.llm_available:
            logger.info(
                "ANTHROPIC_API_KEY not set — running DETERMINISTIC_ONLY. "
                "Every deterministic rule still applies; LLM enrichment is skipped."
            )

    @app.exception_handler(EvidenceBankError)
    def _evidence_bank_error(_request, exc: EvidenceBankError) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": "evidence_bank", "detail": str(exc)})

    app.include_router(router)
    app.include_router(api)
    app.include_router(api2)
    app.include_router(api3)
    app.include_router(api5)
    app.include_router(api51)
    app.include_router(api_collection)
    app.include_router(api_people)
    app.include_router(api_cover_letter)
    return app


app = create_app()
