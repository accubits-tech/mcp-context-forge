# -*- coding: utf-8 -*-
"""Fixture: source contains a hardcoded AWS-key-shaped secret.

Used by the security-scan gate tests to verify gitleaks blocks the deploy.
The constant below MUST trip at least one gitleaks rule (length + pattern).
"""

# Standard
import os

# Third-Party
from fastapi import FastAPI

# This pretend key is structured to match gitleaks's aws-access-key rule.
# It's nonsense, never used to call AWS, and only exists in the fixture tree.
AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"  # noqa: S105

app = FastAPI(title="with-secret")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    # Third-Party
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
