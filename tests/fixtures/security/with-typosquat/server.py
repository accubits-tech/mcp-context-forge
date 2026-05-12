# -*- coding: utf-8 -*-
"""Fixture: requirements.txt contains 'requets' (typosquat of 'requests').

Used by the security-scan gate tests to verify the malicious-package denylist
blocks the deploy before the build step ever runs pip install.
"""

# Third-Party
from fastapi import FastAPI

app = FastAPI(title="with-typosquat")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
