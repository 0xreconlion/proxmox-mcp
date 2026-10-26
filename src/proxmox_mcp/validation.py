"""Canonical shared validators for bounded identifiers and untrusted text.

Every pattern and predicate here was previously duplicated across several modules.
`SAFE_ID` and `SAFE_KEY` are deliberately separate contracts: before this module
existed both were named `_SAFE_ID` in different files, with different character
classes and length bounds, so the same conceptual check disagreed depending on
which module happened to run it. An identifier accepted by the delivery ledger
could be rejected by the mailer. Keep them named apart.
"""

import re
from datetime import datetime

# Externally-visible identifiers: event, rule, case, and target IDs.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

# Internal composite keys, which may carry base64-style "=" padding and run longer.
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/=-]{0,191}$")

# Owner/actor identities, which may carry "@" and "+".
SAFE_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,191}$")

SECRET_LIKE = re.compile(
    r"https?://|\b(?:authorization|bearer\s+|token\s*=|private key|password\s*=)\b",
    re.IGNORECASE,
)


def require_safe_id(value: str, field: str) -> None:
    if not SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} must be a bounded safe identifier")


def require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def contains_control(value: str) -> bool:
    return any(ord(character) < 32 and character not in "\t\n\r" for character in value)


def require_bounded_text(value: str, field: str, limit: int) -> None:
    if not value.strip() or len(value) > limit:
        raise ValueError(f"{field} must be non-empty and at most {limit} characters")
