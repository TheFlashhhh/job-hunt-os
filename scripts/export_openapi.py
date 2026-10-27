"""Write the FastAPI OpenAPI document to web/openapi.json.

The Next.js frontend generates its TypeScript types from this file, so the
backend schema is the single source of truth for response shapes. Run via
``npm run gen:api`` from ``web/``, or directly:

    python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.main import create_app  # noqa: E402


def main() -> int:
    schema = create_app().openapi()
    target = REPO_ROOT / "web" / "openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {target} ({len(schema['paths'])} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
